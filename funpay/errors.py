"""Исключения клиента FunPay."""

from __future__ import annotations


class FunPayError(Exception):
    """Базовая ошибка работы с FunPay."""


class UnauthorizedError(FunPayError):
    """Куки недействительны: FunPay перенаправил на страницу входа."""


class RateLimitError(FunPayError):
    """FunPay вернул 429 или попросил подождать."""


class NetworkError(FunPayError):
    """Сетевая ошибка или 5xx от FunPay."""


class ParseError(FunPayError):
    """Не удалось разобрать ответ FunPay."""


class RaiseError(FunPayError):
    """FunPay отказался поднимать лоты (обычно — кулдаун)."""

    def __init__(self, message: str, wait_seconds: int | None = None, url: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.wait_seconds = wait_seconds
        self.url = url
