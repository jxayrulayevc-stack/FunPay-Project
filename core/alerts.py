"""Дедуплицированные алерты администратору.

Один и тот же алерт (протухшие куки, недоступность FunPay и т.д.) не шлётся
каждые 6 секунд: для каждого kind есть свой cooldown.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.notifier import Notifier


class AlertService:
    """Отправляет повторяющиеся алерты не чаще, чем раз в cooldown секунд."""

    def __init__(self, notifier: Notifier, logger: logging.Logger, default_cooldown: float = 300.0) -> None:
        self._notifier = notifier
        self._logger = logger
        self._default_cooldown = default_cooldown
        self._last_sent: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def send(self, kind: str, text: str, *, cooldown: float | None = None) -> bool:
        """Отправляет алерт, если он не отправлялся недавно."""
        now = time.monotonic()
        async with self._lock:
            last = self._last_sent.get(kind, 0.0)
            if now - last < (self._default_cooldown if cooldown is None else cooldown):
                return False
            self._last_sent[kind] = now
        self._logger.warning("Алерт [%s]: %s", kind, text)
        return await self._notifier.send(text)

    def reset(self, kind: str) -> None:
        """Сбрасывает cooldown — например, после восстановления связи."""
        self._last_sent.pop(kind, None)

    def clear(self, *kinds: str) -> None:
        """Сбрасывает cooldown для группы алертов."""
        for kind in kinds:
            self.reset(kind)
