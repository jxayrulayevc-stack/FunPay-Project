"""Отправка уведомлений админу через текущий экземпляр Telegram-бота."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.runtime import TelegramRuntime


class Notifier:
    """Тонкая прослойка между бизнес-логикой и TelegramRuntime.

    Ссылка на бота берётся динамически: после /token send_admin указывает
    уже на новый экземпляр Bot, поэтому уведомления не теряются.
    """

    def __init__(self, runtime: TelegramRuntime | None = None, logger: logging.Logger | None = None) -> None:
        self._runtime = runtime
        self._logger = logger or logging.getLogger("funpay_helper")

    def attach(self, runtime: TelegramRuntime) -> None:
        """Привязывает runtime после его создания (разрывает цикл зависимостей)."""
        self._runtime = runtime

    async def send(self, text: str) -> bool:
        """Отправляет сообщение админу, не пробрасывая ошибки Telegram в бизнес-логику."""
        if self._runtime is None:
            return False
        try:
            return await self._runtime.send_admin(text)
        except Exception as exc:  # noqa: BLE001 - Telegram не должен ронять FunPay-цикл
            self._logger.error("Не удалось отправить уведомление в Telegram: %s", exc)
            return False
