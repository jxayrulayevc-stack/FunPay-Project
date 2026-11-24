"""Логирование с ротацией и гарантированной маскировкой секретов.

Ни один токен/куки не должен попасть ни в файл логов, ни в journald,
ни в ответ на команду /logs. Маскировка делается на уровне logging.Filter,
поэтому защищены все сообщения, в том числе из сторонних библиотек.
"""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
LOG_FILENAME = "funpay-helper.log"

_TELEGRAM_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")
_SECRET_PAIR_RE = re.compile(r"(?i)\b(golden_key|phpsessid|bot_token|token)\b([\"'=:\s]+)([^\s,;&\"']+)")


class SecretMasker:
    """Реестр секретов, которые нужно заменить звёздочками в любом тексте."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()

    def add(self, value: str | None) -> None:
        """Добавляет секрет в реестр (короткие значения игнорируются)."""
        if value and len(value) >= 5:
            self._secrets.add(value)

    def mask(self, text: str) -> str:
        """Возвращает текст с замаскированными секретами."""
        result = text
        for secret in sorted(self._secrets, key=len, reverse=True):
            result = result.replace(secret, "***")
        result = _TELEGRAM_TOKEN_RE.sub("***", result)
        result = _SECRET_PAIR_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", result)
        return result


class MaskingFilter(logging.Filter):
    """Фильтр, применяющий SecretMasker к сообщению и аргументам записи."""

    def __init__(self, masker: SecretMasker) -> None:
        super().__init__()
        self._masker = masker

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D102
        record.msg = self._masker.mask(str(record.msg))
        if isinstance(record.args, dict):
            record.args = {key: self._masker.mask(str(value)) for key, value in record.args.items()}
        elif record.args:
            record.args = tuple(self._masker.mask(str(arg)) for arg in record.args)
        return True


def setup_logging(log_dir: Path, level: str, masker: SecretMasker) -> logging.Logger:
    """Настраивает корневой логгер приложения: файл 5 МБ x 5 + stdout."""
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("funpay_helper")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter(LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
    masking_filter = MaskingFilter(masker)

    file_handler = RotatingFileHandler(
        log_dir / LOG_FILENAME,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(masking_filter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(masking_filter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    # httpx на INFO печатал бы URL и заголовки — нам это не нужно.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return logger


def log_path(log_dir: Path) -> Path:
    """Путь к текущему файлу логов (для команды /logs)."""
    return log_dir / LOG_FILENAME


def read_last_lines(log_dir: Path, count: int = 30, masker: SecretMasker | None = None) -> str:
    """Читает последние строки лога, при необходимости маскируя секреты."""
    path = log_path(log_dir)
    if not path.exists():
        return "Лог-файл пока пуст."
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = "\n".join(lines[-count:]) or "Лог-файл пока пуст."
    return masker.mask(tail) if masker else tail
