"""Точка входа FunPay Helper Bot.

Запускает Telegram-бота, слушатель FunPay, обработчик событий, автоответ,
автоподнятие и мониторинг. Остановка по SIGTERM — корректная (graceful).
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

from bot.context import BotContext
from bot.runtime import TelegramRuntime
from core.alerts import AlertService
from core.config import Settings
from core.db import Database
from core.logger import SecretMasker, setup_logging
from core.notifier import Notifier
from core.watchdog import Heartbeat, Watchdog
from funpay.autoreply import AutoreplyService
from funpay.client import FunPayClient
from funpay.listener import EventListener
from funpay.mock import MockFunPayClient
from funpay.models import FunPayEvent, MyLotCategory
from funpay.processor import EventProcessor
from funpay.raiser import AutoRaiser


def parse_args() -> argparse.Namespace:
    """Аргументы командной строки."""
    parser = argparse.ArgumentParser(description="FunPay Helper Bot")
    parser.add_argument("--dry-run", action="store_true", help="демо-режим без действий на FunPay")
    parser.add_argument(
        "--live",
        action="store_true",
        help="вместе с --dry-run: прочитать реальный профиль и показать категории без POST",
    )
    parser.add_argument("--env", type=Path, default=None, help="путь к файлу .env")
    return parser.parse_args()


async def inspect_lots(settings: Settings) -> None:
    """Показывает реальные категории с лотами, не отправляя запросов на поднятие."""
    masker = SecretMasker()
    log = setup_logging(settings.log_dir, settings.log_level, masker)
    db = Database(settings.db_path)
    await db.connect()
    client = FunPayClient(settings, db, masker, log)
    try:
        await client.start()
        if client.unauthorized:
            raise SystemExit("Куки FunPay недействительны — пришлите новые через /cookie")
        categories = await client.get_my_lot_categories()
        grouped: dict[tuple[int, str], list[MyLotCategory]] = {}
        for item in categories:
            grouped.setdefault((item.game_id, item.game_name), []).append(item)
        print("DRY-RUN (live): категории для поднятия, POST не отправлялись:")
        if not grouped:
            print("  Нет активных лотов для поднятия")
        for (game_id, game_name), items in grouped.items():
            nodes = ", ".join(f"{item.node_id} «{item.node_name}» ({item.lot_count} лотов)" for item in items)
            print(f"  {game_id} {game_name}: {nodes}")
        print(
            f"Итого: {len(grouped)} игр, {len(categories)} подкатегорий, "
            f"{sum(item.lot_count for item in categories)} лотов"
        )
    finally:
        await client.close()
        await db.close()


async def run(settings: Settings) -> None:
    """Собирает и запускает все подсистемы."""
    masker = SecretMasker()
    log = setup_logging(settings.log_dir, settings.log_level, masker)
    log.info("Запуск FunPay Helper Bot (dry_run=%s, debug=%s)", settings.dry_run, settings.debug)

    db = Database(settings.db_path)
    await db.connect()

    token = await db.get_setting("bot_token") or settings.bot_token
    if not token:
        log.error("Нет токена Telegram: задайте BOT_TOKEN в .env или сохраните через /token")
        await db.close()
        raise SystemExit(1)
    masker.add(token)

    notifier = Notifier(logger=log)
    alerts = AlertService(notifier, log)
    watchdog = Watchdog(settings.watchdog_timeout, log, notifier)

    client: FunPayClient | MockFunPayClient
    if settings.dry_run:
        client = MockFunPayClient(log)
    else:
        client = FunPayClient(settings, db, masker, log)
    await client.start()

    autoreply = AutoreplyService(db, client, alerts, log)  # type: ignore[arg-type]
    raiser = AutoRaiser(db, client, notifier, alerts, settings, log, watchdog, dry_run=settings.dry_run)
    events: asyncio.Queue[FunPayEvent] = asyncio.Queue(maxsize=500)

    context = BotContext(
        settings=settings,
        db=db,
        client=client,  # type: ignore[arg-type]
        notifier=notifier,
        alerts=alerts,
        autoreply=autoreply,
        raiser=raiser,
        watchdog=watchdog,
        masker=masker,
        logger=log,
        events=events,
    )
    runtime = TelegramRuntime(context, token)
    notifier.attach(runtime)
    context.runtime = runtime

    listener = EventListener(client, events, alerts, settings, log, watchdog)  # type: ignore[arg-type]
    processor = EventProcessor(db, client, notifier, alerts, autoreply, log)  # type: ignore[arg-type]
    heartbeat = Heartbeat(db, watchdog, log, settings.heartbeat_interval)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    await runtime.start()
    await notifier.send(
        "🚀 <b>FunPay Helper Bot запущен</b>\n"
        f"Режим: {'демо (dry-run)' if settings.dry_run else 'рабочий'}\n"
        "При проблемах смотрите /logs."
    )

    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(listener.run(stop_event), name="funpay-listener"),
        asyncio.create_task(processor.run(events, stop_event), name="event-processor"),
        asyncio.create_task(raiser.run(stop_event), name="auto-raiser"),
        asyncio.create_task(heartbeat.run(stop_event), name="heartbeat"),
        asyncio.create_task(watchdog.run(stop_event), name="watchdog"),
    ]

    try:
        await stop_event.wait()
    finally:
        log.info("Остановка: отменяем фоновые задачи")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runtime.stop()
        await client.close()
        await db.close()
        log.info("Корректная остановка завершена")


def main() -> None:
    """Точка входа для systemd и локального запуска."""
    args = parse_args()
    try:
        settings = Settings.load(args.env, dry_run=args.dry_run)
        if args.live:
            if not args.dry_run:
                raise ValueError("--live используется только вместе с --dry-run")
            asyncio.run(inspect_lots(settings))
            return
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        pass
    except (ValueError, SystemExit) as exc:
        print(f"Ошибка запуска: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
