"""Тесты разбора ответов FunPay."""

from __future__ import annotations

from funpay.parsing import (
    DEFAULT_WAIT_SECONDS,
    format_duration,
    parse_chat_history,
    parse_raise_response,
    parse_sales_page,
    parse_wait_time,
)

ACCOUNT_HTML = """
<html><body data-app-data='{"userId":42,"csrf-token":"abc","locale":"ru"}'>
<div class="user-link-name">SELLER</div>
<div class="promo-game-list"><div class="promo-game-item">
<div class="game-title" data-id="10"></div><a href="/lots/offer/">Игра</a>
<ul class="list-inline" data-id="10">
  <li><a href="https://funpay.com/lots/offer/?category=101">Подкатегория 1</a></li>
  <li><a href="https://funpay.com/lots/chips/offer/102/">Подкатегория 2</a></li>
</ul></div></div></body></html>
"""

CHAT_HISTORY = {
    "chat": {
        "node": {"id": "users-1-42"},
        "messages": [
            {"id": 10, "author": 0, "html": "<div role='alert'>Заказ оплачен</div>"},
            {"id": 11, "author": 1, "html": "<div class='chat-msg-text'>Привет</div>"},
            {"id": 12, "author": 42, "html": "<div class='chat-msg-text'>Здравствуйте!</div>"},
        ],
    }
}

SALES_HTML = """
<div class="user-link-name">SELLER</div>
<a class="tc-item info">
  <div class="tc-order">#ABC12345</div>
  <div class="order-desc"><div>Лот «Золото»</div></div>
  <div class="tc-price">1 500 ₽</div>
  <div class="media-user-name"><span data-href="https://funpay.com/users/42/">BUYER</span></div>
</a>
<a class="tc-item warning">
  <div class="tc-order">#DEF67890</div>
  <div class="order-desc"><div>Лот «Серебро»</div></div>
  <div class="tc-price">700 ₽</div>
  <div class="media-user-name"><span data-href="https://funpay.com/users/43/">OTHER</span></div>
</a>
"""


def test_parse_wait_time_russian() -> None:
    assert parse_wait_time("Подождите 3 часа 45 минут") == 3 * 3600 + 45 * 60


def test_parse_wait_time_english_and_ukrainian() -> None:
    assert parse_wait_time("Please wait 2 hours") == 7200
    assert parse_wait_time("Зачекайте 5 хвилин") == 300
    assert parse_wait_time("Подождите 30 секунд") == 30


def test_parse_wait_time_fallback() -> None:
    assert parse_wait_time("Что-то пошло не так") == DEFAULT_WAIT_SECONDS


def test_parse_raise_response_success_uses_wait_field() -> None:
    now = 1_000_000.0
    result = parse_raise_response({"wait": 14400}, category_id=7, now=now)
    assert result.ok is True
    assert result.wait_seconds == 14400
    assert result.next_attempt_at == now + 14400


def test_parse_raise_response_cooldown_message() -> None:
    now = 1_000_000.0
    result = parse_raise_response({"error": True, "msg": "Подождите 4 часа"}, category_id=7, now=now)
    assert result.ok is False
    assert result.wait_seconds == 14400
    assert result.next_attempt_at == now + 14400


def test_parse_raise_response_wait_field_priority() -> None:
    result = parse_raise_response(
        {"error": True, "msg": "Подождите 4 часа", "wait": 5400}, category_id=7, now=0
    )
    assert result.wait_seconds == 5400


def test_parse_raise_response_url_forces_long_delay() -> None:
    result = parse_raise_response(
        {"error": True, "url": "https://funpay.com/captcha", "wait": 10}, category_id=7, now=0
    )
    assert result.ok is False
    assert result.wait_seconds >= 3600


def test_parse_account_page_categories() -> None:
    info = None
    from funpay.parsing import parse_app_data

    info = parse_app_data(ACCOUNT_HTML)
    assert info.user_id == 42
    assert info.username == "SELLER"
    assert info.csrf_token == "abc"
    assert len(info.categories) == 1
    assert info.categories[0].id == 10
    assert info.categories[0].subcategory_ids == (101, 102)


def test_parse_chat_history_marks_senders() -> None:
    messages = parse_chat_history(CHAT_HISTORY, self_id=1)
    assert [m.id for m in messages] == [10, 11, 12]
    assert messages[0].is_system is True
    assert messages[1].is_self is True
    assert messages[2].is_self is False
    assert messages[2].text == "Здравствуйте!"


def test_parse_sales_page_statuses() -> None:
    orders = parse_sales_page(SALES_HTML, self_id=1)
    assert [o.status for o in orders] == ["paid", "refunded"]
    assert orders[0].id == "ABC12345"
    assert orders[0].amount == "1500"
    assert orders[0].chat_id == "users-1-42"


def test_format_duration() -> None:
    assert format_duration(3665) == "1 ч 1 мин"
    assert format_duration(65) == "1 мин 5 с"
