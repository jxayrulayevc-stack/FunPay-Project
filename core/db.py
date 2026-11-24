"""Асинхронное хранилище SQLite (aiosqlite).

SQLite работает в режиме WAL: healthcheck и backup читают ту же БД из других процессов.
Все записи сериализуются локальным asyncio.Lock, поэтому конкуренция внутри процесса исключена.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS processed_messages (
    message_id  TEXT PRIMARY KEY,
    chat_id     TEXT NOT NULL,
    author_id   INTEGER,
    author_name TEXT,
    text        TEXT,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON processed_messages(chat_id);
CREATE TABLE IF NOT EXISTS chat_state (
    chat_id        TEXT PRIMARY KEY,
    username       TEXT,
    user_id        INTEGER,
    last_reply_at  REAL,
    last_message_id INTEGER,
    updated_at     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id         TEXT PRIMARY KEY,
    status     TEXT NOT NULL,
    title      TEXT,
    amount     TEXT,
    buyer      TEXT,
    chat_id    TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS raise_state (
    category_id   TEXT PRIMARY KEY,
    name          TEXT,
    last_raise_at REAL,
    next_raise_at REAL,
    last_error    TEXT,
    error_count   INTEGER NOT NULL DEFAULT 0
);
"""

DEFAULT_AUTOREPLY = {
    "autoreply_enabled": "1",
    "autoreply_text": (
        "Здравствуйте! Сейчас я немного занят, отвечу в ближайшее время. Ожидайте, пожалуйста 🙏"
    ),
    "autoreply_interval_hours": "6",
    "autoreply_order_enabled": "0",
    "autoreply_order_text": "Спасибо за заказ! Скоро выдам",
    "autoreply_delay_min": "3",
    "autoreply_delay_max": "8",
    "autoraise_enabled": "1",
    "raise_report_enabled": "1",
    "raise_interval_hours": "4",
}


class Database:
    """Небольшой репозиторий поверх SQLite с подготовленной схемой."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("БД не подключена: вызовите connect()")
        return self._conn

    async def connect(self) -> None:
        """Создаёт БД, включает WAL и применяет схему."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.executescript(SCHEMA)
        for key, value in DEFAULT_AUTOREPLY.items():
            await self._conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))
        await self._conn.commit()

    async def close(self) -> None:
        """Закрывает соединение с БД."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Cursor:
        async with self._lock:
            cursor = await self.conn.execute(sql, params)
            await self.conn.commit()
            return cursor

    async def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        async with self._lock:
            cursor = await self.conn.execute(sql, params)
            row = await cursor.fetchone()
            await cursor.close()
            return row

    async def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        async with self._lock:
            cursor = await self.conn.execute(sql, params)
            rows = await cursor.fetchall()
            await cursor.close()
            return list(rows)

    # --- настройки и секреты -------------------------------------------------

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = await self._fetchone("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        await self._execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    async def get_bool_setting(self, key: str, default: bool = True) -> bool:
        value = await self.get_setting(key)
        if value is None:
            return default
        return value.strip().lower() in {"1", "true", "yes", "on"}

    async def get_float_setting(self, key: str, default: float) -> float:
        value = await self.get_setting(key)
        try:
            return float(value) if value is not None else default
        except ValueError:
            return default

    async def get_secret(self, key: str, env_key: str | None = None) -> str:
        """Приоритет: значение из БД, затем переменная окружения."""
        value = await self.get_setting(key)
        if value:
            return value
        return os.getenv(env_key or key.upper(), "") if env_key or key.upper() else ""

    # --- метаданные ----------------------------------------------------------

    async def get_meta(self, key: str) -> str | None:
        row = await self._fetchone("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        await self._execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    async def set_heartbeat(self, timestamp: float) -> None:
        await self.set_meta("heartbeat", str(timestamp))

    async def get_heartbeat(self) -> float | None:
        raw = await self.get_meta("heartbeat")
        try:
            return float(raw) if raw else None
        except ValueError:
            return None

    # --- сообщения и чаты ----------------------------------------------------

    async def mark_message_processed(
        self,
        message_id: int | str,
        chat_id: str,
        author_id: int | None = None,
        author_name: str | None = None,
        text: str | None = None,
    ) -> bool:
        """Возвращает True, если сообщение увидели впервые (защита от дубликатов)."""
        cursor = await self._execute(
            "INSERT OR IGNORE INTO processed_messages"
            "(message_id, chat_id, author_id, author_name, text, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(message_id), chat_id, author_id, author_name, text, time.time()),
        )
        return cursor.rowcount > 0

    async def get_last_reply_at(self, chat_id: str) -> float | None:
        row = await self._fetchone("SELECT last_reply_at FROM chat_state WHERE chat_id = ?", (chat_id,))
        return row["last_reply_at"] if row else None

    async def set_last_reply_at(
        self, chat_id: str, timestamp: float, username: str | None = None, user_id: int | None = None
    ) -> None:
        """Фиксирует время автоответа, создавая запись о чате при необходимости."""
        await self._execute(
            "INSERT INTO chat_state(chat_id, username, user_id, last_reply_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET "
            "last_reply_at = excluded.last_reply_at, "
            "username = COALESCE(excluded.username, chat_state.username), "
            "user_id = COALESCE(excluded.user_id, chat_state.user_id), "
            "updated_at = excluded.updated_at",
            (chat_id, username, user_id, timestamp, time.time()),
        )

    # --- заказы --------------------------------------------------------------

    async def upsert_order(
        self,
        order_id: str,
        status: str,
        title: str | None,
        amount: str | None,
        buyer: str | None,
        chat_id: str | None,
    ) -> bool:
        """Возвращает True, если заказ новый или изменился его статус."""
        existing = await self._fetchone("SELECT status FROM orders WHERE id = ?", (order_id,))
        changed = existing is None or existing["status"] != status
        now = time.time()
        await self._execute(
            "INSERT INTO orders(id, status, title, amount, buyer, chat_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "status = excluded.status, title = excluded.title, amount = excluded.amount, "
            "buyer = excluded.buyer, chat_id = excluded.chat_id, updated_at = excluded.updated_at",
            (order_id, status, title, amount, buyer, chat_id, now, now),
        )
        return changed

    async def orders_count(self) -> int:
        row = await self._fetchone("SELECT COUNT(*) AS cnt FROM orders")
        return int(row["cnt"]) if row else 0

    # --- поднятие лотов ------------------------------------------------------

    async def set_raise_state(
        self,
        category_id: int | str,
        name: str,
        last_raise_at: float | None,
        next_raise_at: float | None,
        last_error: str | None,
        error_count: int,
    ) -> None:
        await self._execute(
            "INSERT INTO raise_state(category_id, name, last_raise_at, "
            "next_raise_at, last_error, error_count) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(category_id) DO UPDATE SET "
            "name = excluded.name, last_raise_at = excluded.last_raise_at, "
            "next_raise_at = excluded.next_raise_at, last_error = excluded.last_error, "
            "error_count = excluded.error_count",
            (str(category_id), name, last_raise_at, next_raise_at, last_error, error_count),
        )

    async def get_raise_state(self, category_id: int | str) -> dict[str, Any] | None:
        row = await self._fetchone("SELECT * FROM raise_state WHERE category_id = ?", (str(category_id),))
        return dict(row) if row else None

    async def all_raise_states(self) -> list[dict[str, Any]]:
        return [dict(row) for row in await self._fetchall("SELECT * FROM raise_state ORDER BY category_id")]

    async def get_next_raise_at(self) -> float | None:
        row = await self._fetchone("SELECT MIN(next_raise_at) AS value FROM raise_state")
        if not row or row["value"] is None:
            return None
        return float(row["value"])
