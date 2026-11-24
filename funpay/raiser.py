"""Автоподнятие лотов по кулдауну FunPay."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from core.alerts import AlertService
from core.notifier import Notifier
from core.watchdog import wait_or_stop
from funpay.errors import FunPayError, NetworkError, RaiseError, RateLimitError, UnauthorizedError
from funpay.models import RaiseResult
from funpay.parsing import format_duration

if TYPE_CHECKING:
    from core.config import Settings
    from core.db import Database
    from core.watchdog import Watchdog
    from funpay.client import FunPayClient


class AutoRaiser:
    """Поднимает все категории и планирует следующую попытку по ответу FunPay."""

    def __init__(
        self,
        db: Database,
        client: FunPayClient,
        notifier: Notifier,
        alerts: AlertService,
        settings: Settings,
        logger: logging.Logger,
        watchdog: Watchdog,
    ) -> None:
        self._db = db
        self._client = client
        self._notifier = notifier
        self._alerts = alerts
        self._settings = settings
        self._logger = logger
        self._watchdog = watchdog
        self._running = asyncio.Lock()

    async def run(self, stop_event: asyncio.Event) -> None:
        """Фоновая задача, ожидающая next_raise_at из БД."""
        while not stop_event.is_set():
            try:
                enabled = await self._db.get_bool_setting("autoraise_enabled", True)
                next_at = await self._db.get_next_raise_at()
                now = time.time()
                if next_at is None:
                    next_at = now + 15.0  # даём слушателю время на старт
                delay = max(0.0, next_at - now)
                if delay > 0:
                    if await wait_or_stop(stop_event, min(delay, 60.0)):
                        return
                    continue
                if not enabled:
                    if await wait_or_stop(stop_event, 60.0):
                        return
                    continue
                if not self._client.categories:
                    await self._client.refresh_session()
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

    async def raise_all(self, *, report: bool = True) -> list[RaiseResult]:
        """Поднимает все категории и сохраняет время следующей попытки."""
        if self._running.locked():
            self._logger.debug("Поднятие уже выполняется, пропуск")
            return []
        async with self._running:
            return await self._raise_all_locked(report=report)

    async def _raise_all_locked(self, *, report: bool) -> list[RaiseResult]:
        now = time.time()
        fallback = int(await self._db.get_float_setting("raise_interval_hours", 4.0) * 3600)
        results: list[RaiseResult] = []

        for category in self._client.categories:
            state = await self._db.get_raise_state(category.id) or {}
            previous_errors = int(state.get("error_count") or 0)
            try:
                result = await self._client.raise_category(category)
            except RaiseError as exc:
                wait = exc.wait_seconds or fallback
                result = RaiseResult(category.id, False, wait, now + wait, exc.message)
            except (NetworkError, RateLimitError, FunPayError) as exc:
                result = RaiseResult(category.id, False, 900, now + 900, f"Ошибка запроса: {exc}")
            except UnauthorizedError:
                await self._alerts.send(
                    "cookies_invalid", "🔑 Куки недействительны, пришли новые через /cookie."
                )
                break

            error_count = 0 if result.ok else previous_errors + 1
            await self._db.set_raise_state(
                category.id,
                category.name,
                now if result.ok else state.get("last_raise_at"),
                result.next_attempt_at,
                None if result.ok else result.message,
                error_count,
            )
            self._logger.info(
                "Поднятие категории %s (%s): %s, следующая попытка через %s",
                category.id,
                category.name,
                "успешно" if result.ok else result.message,
                format_duration(result.wait_seconds),
            )
            results.append(result)

        if not results:
            return []

        next_at = min(result.next_attempt_at for result in results)
        await self._db.set_meta("next_raise_at", str(next_at))
        if any(result.ok for result in results):
            await self._db.set_meta("last_raise_at", str(now))
            self._alerts.reset("raise_failures")
        error_counts: list[int] = []
        for result in results:
            state = await self._db.get_raise_state(result.category_id) or {}
            error_counts.append(int(state.get("error_count") or 0))
        if error_counts and max(error_counts) >= 3:
            await self._alerts.send(
                "raise_failures",
                "❌ Не удаётся поднять лоты уже 3 раза подряд. Посмотрите /logs.",
            )
        if report and await self._db.get_bool_setting("raise_report_enabled", True):
            await self._notifier.send(self._format_report(results))
        return results

    @staticmethod
    def _format_report(results: list[RaiseResult]) -> str:
        """Короткий отчёт для Telegram."""
        lines = ["⬆️ <b>Поднятие лотов</b>"]
        for result in results:
            icon = "✅" if result.ok else "⏳"
            lines.append(
                f"{icon} {result.category_id}: через "
                f"{format_duration(result.wait_seconds)} — {result.message}"
            )
        return "\n".join(lines)
