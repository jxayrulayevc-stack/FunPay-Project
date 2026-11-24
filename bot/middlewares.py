"""Middleware проверки администратора.

Бот молча игнорирует всех, чей Telegram ID не совпадает с ADMIN_ID.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from bot.context import BotContext


class ContextMiddleware(BaseMiddleware):
    """Пробрасывает общий контекст во все обработчики."""

    def __init__(self, context: BotContext) -> None:
        self._context = context

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data["context"] = self._context
        return await handler(event, data)


class AdminOnlyMiddleware(BaseMiddleware):
    """Пропускает только сообщения/колбэки от админа."""

    def __init__(self, admin_id: int) -> None:
        self.admin_id = admin_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is None or getattr(user, "id", None) != self.admin_id:
            return None
        return await handler(event, data)
