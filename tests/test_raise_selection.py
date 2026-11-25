"""Тесты выбора категорий и расписания автоподнятия."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from core.alerts import AlertService
from core.config import Settings
from core.db import Database
from funpay.errors import FunPayError
from funpay.models import MyLotCategory, RaiseResult
from funpay.parsing import is_wait_message, parse_raise_response, parse_wait_time
from funpay.raiser import AutoRaiser


class FakeNotifier:
    """Собирает сообщения админу, не обращаясь к Telegram."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> bool:
        self.messages.append(text)
        return True


class FakeClient:
    """Имитирует profile + raise_game, запрещая любые сетевые вызовы."""

    def __init__(self, categories: list[MyLotCategory], *, fail: bool = False) -> None:
        self.categories = categories
        self.fail = fail
        self.calls: list[tuple[int, str, tuple[int, ...]]] = []
        self.unauthorized = False

    async def wait_authorized(self, stop_event: Any) -> bool:
        return stop_event.is_set()

    async def get_my_lot_categories(self) -> list[MyLotCategory]:
        return self.categories

    async def raise_game(self, game_id: int, game_name: str, node_ids: Any) -> RaiseResult:
        self.calls.append((game_id, game_name, tuple(node_ids)))
        if self.fail:
            raise FunPayError("Сбой FunPay")
        return RaiseResult(game_id, True, 14400, time.time() + 14400, "Лоты подняты")


class FixedRng:
    """Детерминированный генератор: uniform всегда возвращает левую границу."""

    @staticmethod
    def uniform(low: float, high: float) -> float:
        return low


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        admin_id=1,
        bot_token="test-token",
        data_dir=tmp_path,
        log_dir=tmp_path,
        db_path=tmp_path / "bot.db",
        log_level="INFO",
        poll_interval=6.0,
        account_refresh_minutes=45,
        lots_refresh_minutes=30,
        funpay_request_delay=2.0,
        heartbeat_interval=60,
        watchdog_timeout=180,
        funpay_unavailable_after=300,
        healthchecks_url="",
        healthcheck_max_age=180,
        dry_run=False,
        debug=False,
        user_agent="test-agent",
    )


def _raiser(db: Database, client: FakeClient, notifier: FakeNotifier, settings: Settings) -> AutoRaiser:
    watchdog = SimpleNamespace(tick=lambda: None)
    return AutoRaiser(
        db,
        client,  # type: ignore[arg-type]
        notifier,  # type: ignore[arg-type]
        AlertService(notifier, logging.getLogger("test")),  # type: ignore[arg-type]
        settings,
        logging.getLogger("test"),
        watchdog,  # type: ignore[arg-type]
    )


async def test_only_my_lots_are_selected_and_raised(tmp_path: Path) -> None:
    my_lots = [MyLotCategory(333, "Counter-Strike 2", 1350, "Аккаунты Counter-Strike 2", 5)]
    db = Database(_settings(tmp_path).db_path)
    await db.connect()
    # Старое расписание по чужим играм должно исчезнуть при обновлении.
    await db.set_raise_state(163, "Drakensang Online", None, time.time() + 60, None, 4)
    await db.set_raise_state(853, "Dreadmyst", None, time.time() + 60, None, 5)
    client = FakeClient(my_lots)
    notifier = FakeNotifier()
    raiser = _raiser(db, client, notifier, _settings(tmp_path))

    await raiser.refresh_lots()
    targets = await raiser.targets()
    assert [(item.game_id, item.node_ids) for item in targets] == [(333, (1350,))]

    results = await raiser.raise_all(report=False)
    assert len(results) == 1
    assert client.calls == [(333, "Counter-Strike 2", (1350,))]
    states = await db.all_raise_states()
    assert [int(item["category_id"]) for item in states] == [333]
    assert states[0]["node_ids"] == "1350"
    assert states[0]["next_raise_at"] > time.time() + 3.9 * 3600
    assert notifier.messages == []
    await db.close()


async def test_wait_messages_are_parsed(tmp_path: Path) -> None:
    assert parse_wait_time("Подождите 13 минут") == 780
    assert parse_wait_time("Подождите 2 часа") == 7200
    assert parse_wait_time("Подождите 1 час 30 минут") == 5400
    assert parse_wait_time("Please wait 45 seconds") == 45
    assert is_wait_message("Подождите 13 минут") is True
    parsed = parse_raise_response({"error": True, "msg": "Подождите 13 минут"}, category_id=333, now=1000.0)
    assert parsed.ok is False
    assert parsed.wait_seconds == 780


def test_next_raise_at_uses_buffer_and_cooldown() -> None:
    assert (
        AutoRaiser._next_at(1000.0, 780, from_wait=True, cooldown=14400, rng=FixedRng()) == 1000.0 + 780 + 30
    )
    assert AutoRaiser._next_at(1000.0, 0, from_wait=False, cooldown=14400, rng=FixedRng()) == 1000.0 + 14400


async def test_three_failures_postpone_for_10_15_minutes(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr("funpay.raiser.random.uniform", lambda low, high: 600.0)
    db = Database(_settings(tmp_path).db_path)
    await db.connect()
    await db.replace_my_lot_categories(
        [MyLotCategory(333, "Counter-Strike 2", 1350, "Аккаунты Counter-Strike 2", 5)]
    )
    await db.set_raise_state(333, "Counter-Strike 2", None, 0, "Сбой", 2, (1350,))
    client = FakeClient([], fail=True)
    notifier = FakeNotifier()
    raiser = _raiser(db, client, notifier, _settings(tmp_path))

    await raiser.raise_all(report=False)
    state = await db.get_raise_state(333)
    assert state is not None
    assert 599 < state["next_raise_at"] - time.time() <= 601
    assert state["error_count"] == 3
    await db.close()
