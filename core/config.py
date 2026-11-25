"""Загрузка конфигурации из переменных окружения и файла .env.

Приоритет секретов: значения из SQLite (читаются в db.py) > .env > значения по умолчанию.
Здесь хранятся только несекретные настройки процесса и «начальный» токен из .env.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent


def _raw(name: str, default: str = "") -> str:
    """Возвращает строковое значение переменной окружения."""
    value = os.getenv(name)
    return default if value is None else value.strip()


def _get_int(name: str, default: int) -> int:
    raw = _raw(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Переменная {name} должна быть целым числом, получено {raw!r}") from exc


def _get_float(name: str, default: float) -> float:
    raw = _raw(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"Переменная {name} должна быть числом, получено {raw!r}") from exc


def _get_bool(name: str, default: bool) -> bool:
    raw = _raw(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "y"}


@dataclass(frozen=True, slots=True)
class Settings:
    """Все настройки процесса, загруженные один раз при старте."""

    admin_id: int
    bot_token: str
    data_dir: Path
    log_dir: Path
    db_path: Path
    log_level: str
    poll_interval: float
    account_refresh_minutes: int
    lots_refresh_minutes: int
    funpay_request_delay: float
    heartbeat_interval: int
    watchdog_timeout: int
    funpay_unavailable_after: int
    healthchecks_url: str
    healthcheck_max_age: int
    dry_run: bool
    debug: bool
    user_agent: str

    @classmethod
    def load(cls, env_file: Path | None = None, *, dry_run: bool = False) -> Settings:
        """Читает .env и окружение, проверяет обязательные поля."""
        load_dotenv(env_file or (ROOT_DIR / ".env"), override=False)

        admin_id = _get_int("ADMIN_ID", 0)
        if admin_id <= 0:
            raise ValueError("ADMIN_ID не задан или некорректен — укажите numeric Telegram ID в .env")

        # Токен может отсутствовать в .env: главное, чтобы он был в SQLite
        # (сохранён через /token) — проверка делается в main.py.
        bot_token = _raw("BOT_TOKEN")

        data_dir = Path(_raw("DATA_DIR", str(ROOT_DIR / "data"))).expanduser()
        log_dir = Path(_raw("LOG_DIR", str(ROOT_DIR / "logs"))).expanduser()
        db_path = Path(_raw("DB_PATH", str(data_dir / "funpay.db"))).expanduser()

        settings = cls(
            admin_id=admin_id,
            bot_token=bot_token,
            data_dir=data_dir,
            log_dir=log_dir,
            db_path=db_path,
            log_level=_raw("LOG_LEVEL", "INFO").upper(),
            poll_interval=max(2.0, _get_float("FUNPAY_POLL_INTERVAL", 6.0)),
            account_refresh_minutes=max(5, _get_int("ACCOUNT_REFRESH_MINUTES", 45)),
            lots_refresh_minutes=max(5, _get_int("LOTS_REFRESH_MINUTES", 30)),
            funpay_request_delay=min(3.0, max(2.0, _get_float("FUNPAY_REQUEST_DELAY", 2.5))),
            heartbeat_interval=max(10, _get_int("HEARTBEAT_INTERVAL", 60)),
            watchdog_timeout=max(30, _get_int("WATCHDOG_TIMEOUT", 180)),
            funpay_unavailable_after=max(60, _get_int("FUNPAY_UNAVAILABLE_AFTER", 300)),
            healthchecks_url=_raw("HEALTHCHECKS_URL"),
            healthcheck_max_age=max(60, _get_int("HEARTBEAT_MAX_AGE", 180)),
            dry_run=dry_run or _get_bool("DRY_RUN", False),
            debug=_get_bool("DEBUG", False),
            user_agent=_raw(
                "FUNPAY_USER_AGENT",
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            ),
        )
        if settings.debug and settings.log_level == "INFO":
            object.__setattr__(settings, "log_level", "DEBUG")
        return settings
