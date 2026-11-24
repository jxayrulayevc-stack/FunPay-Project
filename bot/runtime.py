"""Управление жизненным циклом Telegram-бота.

Смена токена через /token пересобирает Bot и Dispatcher внутри того же
процесса: systemd не перезапускается, FunPay-циклы продолжают работать.
"""

from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.context import BotContext
from bot.middlewares import AdminOnlyMiddleware, ContextMiddleware


class TelegramRuntime:
    """Владеет текущим Bot/Dispatcher и очередью отложенных уведомлений."""

    def __init__(self, context: BotContext, token: str) -> None:
        self._context = context
        self._token = token
        self._bot: Bot | None = None
        self._dispatcher: Dispatcher | None = None
        self._polling_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._pending: list[str] = []
        self._logger = context.logger

    @property
    def token(self) -> str:
        return self._token

    def _build_dispatcher(self) -> Dispatcher:
        dispatcher = Dispatcher(storage=MemoryStorage())
        dispatcher.message.middleware(AdminOnlyMiddleware(self._context.settings.admin_id))
        dispatcher.callback_query.middleware(AdminOnlyMiddleware(self._context.settings.admin_id))
        dispatcher.message.middleware(ContextMiddleware(self._context))
        dispatcher.callback_query.middleware(ContextMiddleware(self._context))
        from bot.handlers import register_handlers

        register_handlers(dispatcher, self._context)
        return dispatcher

    async def start(self) -> None:
        """Запускает polling с текущим токеном."""
        async with self._lock:
            await self._start_locked(self._token)

    async def _start_locked(self, token: str) -> None:
        self._bot = Bot(
            token=token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=False),
        )
        self._dispatcher = self._build_dispatcher()
        self._polling_task = asyncio.create_task(
            self._dispatcher.start_polling(
                self._bot,
                handle_signals=False,
                allowed_updates=["message", "callback_query"],
            ),
            name="telegram-polling",
        )
        self._token = token
        self._logger.info("Telegram-бот запущен (polling)")

    async def switch_token(self, new_token: str) -> None:
        """Планирует смену токена вне текущего polling-таска.

        Метод вызывается изнутри хендлера, поэтому отмена старого polling
        выполняется отдельной задачей: иначе мы отменили бы собственный таск
        и пользователь не получил бы ответ на команду /token.
        """
        asyncio.create_task(self._deferred_switch(new_token), name="telegram-token-switch")

    async def _deferred_switch(self, new_token: str) -> None:
        """Даёт хендлеру время отправить ответ, затем меняет бот."""
        await asyncio.sleep(0.3)
        await self._apply_token(new_token)

    async def _apply_token(self, new_token: str) -> None:
        """Останавливает старый polling и запускает бота с новым токеном."""
        async with self._lock:
            self._logger.info("Смена токена Telegram: перезапуск polling без рестарта процесса")
            task = self._polling_task
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            old_bot = self._bot
            if old_bot is not None:
                try:
                    await old_bot.session.close()
                except Exception as exc:  # noqa: BLE001 - закрытие сессии не критично
                    self._logger.debug("Не удалось закрыть старую сессию Telegram: %s", exc)
            await self._start_locked(new_token)
            await self._flush_pending()

    async def stop(self) -> None:
        """Корректно останавливает polling и закрывает сессию."""
        async with self._lock:
            task = self._polling_task
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self._polling_task = None
            if self._bot is not None:
                await self._bot.session.close()
                self._bot = None
            self._logger.info("Telegram-бот остановлен")

    async def send_admin(self, text: str) -> bool:
        """Отправляет сообщение админу; если бота временно нет — откладывает."""
        for _ in range(3):
            bot = self._bot
            if bot is not None:
                try:
                    await bot.send_message(chat_id=self._context.settings.admin_id, text=text)
                    return True
                except Exception as exc:  # noqa: BLE001 - Telegram не должен ломать цикл
                    self._logger.error("Ошибка отправки в Telegram: %s", exc)
                    break
            await asyncio.sleep(0.2)
        if len(self._pending) < 50:
            self._pending.append(text)
        return False

    async def _flush_pending(self) -> None:
        """Отправляет сообщения, накопленные во время смены токена."""
        pending, self._pending = self._pending, []
        for text in pending:
            await self.send_admin(text)
