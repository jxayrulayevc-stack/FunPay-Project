"""Inline-клавиатуры для управления ботом."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


class MenuAction(CallbackData, prefix="fph"):
    """Действие, пришедшее из inline-кнопки."""

    action: str


def _markup(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _button(text: str, action: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=MenuAction(action=action).pack())


def main_menu() -> InlineKeyboardMarkup:
    """Главное меню."""
    return _markup(
        [
            [_button("📊 Статус", "status"), _button("📄 Логи", "logs")],
            [_button("🤖 Автоответ", "autoreply"), _button("⬆️ Поднятие", "raise")],
            [_button("🍪 Куки FunPay", "cookie"), _button("🔑 Токен бота", "token")],
        ]
    )


def autoreply_menu(enabled: bool, interval: float, order_enabled: bool) -> InlineKeyboardMarkup:
    """Меню автоответчика."""
    state = "вкл" if enabled else "выкл"
    order_state = "вкл" if order_enabled else "выкл"
    return _markup(
        [
            [_button(f"Автоответ: {state}", "autoreply_toggle")],
            [_button("✏️ Текст ответа", "autoreply_text")],
            [_button(f"Интервал: {interval:g} ч", "autoreply_interval")],
            [_button(f"Ответ на заказ: {order_state}", "order_toggle")],
            [_button("✏️ Текст для заказа", "order_text")],
            [_button("🔙 Назад", "main")],
        ]
    )


def raise_menu(enabled: bool, report: bool) -> InlineKeyboardMarkup:
    """Меню автоподнятия."""
    return _markup(
        [
            [_button("⬆️ Поднять сейчас", "raise_now")],
            [_button("📋 Список категорий", "raise_list")],
            [_button("🔄 Обновить лоты", "raise_refresh")],
            [_button("🚫 Исключить ID", "raise_exclude"), _button("✅ Вернуть ID", "raise_include")],
            [_button(f"Автоподнятие: {'вкл' if enabled else 'выкл'}", "raise_toggle")],
            [_button(f"Отчёт в TG: {'вкл' if report else 'выкл'}", "raise_report")],
            [_button("🔙 Назад", "main")],
        ]
    )


__all__ = ["MenuAction", "main_menu", "autoreply_menu", "raise_menu"]
