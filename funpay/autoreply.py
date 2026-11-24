"""Автоответчик с пер-чат кулдауном и случайной задержкой."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.alerts import AlertService
from funpay.errors import UnauthorizedError

if TYPE_CHECKING:
    from core.db import Database
    from funpay.client import FunPayClient


@dataclass(frozen=True, slots=True)
class AutoreplyConfig:
    """Настройки автоответчика, читаемые из SQLite."""

    enabled: bool
    text: str
    interval_hours: float
    order_enabled: bool
    order_text: str
    delay_min: float
    delay_max: float

    @classmethod
    async def load(cls, db: Database) -> AutoreplyConfig:
        """Загружает настройки, подставляя значения по умолчанию."""
        return cls(
            enabled=await db.get_bool_setting("autoreply_enabled", True),
            text=await db.get_setting(
                "autoreply_text",
                "Здравствуйте! Сейчас я немного занят, отвечу в ближайшее время. Ожидайте, пожалуйста 🙏",
            )
            or "",
            interval_hours=await db.get_float_setting("autoreply_interval_hours", 6.0),
            order_enabled=await db.get_bool_setting("autoreply_order_enabled", False),
            order_text=await db.get_setting("autoreply_order_text", "Спасибо за заказ! Скоро выдам") or "",
            delay_min=await db.get_float_setting("autoreply_delay_min", 3.0),
            delay_max=await db.get_float_setting("autoreply_delay_max", 8.0),
        )


def cooldown_passed(last_reply_at: float | None, now: float, interval_hours: float) -> bool:
    """True, если по чату можно отправить автоответ (или ответ ещё не отправлялся)."""
    if last_reply_at is None:
        return True
    return now - last_reply_at >= max(0.0, interval_hours) * 3600


class AutoreplyService:
    """Планирует и отправляет автоответы, не допуская спама в чате."""

    def __init__(
        self,
        db: Database,
        client: FunPayClient,
        alerts: AlertService,
        logger: logging.Logger,
    ) -> None:
        self._db = db
        self._client = client
        self._alerts = alerts
        self._logger = logger
        self._pending: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    async def maybe_reply(self, chat_id: str, username: str | None, *, order: bool = False) -> bool:
        """Планирует ответ, если он включён и кулдаун чата истёк."""
        config = await AutoreplyConfig.load(self._db)
        if not config.enabled or (order and not config.order_enabled):
            return False
        if chat_id in self._pending:
            return False
        last_reply = await self._db.get_last_reply_at(chat_id)
        if not cooldown_passed(last_reply, time.time(), config.interval_hours):
            return False

        self._pending.add(chat_id)
        task = asyncio.create_task(
            self._delayed_reply(chat_id, username, config, order), name=f"autoreply:{chat_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _delayed_reply(
        self, chat_id: str, username: str | None, config: AutoreplyConfig, order: bool
    ) -> None:
        """Ждёт 3–8 секунд, повторно проверяет кулдаун и отправляет сообщение."""
        try:
            delay = random.uniform(
                max(0.0, config.delay_min), max(max(0.0, config.delay_min), config.delay_max)
            )
            await asyncio.sleep(delay)
            last_reply = await self._db.get_last_reply_at(chat_id)
            if not cooldown_passed(last_reply, time.time(), config.interval_hours):
                self._logger.debug("Автоответ пропущен: кулдаун чата %s ещё не истёк", chat_id)
                return
            text = config.order_text if order else config.text
            if not text.strip():
                return
            await self._client.send_message(chat_id, text)
            await self._db.set_last_reply_at(chat_id, time.time(), username)
            self._logger.info("Автоответ отправлен в чат %s (заказ=%s)", chat_id, order)
        except UnauthorizedError:
            await self._alerts.send(
                "cookies_invalid",
                "🔑 Куки недействительны, пришли новые через /cookie. Автоответ не отправлен.",
            )
        except Exception as exc:  # noqa: BLE001 - автоответ не должен ронять процессор
            self._logger.error("Не удалось отправить автоответ в чат %s: %s", chat_id, exc)
        finally:
            self._pending.discard(chat_id)
