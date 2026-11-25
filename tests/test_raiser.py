"""Тесты планирования поднятия лотов."""

from __future__ import annotations

from funpay.models import RaiseResult
from funpay.parsing import format_duration, parse_raise_response
from funpay.raiser import AutoRaiser


def test_next_attempt_from_wait_field() -> None:
    result = parse_raise_response({"wait": 14400}, category_id=1, now=1000.0)
    assert result.next_attempt_at == 15400.0
    assert format_duration(result.wait_seconds) == "4 ч 0 мин"


def test_report_contains_all_categories() -> None:
    results = [
        RaiseResult(1, True, 14400, 15400, "Лоты подняты"),
        RaiseResult(2, False, 7200, 8200, "Подождите 2 часа"),
    ]
    report = AutoRaiser._format_report(results)
    assert "✅ #1" in report
    assert "⏳ #2" in report
    assert "Подождите 2 часа" in report
    assert "Поднято: 1" in report
