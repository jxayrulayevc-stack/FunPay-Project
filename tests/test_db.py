"""Тесты SQLite-хранилища."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from core.db import Database


async def test_settings_and_defaults(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    assert await db.get_bool_setting("autoreply_enabled") is True
    assert await db.get_float_setting("autoreply_interval_hours", 6) == 6.0
    await db.set_setting("autoreply_enabled", "0")
    assert await db.get_bool_setting("autoreply_enabled") is False
    await db.close()


async def test_secret_from_db_has_priority(monkeypatch: object, tmp_path: Path) -> None:
    monkeypatch.setenv("BOT_TOKEN", "from-env-token")  # type: ignore[attr-defined]
    db = Database(tmp_path / "bot.db")
    await db.connect()
    assert await db.get_secret("bot_token", "BOT_TOKEN") == "from-env-token"
    await db.set_setting("bot_token", "from-db-token")
    assert await db.get_secret("bot_token", "BOT_TOKEN") == "from-db-token"
    await db.close()


async def test_message_deduplication(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    assert await db.mark_message_processed(123, "users-1-2", 2, "BUYER", "hi") is True
    assert await db.mark_message_processed(123, "users-1-2", 2, "BUYER", "hi") is False
    assert await db.mark_message_processed(124, "users-1-2", 2, "BUYER", "hello") is True
    await db.close()


async def test_order_status_change_detected(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    assert await db.upsert_order("ABC", "paid", "Лот", "100", "BUYER", "users-1-2") is True
    assert await db.upsert_order("ABC", "paid", "Лот", "100", "BUYER", "users-1-2") is False
    assert await db.upsert_order("ABC", "closed", "Лот", "100", "BUYER", "users-1-2") is True
    await db.close()


async def test_heartbeat_and_raise_state(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    now = time.time()
    await db.set_heartbeat(now)
    assert await db.get_heartbeat() == now
    await db.set_raise_state(1, "Game", now, now + 14400, None, 0)
    assert await db.get_next_raise_at() == now + 14400
    assert (await db.get_raise_state(1))["name"] == "Game"
    await db.close()


async def test_migration_keeps_secrets_and_settings(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    old.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    old.execute(
        "CREATE TABLE raise_state (category_id TEXT PRIMARY KEY, name TEXT, "
        "last_raise_at REAL, next_raise_at REAL, last_error TEXT, "
        "error_count INTEGER NOT NULL DEFAULT 0)"
    )
    old.execute("INSERT INTO settings(key, value) VALUES ('golden_key', 'demo-cookie')")
    old.execute(
        "INSERT INTO raise_state(category_id, name, next_raise_at, error_count) "
        "VALUES ('163', 'Drakensang Online', 123.0, 4)"
    )
    old.commit()
    old.close()

    db = Database(path)
    await db.connect()
    assert await db.get_setting("golden_key") == "demo-cookie"
    old_state = await db.get_raise_state(163)
    assert old_state is not None and old_state["node_ids"] == ""
    await db.set_raise_state(333, "Counter-Strike 2", None, 200.0, None, 0, (1350,))
    new_state = await db.get_raise_state(333)
    assert new_state is not None and new_state["node_ids"] == "1350"
    await db.close()
