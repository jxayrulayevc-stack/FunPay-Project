"""Команды Telegram-бота."""

from __future__ import annotations

import html
import time
from typing import Any

from aiogram import types
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext

from bot.context import BotContext
from bot.handlers.router import router
from bot.keyboards import autoreply_menu, main_menu, raise_menu
from bot.secrets import check_bot_token, parse_cookie_input
from bot.states import SecretStates, SettingsStates
from core.logger import read_last_lines
from funpay.autoreply import AutoreplyConfig
from funpay.errors import FunPayError, UnauthorizedError
from funpay.parsing import format_duration

STATUS_WAIT_TEXT = "Пришлите новый токен Telegram в следующем сообщении."
COOKIE_WAIT_TEXT = (
    "Пришлите golden_key FunPay.\n"
    "Можно в одном из форматов:\n"
    "• просто токен\n"
    "• <code>golden_key=...; PHPSESSID=...</code>\n"
    '• JSON <code>{"golden_key": "...", "phpsessid": "..."}</code>\n'
    "Сообщение с секретом будет сразу удалено."
)


async def _delete_secret(message: types.Message) -> None:
    """Удаляет сообщение с токеном/куками, не логируя его содержимое."""
    try:
        await message.delete()
    except TelegramAPIError:
        pass  # сообщение могло быть старше 48 часов или бот не админ в чате


async def status_text(context: BotContext) -> str:
    """Формирует текст для /status."""
    db = context.db
    client = context.client
    cfg = await AutoreplyConfig.load(db)
    next_raise = await context.raiser.next_raise_at()
    last_raise_raw = await db.get_meta("last_raise_at")
    heartbeat = await db.get_heartbeat()
    uptime = format_duration(time.time() - context.started_at)
    session = "❌ НЕЙСТВИТЕЛЬНЫ" if client.unauthorized else "✅ активна"
    autoreply_state = "вкл" if cfg.enabled else "выкл"
    order_state = "вкл" if cfg.order_enabled else "выкл"
    raise_enabled = await db.get_bool_setting("autoraise_enabled", True)
    last_raise = "—"
    if last_raise_raw:
        last_raise = format_duration(time.time() - float(last_raise_raw)) + " назад"
    next_raise_text = "—"
    if next_raise is not None:
        next_raise_text = f"через {format_duration(next_raise - time.time())}"
    heartbeat_text = "—"
    if heartbeat is not None:
        heartbeat_text = f"{max(0, int(time.time() - heartbeat))} с назад"
    last_runner = format_duration(time.time() - client.last_runner_at) if client.last_runner_at else "—"
    my_categories_raw = await db.get_meta("my_lots_count")
    my_categories = my_categories_raw or "—"

    lines = [
        "🟢 <b>FunPay Helper Bot</b>",
        f"Аптайм: {uptime}",
        f"Сессия FunPay: {session}",
        f"Аккаунт: {html.escape(client.username or '—')}",
        f"Мои категории с лотами: {my_categories}",
        f"Последний runner: {last_runner} назад",
        f"Автоответ: {autoreply_state}, интервал {cfg.interval_hours:g} ч, заказ: {order_state}",
        f"Автоподнятие: {'вкл' if raise_enabled else 'выкл'}",
        f"Последнее поднятие: {last_raise}",
        f"Следующее поднятие: {next_raise_text}",
        f"Heartbeat: {heartbeat_text}",
        f"Watchdog: {int(context.watchdog.seconds_since_tick)} с без тика",
    ]
    return "\n".join(lines)


async def autoreply_menu_text(context: BotContext) -> tuple[str, Any]:
    """Текст и клавиатура меню автоответчика."""
    cfg = await AutoreplyConfig.load(context.db)
    text = (
        "🤖 <b>Автоответчик</b>\n"
        f"Состояние: {'вкл' if cfg.enabled else 'выкл'}\n"
        f"Интервал: {cfg.interval_hours:g} ч\n"
        f"Текст: {html.escape(cfg.text)}\n"
        f"Ответ на заказ: {'вкл' if cfg.order_enabled else 'выкл'}\n"
        f"Текст для заказа: {html.escape(cfg.order_text)}"
    )
    return text, autoreply_menu(cfg.enabled, cfg.interval_hours, cfg.order_enabled)


async def raise_menu_text(context: BotContext) -> tuple[str, Any]:
    """Текст и клавиатура меню поднятия."""
    enabled = await context.db.get_bool_setting("autoraise_enabled", True)
    report = await context.db.get_bool_setting("raise_report_enabled", True)
    next_raise = await context.raiser.next_raise_at()
    next_text = "—"
    if next_raise is not None:
        next_text = f"через {format_duration(next_raise - time.time())}"
    targets = await context.raiser.targets()
    text = (
        "⬆️ <b>Поднятие лотов</b>\n"
        f"Автоподнятие: {'вкл' if enabled else 'выкл'}\n"
        f"Отчёт в TG: {'вкл' if report else 'выкл'}\n"
        f"Мои игры с лотами: {len(targets)}\n"
        f"Следующее поднятие: {next_text}\n"
        "Команды: /raise_list /raise_now /raise_refresh /raise_exclude /raise_include"
    )
    return text, raise_menu(enabled, report)


async def raise_list_text(context: BotContext) -> str:
    """Список категорий, которые реально поднимает бот."""
    targets = await context.raiser.targets()
    states = {
        int(row["category_id"]): row
        for row in await context.db.all_raise_states()
        if str(row["category_id"]).isdigit()
    }
    excludes = await context.db.excludes()
    lines = ["⬆️ <b>Категории для поднятия</b>"]
    if not targets:
        lines.append("Нет категорий с активными лотами.")
    now = time.time()
    for target in targets:
        state = states.get(target.game_id) or {}
        ready_at = state.get("next_raise_at")
        next_text = "—"
        if ready_at:
            next_text = f"через {format_duration(max(0, float(ready_at) - now))}"
        nodes = ", ".join(
            f"{node_id} «{html.escape(name)}»"
            for node_id, name in zip(target.node_ids, target.node_names, strict=True)
        )
        lines.append(
            f"🎮 {html.escape(target.game_name)} (id {target.game_id}), "
            f"лотов: {target.lot_count}\n"
            f"   node_id: {nodes}\n"
            f"   следующее: {next_text}"
        )
    if excludes:
        lines.append(
            "Исключено: "
            + ", ".join(
                f"{item['kind']} {item['target_id']} «{html.escape(str(item['name'] or ''))}»"
                for item in excludes
            )
        )
    return "\n".join(lines)


@router.message(CommandStart())
async def cmd_start(message: types.Message, context: BotContext) -> None:
    """Главное меню."""
    await message.answer(
        "Привет! Я держу FunPay онлайн, уведомляю о покупателях, отвечаю им "
        "и поднимаю лоты. Управление — кнопками ниже.",
        reply_markup=main_menu(),
    )


@router.message(Command("status"))
async def cmd_status(message: types.Message, context: BotContext) -> None:
    """Статус системы."""
    await message.answer(await status_text(context))


@router.message(Command("logs"))
async def cmd_logs(message: types.Message, context: BotContext) -> None:
    """Последние 30 строк логов."""
    tail = read_last_lines(context.settings.log_dir, 30, context.masker)
    await message.answer(f"<pre>{html.escape(tail[:3500])}</pre>")


@router.message(Command("token"))
async def cmd_token(message: types.Message, state: FSMContext) -> None:
    """Запрос нового токена Telegram."""
    await state.set_state(SecretStates.wait_token)
    await message.answer(STATUS_WAIT_TEXT)


@router.message(SecretStates.wait_token)
async def msg_token(message: types.Message, state: FSMContext, context: BotContext) -> None:
    """Принимает и проверяет новый токен, затем перезапускает бота в процессе."""
    await _delete_secret(message)
    raw = (message.text or "").strip()
    try:
        bot_id, bot_username = await check_bot_token(raw)
    except Exception as exc:  # noqa: BLE001 - показываем пользователю любую причину отказа
        await message.answer(f"❌ Токен не принят: {html.escape(str(exc))}")
        await state.clear()
        return
    assert context.runtime is not None  # всегда инициализируется в main.py
    await context.db.set_setting("bot_token", raw)
    context.masker.add(raw)
    await state.clear()
    await context.runtime.switch_token(raw)
    await context.runtime.send_admin(
        f"✅ Токен обновлён. Бот @{html.escape(bot_username)} (id {bot_id}) "
        "перезапущен без остановки сервиса."
    )


@router.message(Command("cookie"))
async def cmd_cookie(message: types.Message, state: FSMContext) -> None:
    """Запрос новых куков FunPay."""
    await state.set_state(SecretStates.wait_cookie)
    await message.answer(COOKIE_WAIT_TEXT)


@router.message(SecretStates.wait_cookie)
async def msg_cookie(message: types.Message, state: FSMContext, context: BotContext) -> None:
    """Проверяет новые куки запросом к FunPay и сохраняет их при успехе."""
    await _delete_secret(message)
    raw = message.text or ""
    try:
        golden_key, phpsessid = parse_cookie_input(raw)
    except ValueError as exc:
        await message.answer(f"❌ Не понял куки: {html.escape(str(exc))}")
        await state.clear()
        return
    try:
        info = await context.client.update_cookies(golden_key, phpsessid)
    except UnauthorizedError:
        await message.answer("❌ Не удалось войти: куки недействительны. Проверьте golden_key и PHPSESSID.")
    except FunPayError as exc:
        await message.answer(f"❌ Не удалось проверить куки: {html.escape(str(exc))}")
    else:
        context.alerts.reset("cookies_invalid")
        await message.answer(
            f"✅ Куки приняты. Аккаунт FunPay: <b>{html.escape(info.username)}</b> (id {info.user_id}), "
            f"категорий: {len(info.categories)}. Работа возобновлена."
        )
    finally:
        await state.clear()


@router.message(Command("autoreply"))
async def cmd_autoreply(message: types.Message, context: BotContext) -> None:
    """Меню автоответчика."""
    text, markup = await autoreply_menu_text(context)
    await message.answer(text, reply_markup=markup)


@router.message(Command("raise"))
async def cmd_raise(message: types.Message, context: BotContext) -> None:
    """Меню поднятия лотов."""
    text, markup = await raise_menu_text(context)
    await message.answer(text, reply_markup=markup)


@router.message(Command("raise_list"))
async def cmd_raise_list(message: types.Message, context: BotContext) -> None:
    """Показывает категории, которые поднимает бот."""
    await message.answer(await raise_list_text(context))


@router.message(Command("raise_now"))
async def cmd_raise_now(message: types.Message, context: BotContext) -> None:
    """Поднимает категории, у которых истёк кулдаун."""
    await message.answer("Запускаю поднятие, отчёт придёт одним сообщением.")
    await context.raiser.raise_all(report=True)


@router.message(Command("raise_refresh"))
async def cmd_raise_refresh(message: types.Message, context: BotContext) -> None:
    """Принудительно обновляет список моих лотов."""
    try:
        categories = await context.raiser.refresh_lots(notify=False)
    except UnauthorizedError:
        await message.answer("❌ Куки недействительны — пришлите новые через /cookie.")
        return
    except FunPayError as exc:
        await message.answer(f"❌ Не удалось обновить лоты: {html.escape(str(exc))}")
        return
    games = len({item.game_id for item in categories})
    await message.answer(
        f"✅ Лоты обновлены: {games} игр, {len(categories)} подкатегорий, "
        f"{sum(item.lot_count for item in categories)} лотов."
    )


@router.message(Command("raise_exclude"))
async def cmd_raise_exclude(
    message: types.Message, command: CommandObject, state: FSMContext, context: BotContext
) -> None:
    """Исключает категорию из поднятия: /raise_exclude 333 или n1350."""
    raw = (command.args or "").strip()
    if not raw:
        await state.set_state(SettingsStates.wait_raise_exclude_id)
        await message.answer("Пришлите ID из /raise_list: «333» (игра) или «n1350» (подкатегория).")
        return
    await message.answer(await context.raiser.exclude(raw))


@router.message(Command("raise_include"))
async def cmd_raise_include(
    message: types.Message, command: CommandObject, state: FSMContext, context: BotContext
) -> None:
    """Возвращает категорию в поднятие."""
    raw = (command.args or "").strip()
    if not raw:
        await state.set_state(SettingsStates.wait_raise_include_id)
        await message.answer("Пришлите ID из /raise_list: «333» или «n1350».")
        return
    await message.answer(await context.raiser.include(raw))


@router.message(SettingsStates.wait_raise_exclude_id)
async def msg_raise_exclude(message: types.Message, state: FSMContext, context: BotContext) -> None:
    """Обрабатывает ID, присланный после /raise_exclude без аргумента."""
    await state.clear()
    await message.answer(await context.raiser.exclude(message.text or ""))


@router.message(SettingsStates.wait_raise_include_id)
async def msg_raise_include(message: types.Message, state: FSMContext, context: BotContext) -> None:
    """Обрабатывает ID, присланный после /raise_include без аргумента."""
    await state.clear()
    await message.answer(await context.raiser.include(message.text or ""))


@router.message(SettingsStates.wait_autoreply_text)
async def msg_autoreply_text(message: types.Message, state: FSMContext, context: BotContext) -> None:
    """Сохраняет новый текст автоответа."""
    text = message.text or ""
    if not text.strip():
        await message.answer("Текст не может быть пустым.")
        return
    await context.db.set_setting("autoreply_text", text)
    await state.clear()
    menu_text, markup = await autoreply_menu_text(context)
    await message.answer("✅ Текст автоответа обновлён.\n" + menu_text, reply_markup=markup)


@router.message(SettingsStates.wait_autoreply_interval)
async def msg_autoreply_interval(message: types.Message, state: FSMContext, context: BotContext) -> None:
    """Сохраняет интервал кулдауна автоответа."""
    try:
        hours = float((message.text or "").replace(",", "."))
        if not 0 < hours <= 720:
            raise ValueError
    except ValueError:
        await message.answer("Пришлите число часов от 0.01 до 720 (например, 6).")
        return
    await context.db.set_setting("autoreply_interval_hours", str(hours))
    await state.clear()
    menu_text, markup = await autoreply_menu_text(context)
    await message.answer(f"✅ Интервал автоответа: {hours:g} ч.\n" + menu_text, reply_markup=markup)


@router.message(SettingsStates.wait_order_text)
async def msg_order_text(message: types.Message, state: FSMContext, context: BotContext) -> None:
    """Сохраняет текст автоответа на заказ."""
    text = message.text or ""
    if not text.strip():
        await message.answer("Текст не может быть пустым.")
        return
    await context.db.set_setting("autoreply_order_text", text)
    await state.clear()
    menu_text, markup = await autoreply_menu_text(context)
    await message.answer("✅ Текст для заказа обновлён.\n" + menu_text, reply_markup=markup)
