"""Тесты middleware проверки администратора."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

from aiogram.types import TelegramObject

from bot.middlewares import AdminOnlyMiddleware

ADMIN_ID = 8875725946


async def _handler(event: TelegramObject, data: dict[str, Any]) -> str:  # noqa: ARG001
    return "handled"


def _event(user_id: int | None) -> TelegramObject:
    user = SimpleNamespace(id=user_id) if user_id is not None else None
    return SimpleNamespace(from_user=user)  # type: ignore[return-value]


def test_admin_passes() -> None:
    middleware = AdminOnlyMiddleware(ADMIN_ID)
    result = asyncio.run(
        middleware(_handler, _event(ADMIN_ID), {})  # type: ignore[arg-type]
    )
    assert result == "handled"


def test_non_admin_ignored_silently() -> None:
    middleware = AdminOnlyMiddleware(ADMIN_ID)
    result = asyncio.run(
        middleware(_handler, _event(12345), {})  # type: ignore[arg-type]
    )
    assert result is None


def test_callback_without_user_ignored() -> None:
    middleware = AdminOnlyMiddleware(ADMIN_ID)
    result = asyncio.run(middleware(_handler, _event(None), {}))  # type: ignore[arg-type]
    assert result is None


def test_handler_callable_signature() -> None:
    handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[str]] = _handler
    assert asyncio.run(handler(_event(ADMIN_ID), {})) == "handled"
