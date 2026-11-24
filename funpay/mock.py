"""Демо-клиент FunPay для локального тестирования без реальных действий.

Включается через `python main.py --dry-run` или DRY_RUN=1. Генерирует
фальшивые сообщения и заказы, поэтому можно проверить уведомления, автоответ
и отчёты, не касаясь реального аккаунта.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from funpay.models import AccountInfo, Category, ChatMessage, Order, RaiseResult
from funpay.parsing import random_tag

DEMO_NODE = "users-1-999"
DEMO_USER_ID = 999
DEFAULT_WAIT_SECONDS = 4 * 3600


class MockFunPayClient:
    """Имитирует интерфейс FunPayClient в режиме dry-run."""

    is_mock = True

    def __init__(self, logger: Any) -> None:
        self._logger = logger
        self._poll_count = 0
        self._message_id = 100
        self._order_status = "paid"
        self.user_id = 1
        self.username = "DEMO_SELLER"
        self.csrf_token = "demo-csrf"
        self.locale = "ru"
        self.categories: tuple[Category, ...] = (Category(1, "Демо-игра (dry-run)", (101, 102)),)
        self.unauthorized = False
        self._last_success_at = time.time()
        self._last_runner_at = time.time()
        self._consecutive_failures = 0

    @property
    def last_success_at(self) -> float:
        return self._last_success_at

    @property
    def last_runner_at(self) -> float:
        return self._last_runner_at

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def has_cookies(self) -> bool:
        return True

    async def start(self) -> None:
        self._logger.info("DRY-RUN: демо-клиент FunPay запущен")

    async def close(self) -> None:
        self._logger.info("DRY-RUN: демо-клиент FunPay остановлен")

    async def refresh_session(self) -> AccountInfo:
        self._last_success_at = time.time()
        return AccountInfo(self.user_id, self.username, self.csrf_token, self.locale, self.categories)

    async def wait_authorized(self, stop_event: asyncio.Event) -> bool:
        return stop_event.is_set()

    async def update_cookies(self, golden_key: str, phpsessid: str | None = None) -> AccountInfo:
        self._logger.info("DRY-RUN: куки не проверяются, сохранены как демо-значения")
        return await self.refresh_session()

    async def get_updates(
        self, msg_tag: str, order_tag: str, chats_objects: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        self._poll_count += 1
        self._last_runner_at = time.time()
        objects: list[dict[str, Any]] = []
        if self._poll_count % 3 == 2:
            self._message_id += 1
            html = (
                f'<a class="contact-item" data-id="{DEMO_NODE}" '
                f'data-node-msg="{self._message_id}">'
                f'<div class="contact-item-message">Здравствуйте! Это тест 🙏</div>'
                f'<div class="media-user-name">DEMO_BUYER</div></a>'
            )
            objects.append({"type": "chat_bookmarks", "tag": random_tag(), "data": {"html": html}})
        if self._poll_count % 6 == 4:
            self._order_status = "closed" if self._order_status == "paid" else "paid"
            objects.append(
                {
                    "type": "orders_counters",
                    "tag": random_tag(),
                    "data": {"buyer": 1, "seller": 3},
                }
            )
        return objects

    async def get_chat_history(self, node: str, last_message_id: int = -1) -> list[ChatMessage]:
        message_id = max(last_message_id + 1, self._message_id)
        return [
            ChatMessage(
                id=message_id,
                chat_id=DEMO_NODE,
                author_id=DEMO_USER_ID,
                author_name="DEMO_BUYER",
                text="Здравствуйте! Это тестовое сообщение из dry-run режима 🙏",
                is_system=False,
                is_self=False,
            )
        ]

    async def send_message(self, node: str, text: str) -> int | None:
        self._logger.info("DRY-RUN: сообщение в чат %s НЕ отправлено: %s", node, text[:80])
        return self._message_id + 1

    async def get_sales(self) -> list[Order]:
        return [
            Order(
                id="DEMO1234",
                title="Демо-лот (dry-run)",
                amount="100",
                currency="₽",
                buyer="DEMO_BUYER",
                buyer_id=DEMO_USER_ID,
                status=self._order_status,
                chat_id=DEMO_NODE,
            )
        ]

    async def raise_category(self, category: Category) -> RaiseResult:
        now = time.time()
        self._logger.info("DRY-RUN: поднятие категории %s НЕ выполнено", category.id)
        return RaiseResult(
            category_id=category.id,
            ok=True,
            wait_seconds=DEFAULT_WAIT_SECONDS,
            next_attempt_at=now + DEFAULT_WAIT_SECONDS,
            message="Демо-поднятие (dry-run)",
        )


__all__ = ["MockFunPayClient"]
