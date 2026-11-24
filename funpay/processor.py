"""Обработчик событий: дедупликация, уведомления, автоответы."""

from __future__ import annotations

import asyncio
import html
import logging
from typing import TYPE_CHECKING

from core.alerts import AlertService
from core.notifier import Notifier
from funpay.errors import UnauthorizedError
from funpay.models import AlertEvent, ChatUpdateEvent, FunPayEvent, OrdersChangedEvent

if TYPE_CHECKING:
    from core.db import Database
    from funpay.autoreply import AutoreplyService
    from funpay.client import FunPayClient


STATUS_LABELS = {
    "paid": "💳 Оплачен",
    "closed": "✅ Подтверждён",
    "refunded": "↩️ Возврат средств",
}


def chat_url(node: str) -> str:
    """Ссылка на чат FunPay."""
    from urllib.parse import quote

    return f"https://funpay.com/chat/?node={quote(str(node), safe='-')}"


class EventProcessor:
    """Потребляет очередь событий FunPay и выполняет бизнес-логику."""

    def __init__(
        self,
        db: Database,
        client: FunPayClient,
        notifier: Notifier,
        alerts: AlertService,
        autoreply: AutoreplyService,
        logger: logging.Logger,
    ) -> None:
        self._db = db
        self._client = client
        self._notifier = notifier
        self._alerts = alerts
        self._autoreply = autoreply
        self._logger = logger
        self._orders_seeded = False

    async def run(self, events: asyncio.Queue[FunPayEvent], stop_event: asyncio.Event) -> None:
        """Цикл обработки очереди; ошибки одного события не останавливают остальные."""
        while not stop_event.is_set():
            try:
                event = await asyncio.wait_for(events.get(), timeout=1.0)
            except TimeoutError:
                continue
            try:
                await self.handle(event)
            except UnauthorizedError:
                await self._alerts.send(
                    "cookies_invalid", "🔑 Куки недействительны, пришли новые через /cookie."
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - сохраняем работоспособность цикла
                self._logger.exception("Ошибка обработки события FunPay: %s", exc)
            finally:
                events.task_done()

    async def handle(self, event: FunPayEvent) -> None:
        """Маршрутизирует событие в нужный обработчик."""
        if isinstance(event, ChatUpdateEvent):
            await self._handle_chat(event)
        elif isinstance(event, OrdersChangedEvent):
            await self._handle_orders()
        elif isinstance(event, AlertEvent):
            await self._alerts.send(event.kind, event.text)

    async def _handle_chat(self, event: ChatUpdateEvent) -> None:
        for message in event.messages:
            first_time = await self._db.mark_message_processed(
                message.id,
                message.chat_id,
                message.author_id,
                message.author_name,
                message.text,
            )
            if not first_time:
                continue
            if message.is_system or message.is_self:
                continue  # системные и свои сообщения уведомлений не вызывают

            nickname = html.escape(message.author_name or "Покупатель")
            text = html.escape(message.text or "(сообщение без текста)")
            await self._notifier.send(
                f"💬 <b>Новое сообщение</b> от {nickname}\n{text}\n"
                f'<a href="{chat_url(message.chat_id)}">Открыть чат FunPay</a>'
            )
            await self._autoreply.maybe_reply(message.chat_id, message.author_name)

    async def _handle_orders(self) -> None:
        orders = await self._client.get_sales()
        if not self._orders_seeded and await self._db.orders_count() == 0:
            for order in orders:
                await self._db.upsert_order(
                    order.id, order.status, order.title, order.amount, order.buyer, order.chat_id
                )
            self._orders_seeded = True
            self._logger.info("Первичная загрузка заказов: %s шт. (без уведомлений)", len(orders))
            return

        for order in orders:
            changed = await self._db.upsert_order(
                order.id, order.status, order.title, order.amount, order.buyer, order.chat_id
            )
            if not changed:
                continue
            label = STATUS_LABELS.get(order.status, order.status)
            await self._notifier.send(
                f"{label} <b>#{html.escape(order.id)}</b> — {html.escape(order.buyer)}\n"
                f"Лот: {html.escape(order.title)} • {html.escape(order.amount)} "
                f"{html.escape(order.currency)}\n"
                f'<a href="{chat_url(order.chat_id)}">Чат с покупателем</a>'
            )
            if order.status == "paid":
                await self._autoreply.maybe_reply(order.chat_id, order.buyer, order=True)
