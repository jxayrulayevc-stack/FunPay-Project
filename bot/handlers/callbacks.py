"""Обработчики inline-кнопок."""

from __future__ import annotations

import html
from typing import Any

from aiogram import types
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext

from bot.context import BotContext
from bot.handlers.commands import autoreply_menu_text, raise_list_text, raise_menu_text, status_text
from bot.handlers.router import router
from bot.keyboards import MenuAction, main_menu
from bot.states import SecretStates, SettingsStates
from core.logger import read_last_lines


async def _edit(
    callback: types.CallbackQuery,
    text: str,
    markup: Any = None,
) -> None:
    """Безопасно редактирует сообщение с меню."""
    try:
        await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    except TelegramAPIError:
        pass


@router.callback_query(MenuAction.filter())
async def handle_menu(
    callback: types.CallbackQuery,
    callback_data: MenuAction,
    state: FSMContext,
    context: BotContext,
) -> None:
    """Маршрутизирует действия из меню."""
    action = callback_data.action

    if action == "main":
        await callback.answer()
        await _edit(callback, "Главное меню:", main_menu())
        return

    if action == "status":
        await callback.answer()
        await _edit(callback, await status_text(context))
        return

    if action == "logs":
        await callback.answer()
        tail = read_last_lines(context.settings.log_dir, 30, context.masker)
        await _edit(callback, f"<pre>{html.escape(tail[:3500])}</pre>")
        return

    if action == "autoreply":
        await callback.answer()
        text, markup = await autoreply_menu_text(context)
        await _edit(callback, text, markup)
        return

    if action == "autoreply_toggle":
        current = await context.db.get_bool_setting("autoreply_enabled", True)
        await context.db.set_setting("autoreply_enabled", "0" if current else "1")
        await callback.answer("Автоответ переключён")
        text, markup = await autoreply_menu_text(context)
        await _edit(callback, text, markup)
        return

    if action == "autoreply_text":
        await state.set_state(SettingsStates.wait_autoreply_text)
        await callback.answer()
        await _edit(callback, "Пришлите новый текст автоответа (следующим сообщением).")
        return

    if action == "autoreply_interval":
        await state.set_state(SettingsStates.wait_autoreply_interval)
        await callback.answer()
        await _edit(callback, "Пришлите интервал в часах (например, 6).")
        return

    if action == "order_toggle":
        current = await context.db.get_bool_setting("autoreply_order_enabled", False)
        await context.db.set_setting("autoreply_order_enabled", "0" if current else "1")
        await callback.answer("Ответ на заказ переключён")
        text, markup = await autoreply_menu_text(context)
        await _edit(callback, text, markup)
        return

    if action == "order_text":
        await state.set_state(SettingsStates.wait_order_text)
        await callback.answer()
        await _edit(callback, "Пришлите текст ответа на заказ (следующим сообщением).")
        return

    if action == "raise":
        await callback.answer()
        text, markup = await raise_menu_text(context)
        await _edit(callback, text, markup)
        return

    if action == "raise_now":
        await callback.answer("Поднимаю лоты, отчёт придёт отдельным сообщением")
        await context.raiser.raise_all(report=True)
        return

    if action == "raise_list":
        await callback.answer()
        await _edit(callback, await raise_list_text(context))
        return

    if action == "raise_refresh":
        await callback.answer("Обновляю список лотов")
        try:
            categories = await context.raiser.refresh_lots(notify=False)
        except Exception as exc:  # noqa: BLE001 - показываем причину админу
            await _edit(callback, f"❌ Не удалось обновить лоты: {html.escape(str(exc))}")
            return
        await _edit(
            callback,
            f"✅ Лоты обновлены: {len({item.game_id for item in categories})} игр, "
            f"{sum(item.lot_count for item in categories)} лотов.",
        )
        return

    if action == "raise_exclude":
        await state.set_state(SettingsStates.wait_raise_exclude_id)
        await callback.answer()
        await _edit(callback, "Пришлите ID из /raise_list, который нужно исключить.")
        return

    if action == "raise_include":
        await state.set_state(SettingsStates.wait_raise_include_id)
        await callback.answer()
        await _edit(callback, "Пришлите ID исключённой категории, которую нужно вернуть.")
        return

    if action == "raise_toggle":
        current = await context.db.get_bool_setting("autoraise_enabled", True)
        await context.db.set_setting("autoraise_enabled", "0" if current else "1")
        await callback.answer("Автоподнятие переключено")
        text, markup = await raise_menu_text(context)
        await _edit(callback, text, markup)
        return

    if action == "raise_report":
        current = await context.db.get_bool_setting("raise_report_enabled", True)
        await context.db.set_setting("raise_report_enabled", "0" if current else "1")
        await callback.answer("Отчёт переключён")
        text, markup = await raise_menu_text(context)
        await _edit(callback, text, markup)
        return

    if action == "token":
        await state.set_state(SecretStates.wait_token)
        await callback.answer()
        await _edit(callback, "Пришлите новый токен Telegram следующим сообщением.")
        return

    if action == "cookie":
        await state.set_state(SecretStates.wait_cookie)
        await callback.answer()
        await _edit(callback, "Пришлите golden_key FunPay следующим сообщением.")
        return

    await callback.answer("Неизвестное действие")
