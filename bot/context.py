"""Общий контекст приложения, доступный обработчикам Telegram-бота."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.runtime import TelegramRuntime
    from core.alerts import AlertService
    from core.config import Settings
    from core.db import Database
    from core.logger import SecretMasker
    from core.notifier import Notifier
    from core.watchdog import Watchdog
    from funpay.autoreply import AutoreplyService
    from funpay.client import FunPayClient
    from funpay.models import FunPayEvent
    from funpay.raiser import AutoRaiser


@dataclass
class BotContext:
    """Зависимости, которые нужны командам бота."""

    settings: Settings
    db: Database
    client: FunPayClient
    notifier: Notifier
    alerts: AlertService
    autoreply: AutoreplyService
    raiser: AutoRaiser
    watchdog: Watchdog
    masker: SecretMasker
    logger: logging.Logger
    events: asyncio.Queue[FunPayEvent]
    runtime: TelegramRuntime | None = None
    started_at: float = field(default_factory=time.time)
