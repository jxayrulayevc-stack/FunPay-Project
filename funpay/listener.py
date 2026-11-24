"""Long-polling слушатель событий FunPay (эндпоинт runner/)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from core.alerts import AlertService
from core.watchdog import wait_or_stop
from funpay.errors import FunPayError, NetworkError, RateLimitError, UnauthorizedError
from funpay.models import ChatUpdateEvent, FunPayEvent, OrdersChangedEvent
from funpay.parsing import parse_chat_contacts, random_tag

if TYPE_CHECKING:
    from core.config import Settings
    from core.watchdog import Watchdog
    from funpay.client import FunPayClient


class EventListener:
    """Опрашивает FunPay и складывает события в общую очередь."""

    def __init__(
        self,
        client: FunPayClient,
        events: asyncio.Queue[FunPayEvent],
        alerts: AlertService,
        settings: Settings,
        logger: logging.Logger,
        watchdog: Watchdog,
    ) -> None:
        self._client = client
        self._events = events
        self._alerts = alerts
        self._settings = settings
        self._logger = logger
        self._watchdog = watchdog
        self._last_message_ids: dict[str, int] = {}
        self._down_since: float | None = None
        self._was_down_long = False

    async def run(self, stop_event: asyncio.Event) -> None:
        """Бесконечный цикл опроса с backoff и без падения при ошибках."""
        msg_tag = random_tag()
        order_tag = random_tag()
        first_request = True
        next_refresh = time.monotonic() + self._settings.account_refresh_minutes * 60
        backoff = 2.0

        while not stop_event.is_set():
            try:
                if await self._client.wait_authorized(stop_event):
                    return
                if time.monotonic() >= next_refresh:
                    await self._client.refresh_session()
                    next_refresh = time.monotonic() + self._settings.account_refresh_minutes * 60

                objects = await self._client.get_updates(msg_tag, order_tag)
                for obj in objects:
                    tag = obj.get("tag")
                    if not tag:
                        continue
                    if obj.get("type") == "chat_bookmarks":
                        msg_tag = str(tag)
                    elif obj.get("type") == "orders_counters":
                        order_tag = str(tag)

                if first_request:
                    # Первый ответ long-polling — снимок состояния, а не новые события.
                    self._seed_contacts(objects)
                else:
                    await self._dispatch(objects)

                first_request = False
                self._watchdog.tick()
                self._mark_recovered()
                backoff = 2.0
                if await wait_or_stop(stop_event, self._settings.poll_interval):
                    return
            except UnauthorizedError:
                await self._alerts.send(
                    "cookies_invalid",
                    "🔑 Куки недействительны, пришли новые через /cookie. "
                    "Все действия на FunPay временно остановлены.",
                )
                self._logger.error("Куки протухли: действия на FunPay остановлены")
                backoff = 30.0
                if await wait_or_stop(stop_event, backoff):
                    return
            except (NetworkError, RateLimitError, FunPayError) as exc:
                await self._handle_failure(exc, backoff)
                backoff = min(backoff * 2, 60.0)
                if await wait_or_stop(stop_event, backoff):
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - цикл обязан выжить при любом баге
                self._logger.exception("Неожиданная ошибка слушателя FunPay: %s", exc)
                if await wait_or_stop(stop_event, min(max(backoff, 5.0), 30.0)):
                    return

    def _seed_contacts(self, objects: list[dict[str, Any]]) -> None:
        """Запоминает текущие ID сообщений, чтобы не уведомлять о старых."""
        for obj in objects:
            if obj.get("type") != "chat_bookmarks":
                continue
            for contact in parse_chat_contacts(obj):
                self._last_message_ids[contact.chat_id] = contact.last_message_id

    async def _dispatch(self, objects: list[dict[str, Any]]) -> None:
        """Превращает объекты runner в события шины."""
        for obj in objects:
            kind = obj.get("type")
            if kind == "chat_bookmarks":
                for contact in parse_chat_contacts(obj):
                    previous = self._last_message_ids.get(contact.chat_id, -1)
                    if contact.last_message_id <= previous:
                        continue
                    messages = await self._client.get_chat_history(contact.chat_id, last_message_id=previous)
                    fresh = tuple(msg for msg in messages if msg.id > previous)
                    if fresh:
                        self._last_message_ids[contact.chat_id] = max(msg.id for msg in fresh)
                        await self._events.put(ChatUpdateEvent(fresh))
            elif kind == "orders_counters":
                await self._events.put(OrdersChangedEvent())

    async def _handle_failure(self, exc: Exception, backoff: float) -> None:
        """Уведомляет о долгом недоступе FunPay и логирует ошибку."""
        now = time.time()
        if self._down_since is None:
            self._down_since = now
        self._logger.error("Ошибка опроса FunPay (%s): %s. Пауза %s с", type(exc).__name__, exc, backoff)
        if now - self._down_since >= self._settings.funpay_unavailable_after:
            self._was_down_long = True
            await self._alerts.send(
                "funpay_unavailable",
                "⚠️ FunPay недоступен дольше 5 минут. Проверьте сеть и состояние сайта.",
            )

    def _mark_recovered(self) -> None:
        """Сбрасывает счётчик простоя после успешного опроса."""
        if self._was_down_long:
            self._alerts.reset("funpay_unavailable")
        self._down_since = None
        self._was_down_long = False
