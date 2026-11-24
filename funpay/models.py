"""Типы данных FunPay и события, передаваемые между компонентами."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Category:
    """Категория (игра) продавца и её подкатегории."""

    id: int
    name: str
    subcategory_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class AccountInfo:
    """Данные авторизованного аккаунта, полученные с главной страницы."""

    user_id: int
    username: str
    csrf_token: str
    locale: str
    categories: tuple[Category, ...]


@dataclass(frozen=True, slots=True)
class ChatContact:
    """Элемент списка чатов из ответа runner/."""

    chat_id: str
    username: str
    last_message: str
    last_message_id: int


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """Сообщение из истории чата."""

    id: int
    chat_id: str
    author_id: int
    author_name: str | None
    text: str
    is_system: bool
    is_self: bool


@dataclass(frozen=True, slots=True)
class Order:
    """Заказ из раздела «Мои продажи»."""

    id: str
    title: str
    amount: str
    currency: str
    buyer: str
    buyer_id: int
    status: str  # paid | closed | refunded
    chat_id: str


@dataclass(frozen=True, slots=True)
class RaiseResult:
    """Результат поднятия одной категории."""

    category_id: int
    ok: bool
    wait_seconds: int
    next_attempt_at: float
    message: str


# --- события шины -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChatUpdateEvent:
    """В чатах появились новые сообщения."""

    messages: tuple[ChatMessage, ...]


@dataclass(frozen=True, slots=True)
class OrdersChangedEvent:
    """Счётчики заказов изменились — нужно обновить список продаж."""


@dataclass(frozen=True, slots=True)
class AlertEvent:
    """Служебное событие, требующее уведомить админа."""

    kind: str
    text: str


FunPayEvent = ChatUpdateEvent | OrdersChangedEvent | AlertEvent
