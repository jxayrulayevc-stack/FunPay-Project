"""Чистые функции разбора HTML/JSON ответов FunPay.

Все функции детерминированы и не делают запросов — их легко покрыть тестами.
"""

from __future__ import annotations

import json
import random
import re
import string
import unicodedata
from typing import Any

from bs4 import BeautifulSoup

from funpay.errors import ParseError
from funpay.models import AccountInfo, Category, ChatContact, ChatMessage, Order, RaiseResult

DEFAULT_WAIT_SECONDS = 7200

_HOURS_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(часов|часа|час|hours?|годин|години|h)\b", re.IGNORECASE)
_MINUTES_RE = re.compile(r"(\d+)\s*(минут[а-я]*|minutes?|хвилин[а-я]*|min|m)\b", re.IGNORECASE)
_SECONDS_RE = re.compile(r"(\d+)\s*(секунд[а-я]*|seconds?|секунд|sec|s)\b", re.IGNORECASE)
_WAIT_WORDS_RE = re.compile(r"подождите|please\s+wait|зачекайте", re.IGNORECASE)


def random_tag(length: int = 10) -> str:
    """Генерирует тег события для long-polling (как в FunPayAPI)."""
    alphabet = string.digits + string.ascii_lowercase
    return "".join(random.choice(alphabet) for _ in range(length))


def strip_invisible_suffix(text: str) -> str:
    """Убирает переводы строк и невидимые Unicode-символы в конце текста."""
    end = len(text)
    while end > 0 and (text[end - 1].isspace() or unicodedata.category(text[end - 1]) == "Cf"):
        end -= 1
    return text[:end]


def parse_wait_time(message: str) -> int:
    """Достаёт время ожидания из сообщения вида «Подождите 3 часа 45 минут».

    Возвращает секунды. Если единицы не распознаны — DEFAULT_WAIT_SECONDS.
    """
    text = message.replace(",", ".")
    total = 0.0
    found = False
    for pattern, multiplier in ((_HOURS_RE, 3600), (_MINUTES_RE, 60), (_SECONDS_RE, 1)):
        match = pattern.search(text)
        if match:
            total += float(match.group(1).replace(",", ".")) * multiplier
            found = True
    if found:
        return max(1, int(total))
    if _WAIT_WORDS_RE.search(text):
        return DEFAULT_WAIT_SECONDS
    return DEFAULT_WAIT_SECONDS


def parse_raise_response(
    payload: dict[str, Any],
    *,
    category_id: int,
    now: float,
    fallback_wait_seconds: int = DEFAULT_WAIT_SECONDS,
) -> RaiseResult:
    """Преобразует JSON ответа `lots/raise` в результат поднятия.

    FunPay возвращает `wait` (секунды) и/или текст ошибки «Подождите N часов».
    Приоритет: числовое поле `wait`, затем текст сообщения, затем fallback.
    """
    error = payload.get("error")
    message = str(payload.get("msg") or payload.get("message") or "").strip()
    url = payload.get("url")

    raw_wait = payload.get("wait")
    try:
        wait_seconds = int(float(raw_wait)) if raw_wait is not None else 0
    except (TypeError, ValueError):
        wait_seconds = 0
    if wait_seconds <= 0:
        wait_seconds = (
            parse_wait_time(message) if message and _WAIT_WORDS_RE.search(message) else fallback_wait_seconds
        )

    ok = not error and not url
    if not ok and url and wait_seconds < 3600:
        # Ссылка обычно означает капчу/подтверждение — не дёргаем FunPay часто.
        wait_seconds = 3600
    if not message:
        message = "Лоты подняты" if ok else "Отказ FunPay без пояснения"
    return RaiseResult(
        category_id=category_id,
        ok=ok,
        wait_seconds=wait_seconds,
        next_attempt_at=now + wait_seconds,
        message=message,
    )


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def parse_app_data(html: str) -> AccountInfo:
    """Парсит главную страницу: ник, ID, csrf-токен и категории продавца."""
    soup = _soup(html)
    body = soup.find("body")
    raw_app_data = str(body.get("data-app-data") or "") if body else ""
    if not raw_app_data:
        raise ParseError("На странице нет data-app-data — похоже, куки недействительны")
    try:
        app_data = json.loads(raw_app_data)
    except (TypeError, ValueError) as exc:
        raise ParseError("Не удалось разобрать data-app-data") from exc

    username_tag = soup.find("div", class_="user-link-name")
    if not username_tag:
        raise ParseError("На странице нет ника пользователя — куки недействительны")
    try:
        user_id = int(app_data["userId"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ParseError("В data-app-data нет userId") from exc

    return AccountInfo(
        user_id=user_id,
        username=username_tag.get_text(strip=True),
        csrf_token=str(app_data.get("csrf-token", "")),
        locale=str(app_data.get("locale", "ru")),
        categories=tuple(parse_categories(html)),
    )


def parse_categories(html: str) -> list[Category]:
    """Достаёт все категории (игры) и подкатегории продавца с главной страницы."""
    soup = _soup(html)
    tables = soup.find_all("div", class_="promo-game-list")
    if not tables:
        return []
    table = tables[1] if len(tables) > 1 else tables[0]
    result: list[Category] = []
    for item in table.find_all("div", class_="promo-game-item"):
        title = item.find("div", class_="game-title")
        if not title or not title.get("data-id"):
            continue
        try:
            game_id = int(str(title["data-id"]))
        except (TypeError, ValueError):
            continue
        link = item.find("a", href=True)
        name = link.get_text(strip=True) if link else f"Категория {game_id}"

        subcategory_ids: set[int] = set()
        for listing in item.find_all("ul", class_="list-inline"):
            for anchor in listing.find_all("a", href=True):
                href = str(anchor["href"])
                ids = re.findall(r"/(\d+)(?:/|$)", href)
                ids += re.findall(r"(?:category|node|section)=(\d+)", href)
                for raw_id in ids:
                    sub_id = int(raw_id)
                    if sub_id != game_id:
                        subcategory_ids.add(sub_id)
        if not subcategory_ids:
            # Если подкатегорий нет, FunPay всё равно принимает node_id = game_id.
            subcategory_ids.add(game_id)
        result.append(Category(game_id, name, tuple(sorted(subcategory_ids))))
    return result


def parse_chat_contacts(obj: dict[str, Any]) -> list[ChatContact]:
    """Разбирает объект `chat_bookmarks` из ответа runner/."""
    data = obj.get("data") or {}
    html = data.get("html") if isinstance(data, dict) else None
    if not html:
        return []
    contacts: list[ChatContact] = []
    for anchor in _soup(html).find_all("a", class_="contact-item"):
        chat_id = str(anchor.get("data-id", ""))
        message_tag = anchor.find("div", class_="contact-item-message")
        name_tag = anchor.find("div", class_="media-user-name")
        if not chat_id or not message_tag:
            continue
        raw_last_id = anchor.get("data-node-msg")
        try:
            last_id = int(str(raw_last_id)) if raw_last_id is not None else 0
        except (TypeError, ValueError):
            last_id = 0
        contacts.append(
            ChatContact(
                chat_id=chat_id,
                username=name_tag.get_text(" ", strip=True) if name_tag else "Покупатель",
                last_message=strip_invisible_suffix(message_tag.get_text(" ", strip=True)),
                last_message_id=last_id,
            )
        )
    return contacts


def parse_chat_history(payload: dict[str, Any], *, self_id: int) -> list[ChatMessage]:
    """Разбирает ответ `chat/history` и помечает системные/свои сообщения."""
    chat = payload.get("chat") or {}
    node = chat.get("node") or {}
    chat_id = str(node.get("id", ""))
    messages: list[ChatMessage] = []
    for raw in chat.get("messages", []):
        try:
            message_id = int(raw.get("id", 0))
            author_id = int(raw.get("author", 0))
        except (TypeError, ValueError):
            continue
        html = str(raw.get("html", ""))
        soup = _soup(html)
        if soup.find("a", class_="chat-img-link"):
            text = "📷 Изображение"
        else:
            text_tag = soup.find("div", class_="chat-msg-text") or soup.find(role="alert")
            text = strip_invisible_suffix(text_tag.get_text("\n", strip=True)) if text_tag else ""
        author_tag = soup.find("div", class_="media-user-name")
        author_name = author_tag.get_text(" ", strip=True) if author_tag else None
        messages.append(
            ChatMessage(
                id=message_id,
                chat_id=chat_id,
                author_id=author_id,
                author_name=author_name,
                text=text,
                is_system=author_id == 0,
                is_self=author_id == self_id,
            )
        )
    return messages


def parse_sales_page(html: str, *, self_id: int) -> list[Order]:
    """Разбирает страницу «Мои продажи» в список заказов."""
    soup = _soup(html)
    orders: list[Order] = []
    for item in soup.find_all("a", class_="tc-item"):
        classes = [str(value) for value in (item.get("class") or [])]
        if "warning" in classes:
            status = "refunded"
        elif "info" in classes:
            status = "paid"
        else:
            status = "closed"

        order_tag = item.find("div", class_="tc-order")
        desc_tag = item.find("div", class_="order-desc")
        price_tag = item.find("div", class_="tc-price")
        buyer_tag = item.find("div", class_="media-user-name")
        if not (order_tag and desc_tag and price_tag and buyer_tag):
            continue
        order_id = order_tag.get_text(strip=True).lstrip("#").strip()
        desc_inner = desc_tag.find("div") or desc_tag
        title = desc_inner.get_text(" ", strip=True)
        price_parts = price_tag.get_text(" ", strip=True).rsplit(" ", 1)
        amount = price_parts[0].replace(" ", "") if price_parts else "0"
        currency = price_parts[-1] if len(price_parts) > 1 else "₽"
        buyer_inner = buyer_tag.find("span") or buyer_tag
        buyer = buyer_inner.get_text(" ", strip=True)
        href = str(buyer_inner.get("data-href", ""))
        match = re.search(r"/users/(\d+)", href)
        buyer_id = int(match.group(1)) if match else 0
        chat_id = f"users-{min(buyer_id, self_id)}-{max(buyer_id, self_id)}" if buyer_id else order_id
        orders.append(
            Order(
                id=order_id,
                title=title,
                amount=amount,
                currency=currency,
                buyer=buyer,
                buyer_id=buyer_id,
                status=status,
                chat_id=chat_id,
            )
        )
    return orders


def format_duration(seconds: float) -> str:
    """Человекочитаемое время для /status и отчётов."""
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} ч {minutes} мин"
    if minutes:
        return f"{minutes} мин {secs} с"
    return f"{secs} с"
