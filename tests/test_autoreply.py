"""Тесты логики кулдауна автоответчика."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from core.db import Database
from core.logger import SecretMasker
from funpay.autoreply import AutoreplyService, cooldown_passed


class FakeAlerts:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, kind: str, text: str, *, cooldown: float | None = None) -> bool:  # noqa: ARG002
        self.messages.append(text)
        return True

    def reset(self, kind: str) -> None:  # noqa: ARG002
        pass


class FakeClient:
    is_mock = True

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, node: str, text: str) -> int:
        self.sent.append((node, text))
        return 1


def test_cooldown_passed_first_message() -> None:
    assert cooldown_passed(None, time.time(), 6) is True


def test_cooldown_passed_blocks_early_reply() -> None:
    now = 1_000_000.0
    assert cooldown_passed(now - 5 * 3600, now, 6) is False
    assert cooldown_passed(now - 7 * 3600, now, 6) is True


def test_cooldown_passed_zero_interval() -> None:
    assert cooldown_passed(1_000_000.0, 1_000_001.0, 0) is True


async def test_autoreply_service_respects_cooldown(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    await db.connect()
    await db.set_setting("autoreply_delay_min", "0.01")
    await db.set_setting("autoreply_delay_max", "0.02")
    await db.set_setting("autoreply_text", "Здравствуйте!")
    client = FakeClient()
    service = AutoreplyService(db, client, FakeAlerts(), logging.getLogger("test"))  # type: ignore[arg-type]

    assert await service.maybe_reply("users-1-2", "BUYER") is True
    await asyncio.sleep(0.1)
    assert client.sent == [("users-1-2", "Здравствуйте!")]

    assert await service.maybe_reply("users-1-2", "BUYER") is False
    await asyncio.sleep(0.05)
    assert len(client.sent) == 1
    await db.close()


async def test_autoreply_service_disabled(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    await db.connect()
    await db.set_setting("autoreply_enabled", "0")
    client = FakeClient()
    service = AutoreplyService(db, client, FakeAlerts(), logging.getLogger("test"))  # type: ignore[arg-type]
    assert await service.maybe_reply("users-1-2", "BUYER") is False
    await db.close()


async def test_order_template_disabled_by_default(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    await db.connect()
    client = FakeClient()
    service = AutoreplyService(db, client, FakeAlerts(), logging.getLogger("test"))  # type: ignore[arg-type]
    assert await service.maybe_reply("users-1-2", "BUYER", order=True) is False
    await db.close()


def test_masker_hides_secrets() -> None:
    masker: Any = SecretMasker()
    masker.add("superSecretGoldenKey")
    assert "superSecretGoldenKey" not in masker.mask("key=superSecretGoldenKey")
    assert "***" in masker.mask("123456789:AAabcdefghijklmnopqrstuvwxyz1234567")
