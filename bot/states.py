"""Состояния FSM для пошагового ввода секретов и настроек."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class SecretStates(StatesGroup):
    """Ожидание нового токена/куков."""

    wait_token = State()
    wait_cookie = State()


class SettingsStates(StatesGroup):
    """Ожидание текста и интервала автоответа."""

    wait_autoreply_text = State()
    wait_autoreply_interval = State()
    wait_order_text = State()
