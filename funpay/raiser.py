"""Автоподнятие только тех категорий, где есть мои активные лоты.

Запрос на поднятие отправляется один раз на игру (game_id) и содержит только
те node_id, которые реально есть в моём профиле. Расписание хранится в SQLite.
"""

from __future__ import annotations

import asyncio
import html
import logging
import random
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Protocol

from core.alerts import AlertService
from core.notifier import Notifier
from core.watchdog import wait_or_stop
from funpay.errors import FunPayError, NetworkError, RaiseError, RateLimitError, UnauthorizedError
from funpay.models import MyLotCategory, RaiseResult, RaiseTarget
from funpay.parsing import format_duration, is_wait_message

if TYPE_CHECKING:
    from core.config import Settings
    from core.db import Database
    from core.watchdog import Watchdog


class RaiserClient(Protocol):
    """Минимальный интерфейс клиента, необходимый автоподнятию."""

    async def wait_authorized(self, stop_event: asyncio.Event) -> bool: ...

    async def get_my_lot_categories(self) -> list[MyLotCategory]: ...

    async def raise_game(self, game_id: int, game_name: str, node_ids: Sequence[int]) -> RaiseResult: ...


class AutoRaiser:
    """Последовательная очередь поднятия с кулдаунами и защитой от 429."""

    def __init__(
        self,
        db: Database,
        client: RaiserClient,
        notifier: Notifier,
        alerts: AlertService,
        settings: Settings,
        logger: logging.Logger,
        watchdog: Watchdog,
        *,
        dry_run: bool = False,
    ) -> None:
        self._db = db
        self._client = client
        self._notifier = notifier
        self._alerts = alerts
        self._settings = settings
        self._logger = logger
        self._watchdog = watchdog
        self._dry_run = dry_run
        self._running = asyncio.Lock()

    # --- фоновая задача ------------------------------------------------------

    async def run(self, stop_event: asyncio.Event) -> None:
        """Обновляет лоты раз в N минут и поднимает только готовые категории."""
        refresh_interval = self._settings.lots_refresh_minutes * 60
        last_refresh = 0.0
        while not stop_event.is_set():
            try:
                if await self._client.wait_authorized(stop_event):
                    return
                now = time.time()
                if not last_refresh or now - last_refresh >= refresh_interval:
                    await self.refresh_lots()
                    last_refresh = time.time()
                next_at = await self.next_raise_at()
                if next_at is None:
                    next_at = time.time() + 15.0
                delay = max(0.0, next_at - time.time())
                until_refresh = max(0.0, refresh_interval - (time.time() - last_refresh))
                sleep_for = min(delay, until_refresh) if until_refresh else delay
                if sleep_for > 0:
                    if await wait_or_stop(stop_event, min(sleep_for, 60.0)):
                        return
                    continue
                if not await self._db.get_bool_setting("autoraise_enabled", True):
                    if await wait_or_stop(stop_event, 60.0):
                        return
                    continue
                await self.raise_all(report=True)
                self._watchdog.tick()
                if await wait_or_stop(stop_event, 5.0):
                    return
            except UnauthorizedError:
                await self._alerts.send(
                    "cookies_invalid",
                    "🔑 Куки недействительны, пришли новые через /cookie. Поднятие лотов остановлено.",
                )
                if await wait_or_stop(stop_event, 30.0):
                    return
            except Exception as exc:  # noqa: BLE001 - автоподнятие не роняет сервис
                self._logger.exception("Ошибка цикла автоподнятия: %s", exc)
                if await wait_or_stop(stop_event, 60.0):
                    return

    async def next_raise_at(self) -> float | None:
        """Ближайшее поднятие среди моих категорий (исключения не учитываются)."""
        return await self._db.get_active_next_raise_at()

    # --- список моих лотов ---------------------------------------------------

    async def refresh_lots(self, *, notify: bool = True) -> list[MyLotCategory]:
        """Загружает профиль, обновляет кэш и чистит старое расписание."""
        categories = await self._client.get_my_lot_categories()
        await self._db.replace_my_lot_categories(categories)
        await self._db.prune_raise_states({item.game_id for item in categories})
        await self._db.set_meta("my_lots_refreshed_at", str(time.time()))
        await self._db.set_meta("my_lots_count", str(len(categories)))
        await self._sync_next_meta()
        if categories:
            self._alerts.reset("no_lots")
            self._logger.info(
                "Мои лоты обновлены: %s подкатегорий, %s активных лотов",
                len(categories),
                sum(item.lot_count for item in categories),
            )
        elif notify:
            await self._alerts.send("no_lots", "Не найдено активных лотов для поднятия", cooldown=3600)
        return categories

    async def targets(self) -> list[RaiseTarget]:
        """Группирует мои лоты по играм, убирая чёрный список."""
        rows = await self._db.my_lot_categories()
        excludes = await self._db.excludes()
        excluded_games = {int(item["target_id"]) for item in excludes if item["kind"] == "game"}
        excluded_nodes = {int(item["target_id"]) for item in excludes if item["kind"] == "node"}
        grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            game_id = int(row["game_id"])
            node_id = int(row["node_id"])
            if game_id in excluded_games or node_id in excluded_nodes:
                continue
            grouped[game_id].append(row)
        result: list[RaiseTarget] = []
        for game_id, items in grouped.items():
            items.sort(key=lambda item: int(item["node_id"]))
            result.append(
                RaiseTarget(
                    game_id=game_id,
                    game_name=str(items[0].get("game_name") or f"Игра {game_id}"),
                    node_ids=tuple(int(item["node_id"]) for item in items),
                    node_names=tuple(str(item.get("node_name") or "") for item in items),
                    lot_count=sum(int(item.get("lot_count") or 0) for item in items),
                )
            )
        return sorted(result, key=lambda item: item.game_id)

    # --- поднятие ------------------------------------------------------------

    async def raise_all(self, *, report: bool = True) -> list[RaiseResult]:
        """Поднимает готовые игры последовательно; отчёт — одним сообщением."""
        if self._running.locked():
            self._logger.debug("Поднятие уже выполняется, пропуск")
            return []
        async with self._running:
            return await self._raise_all_locked(report=report)

    async def _raise_all_locked(self, *, report: bool) -> list[RaiseResult]:
        targets = await self.targets()
        if not targets:
            await self._db.set_meta("next_raise_at", "")
            if report and await self._db.get_bool_setting("raise_report_enabled", True):
                await self._notifier.send("⬆️ Нет категорий с активными лотами для поднятия.")
            return []

        cooldown_hours = max(0.5, await self._db.get_float_setting("raise_interval_hours", 4.0))
        cooldown = cooldown_hours * 3600
        results: list[RaiseResult] = []
        waiting: list[str] = []
        now = time.time()

        for target in targets:
            state = await self._db.get_raise_state(target.game_id) or {}
            ready_at = float(state.get("next_raise_at") or 0)
            if ready_at > now:
                waiting.append(
                    f"{html.escape(target.game_name)} (#{target.game_id}): "
                    f"через {format_duration(ready_at - now)}"
                )
                continue
            previous_errors = int(state.get("error_count") or 0)
            try:
                if self._dry_run:
                    result = RaiseResult(
                        target.game_id,
                        True,
                        int(cooldown),
                        now + cooldown,
                        "DRY-RUN: запрос не отправлен",
                    )
                else:
                    result = await self._client.raise_game(target.game_id, target.game_name, target.node_ids)
            except RaiseError as exc:
                result = RaiseResult(
                    target.game_id, False, exc.wait_seconds or int(cooldown), now, exc.message
                )
            except (NetworkError, RateLimitError, FunPayError) as exc:
                result = RaiseResult(target.game_id, False, 900, now, f"Ошибка запроса: {exc}")
            except UnauthorizedError:
                await self._alerts.send(
                    "cookies_invalid", "🔑 Куки недействительны, пришли новые через /cookie."
                )
                break

            error_count = 0 if result.ok else previous_errors + 1
            from_wait = is_wait_message(result.message)
            if result.ok:
                next_at = self._next_at(now, cooldown, from_wait=False, cooldown=cooldown)
            elif error_count >= 3:
                # После трёх неудач не дёргаем FunPay, а откладываем на 10–15 минут.
                pause = random.uniform(600, 900)
                next_at = now + pause
                result = replace(
                    result,
                    wait_seconds=int(pause),
                    next_attempt_at=next_at,
                    message=f"{result.message} (пауза после ошибок)",
                )
                await self._alerts.send(
                    "raise_failures",
                    "❌ Не удаётся поднять лоты уже 3 раза подряд. Посмотрите /logs.",
                )
            elif from_wait:
                next_at = self._next_at(now, result.wait_seconds, from_wait=True, cooldown=cooldown)
            else:
                next_at = now + max(result.wait_seconds, 60) + random.uniform(0, 30)

            await self._db.set_raise_state(
                target.game_id,
                target.game_name,
                now if result.ok else state.get("last_raise_at"),
                next_at,
                None if result.ok else result.message,
                error_count,
                target.node_ids,
            )
            nodes = ", ".join(str(node_id) for node_id in target.node_ids)
            next_text = format_duration(next_at - now)
            if result.ok:
                self._logger.info(
                    "⬆️ Поднята игра %s (%s), node_id=%s; следующее через %s",
                    target.game_id,
                    target.game_name,
                    nodes,
                    next_text,
                )
            elif from_wait:
                self._logger.debug(
                    "⏳ Игра %s (%s): %s; следующее через %s",
                    target.game_id,
                    target.game_name,
                    result.message,
                    next_text,
                )
            else:
                self._logger.warning(
                    "⚠️ Игра %s (%s): %s; следующее через %s",
                    target.game_id,
                    target.game_name,
                    result.message,
                    next_text,
                )
            results.append(result)

        await self._sync_next_meta()
        if any(result.ok for result in results):
            await self._db.set_meta("last_raise_at", str(time.time()))
            self._alerts.reset("raise_failures")
        if report and await self._db.get_bool_setting("raise_report_enabled", True):
            await self._notifier.send(self._format_report(results, waiting, await self.next_raise_at()))
        return results

    @staticmethod
    def _next_at(
        now: float,
        wait_seconds: float,
        *,
        from_wait: bool,
        cooldown: float,
        rng: Any = random,
    ) -> float:
        """Время следующей попытки: ответ FunPay + запас/джиттер или кулдаун."""
        if from_wait:
            extra = rng.uniform(30, 60) + rng.uniform(0, 30)
            return now + max(1, wait_seconds) + extra
        return now + cooldown + rng.uniform(0, 60)

    async def _sync_next_meta(self) -> None:
        next_at = await self.next_raise_at()
        await self._db.set_meta("next_raise_at", str(next_at) if next_at is not None else "")

    @staticmethod
    def _format_report(
        results: list[RaiseResult],
        waiting: Sequence[str] = (),
        next_at: float | None = None,
    ) -> str:
        """Короткий отчёт одним сообщением после цикла поднятия."""
        lines = ["⬆️ <b>Поднятие лотов</b>"]
        lines.append(f"Поднято: {sum(1 for item in results if item.ok)}")
        for result in results:
            icon = "✅" if result.ok else "⏳"
            lines.append(
                f"{icon} #{result.category_id}: {html.escape(result.message)} — "
                f"следующее через {format_duration(result.wait_seconds)}"
            )
        waiting_list = list(waiting)
        if waiting_list:
            lines.append("Ждут кулдауна:")
            lines.extend(f"• {item}" for item in waiting_list[:10])
            if len(waiting_list) > 10:
                lines.append(f"• и ещё {len(waiting_list) - 10}")
        if next_at is not None:
            lines.append(f"Ближайшее поднятие: через {format_duration(next_at - time.time())}")
        return "\n".join(lines)

    # --- чёрный список -------------------------------------------------------

    @staticmethod
    def _parse_id(raw: str) -> tuple[str | None, int | None]:
        """Разбирает «333», «g333» (игра) или «n1350» (подкатегория)."""
        text = raw.strip().lower().lstrip("#")
        hint: str | None = None
        if text.startswith("g"):
            hint, text = "game", text[1:]
        elif text.startswith("n"):
            hint, text = "node", text[1:]
        if not text.isdigit():
            return hint, None
        return hint, int(text)

    async def resolve_target(self, raw: str) -> tuple[str, int, str, int] | None:
        """Возвращает (kind, id, название, game_id) для моей категории."""
        hint, target_id = self._parse_id(raw)
        if target_id is None:
            return None
        rows = await self._db.my_lot_categories()
        games: dict[int, str] = {}
        nodes: dict[int, tuple[str, int]] = {}
        for row in rows:
            game_id = int(row["game_id"])
            games[game_id] = str(row.get("game_name") or f"Игра {game_id}")
            nodes[int(row["node_id"])] = (str(row.get("node_name") or ""), game_id)
        if hint in (None, "game") and target_id in games:
            return "game", target_id, games[target_id], target_id
        if hint in (None, "node") and target_id in nodes:
            name, game_id = nodes[target_id]
            return "node", target_id, name, game_id
        return None

    async def exclude(self, raw: str) -> str:
        """Исключает игру/подкатегорию из поднятия."""
        found = await self.resolve_target(raw)
        if found is None:
            return "Не нашёл такую категорию среди моих лотов. Пришлите ID из /raise_list."
        kind, target_id, name, game_id = found
        await self._db.set_exclude(kind, target_id, name)
        if kind == "game":
            await self._db.delete_raise_state(target_id)
        elif not any(item.game_id == game_id for item in await self.targets()):
            await self._db.delete_raise_state(game_id)
        await self._sync_next_meta()
        return f"Исключено: {html.escape(name)} ({kind} {target_id})"

    async def include(self, raw: str) -> str:
        """Возвращает категорию в поднятие."""
        hint, target_id = self._parse_id(raw)
        if target_id is None:
            return "Пришлите ID из /raise_list, например 333 или n1350."
        excludes = [
            item
            for item in await self._db.excludes()
            if int(item["target_id"]) == target_id and (hint is None or item["kind"] == hint)
        ]
        if not excludes:
            return "Эта категория не была исключена."
        for item in excludes:
            await self._db.remove_exclude(str(item["kind"]), target_id)
        targets = await self.targets()
        for target in targets:
            if target.game_id == target_id or any(node_id == target_id for node_id in target.node_ids):
                await self._db.set_raise_state(
                    target.game_id,
                    target.game_name,
                    None,
                    time.time() + 5.0,
                    None,
                    0,
                    target.node_ids,
                )
                break
        await self._sync_next_meta()
        return f"Возвращено в поднятие: ID {target_id}"


__all__ = ["AutoRaiser"]
