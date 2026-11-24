"""Watchdog главного цикла и heartbeat в БД.

Если ни один из долгоживущих тасков не отметился дольше timeout секунд,
процесс завершается кодом 1 — systemd с Restart=always поднимает его заново.
Параллельно раз в минуту в БД пишется метка, которую проверяет healthcheck-таймер.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.db import Database
    from core.notifier import Notifier


async def wait_or_stop(stop_event: asyncio.Event, seconds: float) -> bool:
    """Ждёт либо timeout, либо stop_event. Возвращает True, если запрошен stop."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=max(0.0, seconds))
        return True
    except TimeoutError:
        return False


class Watchdog:
    """Следит за регулярностью вызовов tick() из рабочих циклов."""

    def __init__(
        self,
        timeout_seconds: int,
        logger: logging.Logger,
        notifier: Notifier | None = None,
        on_timeout: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._timeout = float(timeout_seconds)
        self._logger = logger
        self._notifier = notifier
        self._on_timeout = on_timeout
        self._last_tick = time.monotonic()

    def tick(self) -> None:
        """Отмечает, что цикл жив."""
        self._last_tick = time.monotonic()

    @property
    def seconds_since_tick(self) -> float:
        return time.monotonic() - self._last_tick

    async def run(self, stop_event: asyncio.Event) -> None:
        """Проверяет свежесть tick каждые 5 секунд."""
        self._logger.info("Watchdog запущен (таймаут %s сек)", int(self._timeout))
        while not stop_event.is_set():
            if await wait_or_stop(stop_event, 5.0):
                return
            if self.seconds_since_tick <= self._timeout:
                continue
            self._logger.critical(
                "Watchdog: главный цикл не отвечает %s сек. Перезапуск процесса.",
                int(self.seconds_since_tick),
            )
            if self._notifier is not None:
                try:
                    await asyncio.wait_for(
                        self._notifier.send("🚨 Watchdog: бот завис, процесс перезапускается."),
                        timeout=3.0,
                    )
                except Exception:  # noqa: BLE001 - алерт не должен блокировать перезапуск
                    pass
            if self._on_timeout is not None:
                try:
                    await self._on_timeout()
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.sleep(0.5)
            os._exit(1)


class Heartbeat:
    """Раз в минуту обновляет метку в БД и «пинает» watchdog."""

    def __init__(
        self, db: Database, watchdog: Watchdog, logger: logging.Logger, interval_seconds: int
    ) -> None:
        self._db = db
        self._watchdog = watchdog
        self._logger = logger
        self._interval = float(interval_seconds)

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self._db.set_heartbeat(time.time())
                self._watchdog.tick()
            except Exception as exc:  # noqa: BLE001 - БД не должна убивать сервис
                self._logger.error("Heartbeat: не удалось записать метку: %s", exc)
            if await wait_or_stop(stop_event, self._interval):
                return
