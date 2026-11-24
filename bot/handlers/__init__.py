"""Регистрация всех обработчиков."""

from __future__ import annotations

from aiogram import Dispatcher

from bot.context import BotContext
from bot.handlers import callbacks, commands  # noqa: F401 - импорт регистраторов
from bot.handlers.router import router


def register_handlers(dispatcher: Dispatcher, _context: BotContext) -> None:
    """Подключает роутер к Dispatcher (контекст пробрасывается middleware)."""
    dispatcher.include_router(router)
