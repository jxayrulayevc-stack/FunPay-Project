#!/usr/bin/env python3
"""Отдельный healthcheck: проверяет heartbeat в SQLite и шлёт алерт в TG.

Запускается systemd-таймером раз в минуту. Не импортирует основной код,
поэтому продолжает работать, даже если основной процесс упал.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent
STATE_FILENAME = "healthcheck_state.json"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def read_meta(db_path: Path, key: str) -> str | None:
    """Читает значение из таблицы meta (не блокирует WAL-читателя)."""
    if not db_path.exists():
        return None
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row and row[0] is not None else None
    except sqlite3.Error:
        return None
    finally:
        connection.close()


def send_telegram(token: str, admin_id: int, text: str) -> bool:
    """Отправляет сообщение через Telegram Bot API (только stdlib)."""
    if not token or admin_id <= 0:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": admin_id, "text": text, "parse_mode": "HTML"}).encode("utf-8")
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as response:  # noqa: S310 - фиксированный URL
            return response.status == 200
    except Exception:  # noqa: BLE001 - healthcheck не должен падать из-за сети
        return False


def ping_healthchecks(url: str, *, fail: bool = False) -> None:
    """Опциональный ping Healthchecks.io (по умолчанию выключен)."""
    if not url:
        return
    target = f"{url.rstrip('/')}/fail" if fail else url.rstrip("/")
    try:
        with urllib.request.urlopen(target, timeout=10) as response:  # noqa: S310
            response.read()
    except Exception:  # noqa: BLE001
        pass


def main() -> None:
    """Точка входа таймера."""
    parser = argparse.ArgumentParser(description="Проверка heartbeat FunPay Helper")
    parser.add_argument("--env", type=Path, default=ROOT_DIR / ".env")
    args = parser.parse_args()
    load_dotenv(args.env, override=False)

    data_dir = Path(os.getenv("DATA_DIR", str(ROOT_DIR / "data"))).expanduser()
    db_path = Path(os.getenv("DB_PATH", str(data_dir / "funpay.db"))).expanduser()
    state_path = data_dir / STATE_FILENAME
    max_age = _env_int("HEARTBEAT_MAX_AGE", 180)
    admin_id = _env_int("ADMIN_ID", 0)
    token = read_meta(db_path, "bot_token") or os.getenv("BOT_TOKEN", "") or ""
    healthchecks_url = os.getenv("HEALTHCHECKS_URL", "")

    raw_heartbeat = read_meta(db_path, "heartbeat")
    try:
        heartbeat = float(raw_heartbeat) if raw_heartbeat else 0.0
    except ValueError:
        heartbeat = 0.0
    age = max(0.0, time.time() - heartbeat)
    stale = heartbeat == 0.0 or age > max_age

    state: dict[str, bool] = {"alerted": False}
    if state_path.exists():
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state["alerted"] = bool(loaded.get("alerted", False))
        except (ValueError, OSError):
            pass

    if stale and not state["alerted"]:
        send_telegram(
            token,
            admin_id,
            f"🚨 <b>FunPay Helper не отвечает!</b>\nHeartbeat отсутствует {int(age)} с "
            f"(порог {max_age} с). Проверьте: <code>systemctl status funpay-helper</code>",
        )
        ping_healthchecks(healthchecks_url, fail=True)
        state["alerted"] = True
    elif not stale and state["alerted"]:
        send_telegram(token, admin_id, "✅ FunPay Helper снова отвечает, heartbeat восстановлен.")
        ping_healthchecks(healthchecks_url)
        state["alerted"] = False
    elif not stale:
        ping_healthchecks(healthchecks_url)

    data_dir.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state), encoding="utf-8")


if __name__ == "__main__":
    main()
