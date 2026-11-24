"""Парсинг и проверка секретов, присылаемых админом в чат."""

from __future__ import annotations

import json
import re

from aiogram import Bot

COOKIE_KEYS = {
    "golden_key": "golden_key",
    "goldenkey": "golden_key",
    "golden-key": "golden_key",
    "golden": "golden_key",
    "phpsessid": "phpsessid",
    "php_session_id": "phpsessid",
    "phpsession": "phpsessid",
}
TELEGRAM_TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{20,}$")


def parse_cookie_input(raw: str) -> tuple[str, str | None]:
    """Принимает golden_key в любом удобном формате.

    Поддерживаются: JSON, `golden_key=...; PHPSESSID=...`, две строки через
    пробел/перевод строки и просто один токен golden_key.
    """
    text = raw.strip().strip("`").strip()
    if not text:
        raise ValueError("Пустое сообщение")

    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ValueError("Не удалось разобрать JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("JSON должен быть объектом")
        result: dict[str, str] = {}
        for key, value in data.items():
            normalized = COOKIE_KEYS.get(str(key).strip().lower())
            if normalized:
                result[normalized] = str(value).strip()
        golden = result.get("golden_key", "")
        if not golden:
            raise ValueError("В JSON нет golden_key")
        return golden, result.get("phpsessid") or None

    found: dict[str, str] = {}
    normalized_text = re.sub(r"[\n\r;]+", " ", text)
    for part in normalized_text.split():
        if "=" in part:
            key, _, value = part.partition("=")
            normalized = COOKIE_KEYS.get(key.strip().lower())
            if normalized and value.strip():
                found[normalized] = value.strip().strip("\"'")
        else:
            continue

    if found:
        golden = found.get("golden_key", "")
        if not golden:
            raise ValueError("Не найден golden_key")
        return golden, found.get("phpsessid") or None

    tokens = normalized_text.split()
    if len(tokens) == 1:
        golden = tokens[0]
        phpsessid = None
    elif len(tokens) == 2:
        golden, phpsessid = tokens
    else:
        raise ValueError("Слишком много значений. Пришлите golden_key и (опционально) PHPSESSID")

    if TELEGRAM_TOKEN_RE.match(golden):
        raise ValueError("Похоже, вы прислали токен Telegram, а не golden_key FunPay")
    if len(golden) < 10:
        raise ValueError("golden_key слишком короткий")
    return golden, phpsessid


async def check_bot_token(token: str) -> tuple[int, str]:
    """Проверяет токен через Telegram getMe, возвращает (id, username)."""
    token = token.strip().strip("`").strip()
    if not TELEGRAM_TOKEN_RE.match(token):
        raise ValueError("Токен Telegram выглядит некорректно (ожидается вида 123456789:AA...)")
    bot = Bot(token=token)
    try:
        me = await bot.get_me()
    finally:
        await bot.session.close()
    return me.id, me.username or ""
