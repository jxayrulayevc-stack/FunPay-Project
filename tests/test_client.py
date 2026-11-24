"""Тесты ожидания авторизации FunPay без обращения к сети."""

from __future__ import annotations

import asyncio

from funpay.client import FunPayClient


def _client() -> FunPayClient:
    client = FunPayClient.__new__(FunPayClient)
    client.unauthorized = True
    client._auth_event = asyncio.Event()  # noqa: SLF001 - тест проверяет внутреннее событие
    return client


def test_wait_authorized_survives_timeout_and_stops() -> None:
    async def scenario() -> None:
        client = _client()
        stop = asyncio.Event()
        task = asyncio.create_task(client.wait_authorized(stop))
        await asyncio.sleep(5.2)  # дольше внутреннего интервала ожидания 5 с
        assert not task.done(), "ожидание куков не должно падать по TimeoutError"
        stop.set()
        assert await asyncio.wait_for(task, 2.0) is True

    asyncio.run(scenario())


def test_wait_authorized_wakes_on_new_cookies() -> None:
    async def scenario() -> None:
        client = _client()
        stop = asyncio.Event()
        task = asyncio.create_task(client.wait_authorized(stop))
        client.unauthorized = False
        client._auth_event.set()  # noqa: SLF001 - имитация /cookie
        assert await asyncio.wait_for(task, 2.0) is False

    asyncio.run(scenario())
