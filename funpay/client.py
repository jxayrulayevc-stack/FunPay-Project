"""Асинхронный клиент FunPay на httpx + BeautifulSoup.

Использует те же эндпоинты и формат payload, что FunPayAPI из FunPayCardinal,
но не блокирует event loop: все запросы и ожидания асинхронные.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from typing import Any

import httpx

from core.config import Settings
from core.db import Database
from core.logger import SecretMasker
from funpay.errors import (
    FunPayError,
    NetworkError,
    ParseError,
    RaiseError,
    RateLimitError,
    UnauthorizedError,
)
from funpay.models import AccountInfo, Category, ChatMessage, Order, RaiseResult
from funpay.parsing import (
    parse_app_data,
    parse_chat_history,
    parse_raise_response,
    parse_sales_page,
)

BASE_URL = "https://funpay.com"

RUNNER_HEADERS = {
    "accept": "*/*",
    "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
    "x-requested-with": "XMLHttpRequest",
}
MAX_ATTEMPTS = 8
_MESSAGE_ID_RE = re.compile(r'id="message-(\d+)"')


class FunPayClient:
    """Клиент с retry/backoff и контролем авторизации."""

    def __init__(self, settings: Settings, db: Database, masker: SecretMasker, logger: Any) -> None:
        self._settings = settings
        self._db = db
        self._masker = masker
        self._logger = logger
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=httpx.Timeout(25.0, connect=12.0),
            follow_redirects=False,
            headers={"user-agent": settings.user_agent, "accept-language": "ru,en;q=0.9"},
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        self._golden_key = ""
        self._phpsessid: str | None = None
        self.user_id = 0
        self.username = ""
        self.csrf_token = ""
        self.locale = "ru"
        self.categories: tuple[Category, ...] = ()
        self.unauthorized = True
        self._auth_event = asyncio.Event()
        self._last_success_at = 0.0
        self._last_runner_at = 0.0
        self._consecutive_failures = 0

    # --- свойства для /status ------------------------------------------------

    @property
    def last_success_at(self) -> float:
        return self._last_success_at

    @property
    def last_runner_at(self) -> float:
        return self._last_runner_at

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def has_cookies(self) -> bool:
        return bool(self._golden_key)

    # --- жизненный цикл -------------------------------------------------------

    async def start(self) -> None:
        """Загружает куки из БД и проверяет их запросом к FunPay."""
        self._golden_key = await self._db.get_secret("golden_key", "FUNPAY_GOLDEN_KEY")
        self._phpsessid = await self._db.get_secret("phpsessid", "FUNPAY_PHPSESSID") or None
        self._masker.add(self._golden_key)
        self._masker.add(self._phpsessid)
        self._apply_cookies()
        if not self._golden_key:
            self._logger.warning("Куки FunPay не заданы — отправьте их через /cookie")
            return
        try:
            await self.refresh_session()
        except UnauthorizedError:
            self._logger.error("Куки FunPay недействительны при запуске")
        except FunPayError as exc:
            self._logger.error("Не удалось проверить сессию FunPay при запуске: %s", exc)

    async def close(self) -> None:
        """Закрывает HTTP-сессию."""
        await self._http.aclose()

    def _apply_cookies(self, *, golden_key: str | None = None, phpsessid: str | None = None) -> None:
        """Применяет куки к HTTP-клиенту."""
        self._http.cookies.clear()
        if golden_key is not None:
            self._golden_key = golden_key
        if phpsessid is not None:
            self._phpsessid = phpsessid
        if self._golden_key:
            self._http.cookies.set("golden_key", self._golden_key, domain="funpay.com")
        if self._phpsessid:
            self._http.cookies.set("PHPSESSID", self._phpsessid, domain="funpay.com")
        self._http.cookies.set("cookie_prefs", "1", domain="funpay.com")

    def _mark_unauthorized(self) -> None:
        self.unauthorized = True
        self._auth_event.clear()

    def _mark_authorized(self) -> None:
        self.unauthorized = False
        self._last_success_at = time.time()
        self._consecutive_failures = 0
        self._auth_event.set()

    async def wait_authorized(self, stop_event: asyncio.Event) -> bool:
        """Ждёт валидных куков. Возвращает True, если запрошен stop."""
        while self.unauthorized and not stop_event.is_set():
            if await asyncio.wait_for(stop_event.wait(), timeout=5.0):
                return True
        return stop_event.is_set()

    # --- низкоуровневые запросы ---------------------------------------------

    def _is_login_response(self, response: httpx.Response) -> bool:
        if response.status_code == 403:
            return True
        location = response.headers.get("location", "")
        return bool(location) and ("account/login" in location or "logout" in location)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
        cookies: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Выполняет запрос с exponential backoff + jitter для 429/5xx и сети."""
        http_client = client or self._http
        url = path if path.startswith("http") else f"{BASE_URL}/{path.lstrip('/')}"
        last_error: Exception | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await http_client.request(
                    method, url, data=data, params=params, headers=headers, cookies=cookies
                )
            except httpx.RequestError as exc:
                last_error = exc
                self._consecutive_failures += 1
                delay = min(2**attempt, 60) + random.uniform(0, 1.5)
                self._logger.warning(
                    "Сеть: ошибка запроса к FunPay (попытка %s/%s, пауза %s с): %s",
                    attempt,
                    MAX_ATTEMPTS,
                    round(delay, 1),
                    type(exc).__name__,
                )
                await asyncio.sleep(delay)
                continue

            if self._is_login_response(response):
                if client is None:
                    self._mark_unauthorized()
                raise UnauthorizedError("Куки FunPay недействительны или сессия завершена")

            if response.status_code == 429:
                last_error = RateLimitError("FunPay вернул 429 Too Many Requests")
                self._consecutive_failures += 1
                retry_after = response.headers.get("retry-after", "")
                try:
                    delay = min(float(retry_after), 120.0) if retry_after else 0.0
                except ValueError:
                    delay = 0.0
                delay = delay or min(2**attempt, 60) + random.uniform(0, 1.5)
                self._logger.warning(
                    "FunPay: 429, попытка %s/%s, пауза %s с", attempt, MAX_ATTEMPTS, round(delay, 1)
                )
                await asyncio.sleep(delay)
                continue

            if response.status_code >= 500:
                last_error = NetworkError(f"FunPay вернул HTTP {response.status_code}")
                self._consecutive_failures += 1
                delay = min(2**attempt, 60) + random.uniform(0, 1.5)
                self._logger.warning(
                    "FunPay: HTTP %s, попытка %s/%s, пауза %s с",
                    response.status_code,
                    attempt,
                    MAX_ATTEMPTS,
                    round(delay, 1),
                )
                await asyncio.sleep(delay)
                continue

            if response.status_code != 200:
                raise FunPayError(f"FunPay вернул HTTP {response.status_code} на {path}")

            if client is None:
                self._mark_authorized()
            return response

        raise (last_error or NetworkError("FunPay недоступен"))

    # --- авторизация ---------------------------------------------------------

    async def refresh_session(self) -> AccountInfo:
        """Обновляет сессию (и PHPSESSID) запросом к главной странице."""
        if not self._golden_key:
            self._mark_unauthorized()
            raise UnauthorizedError("Куки не заданы")
        response = await self._request("GET", "/")
        info = parse_app_data(response.text)

        self.user_id = info.user_id
        self.username = info.username
        self.csrf_token = info.csrf_token
        self.locale = info.locale
        self.categories = info.categories

        phpsessid = response.cookies.get("PHPSESSID")
        if phpsessid and phpsessid != self._phpsessid:
            self._phpsessid = phpsessid
            self._http.cookies.set("PHPSESSID", phpsessid, domain="funpay.com")
            await self._db.set_setting("phpsessid", phpsessid)
            self._masker.add(phpsessid)

        await self._db.set_setting("funpay_username", info.username)
        await self._db.set_setting("funpay_user_id", str(info.user_id))
        self._mark_authorized()
        self._logger.info(
            "FunPay: сессия активна, аккаунт %s, категорий: %s", info.username, len(info.categories)
        )
        return info

    async def update_cookies(self, golden_key: str, phpsessid: str | None = None) -> AccountInfo:
        """Проверяет новые куки и сохраняет их только при успешном входе."""
        golden_key = golden_key.strip()
        phpsessid = phpsessid.strip() if phpsessid else None
        probe = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=httpx.Timeout(25.0, connect=12.0),
            follow_redirects=False,
            headers={"user-agent": self._settings.user_agent},
        )
        cookies = {"golden_key": golden_key, "cookie_prefs": "1"}
        if phpsessid:
            cookies["PHPSESSID"] = phpsessid
        try:
            response = await self._request("GET", "/", client=probe, cookies=cookies)
            info = parse_app_data(response.text)
        finally:
            await probe.aclose()

        self._apply_cookies(golden_key=golden_key, phpsessid=phpsessid)
        await self._db.set_setting("golden_key", golden_key)
        await self._db.set_setting("phpsessid", phpsessid or "")
        self._masker.add(golden_key)
        self._masker.add(phpsessid)
        self.user_id = info.user_id
        self.username = info.username
        self.csrf_token = info.csrf_token
        self.locale = info.locale
        self.categories = info.categories
        self._mark_authorized()
        self._logger.info("Куки FunPay обновлены, аккаунт: %s", info.username)
        return info

    # --- runner --------------------------------------------------------------

    async def runner(
        self, objects: list[dict[str, Any]], request: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Отправляет пакет объектов на long-polling эндпоинт runner/."""
        if self.unauthorized:
            raise UnauthorizedError("Нет авторизации на FunPay")
        payload = {
            "objects": json.dumps(objects, ensure_ascii=False),
            "request": json.dumps(request, ensure_ascii=False) if request else "false",
            "csrf_token": self.csrf_token,
        }
        response = await self._request("POST", "runner/", data=payload, headers=RUNNER_HEADERS)
        self._last_runner_at = time.time()
        try:
            result = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ParseError("Runner вернул не JSON") from exc
        if not isinstance(result, dict):
            raise ParseError("Runner вернул неожиданный формат ответа")
        error_text = str(result.get("error") or result.get("msg") or "").lower()
        if "authoriz" in error_text or "login" in error_text:
            self._mark_unauthorized()
            raise UnauthorizedError("FunPay сообщил об отсутствии авторизации")
        return result

    async def get_updates(
        self, msg_tag: str, order_tag: str, chats_objects: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        """Запрашивает изменения чатов и заказов."""
        objects: list[dict[str, Any]] = [
            {"type": "chat_bookmarks", "id": self.user_id, "tag": msg_tag, "data": False},
            {"type": "orders_counters", "id": self.user_id, "tag": order_tag, "data": False},
        ]
        if chats_objects:
            objects.extend(chats_objects)
        result = await self.runner(objects)
        raw_objects = result.get("objects") or []
        return [item for item in raw_objects if isinstance(item, dict)]

    async def get_chat_history(self, node: str, last_message_id: int = -1) -> list[ChatMessage]:
        """Получает историю чата (только новые сообщения)."""
        if self.unauthorized:
            raise UnauthorizedError("Нет авторизации на FunPay")
        response = await self._request(
            "GET",
            "/chat/history",
            params={"node": node, "last_message": last_message_id},
            headers={"accept": "*/*", "x-requested-with": "XMLHttpRequest"},
        )
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ParseError("chat/history вернул не JSON") from exc
        return parse_chat_history(payload, self_id=self.user_id)

    async def send_message(self, node: str, text: str) -> int | None:
        """Отправляет сообщение в чат через runner/. Возвращает ID сообщения."""
        if self.unauthorized:
            raise UnauthorizedError("Нет авторизации на FunPay")
        objects = [
            {
                "type": "chat_node",
                "id": node,
                "tag": "00000000",
                "data": {"node": node, "last_message": -1, "content": ""},
            }
        ]
        request = {"action": "chat_message", "data": {"node": node, "last_message": -1, "content": text}}
        result = await self.runner(objects, request)
        error_text = str(result.get("error") or result.get("msg") or "")
        if error_text:
            raise FunPayError(f"FunPay не доставил сообщение: {error_text}")
        for obj in result.get("objects") or []:
            html = str(
                ((obj.get("data") or {}).get("html") if isinstance(obj.get("data"), dict) else "") or ""
            )
            if match := _MESSAGE_ID_RE.search(html):
                return int(match.group(1))
        return None

    # --- заказы и поднятие ---------------------------------------------------

    async def get_sales(self) -> list[Order]:
        """Получает список продаж со страницы /orders/trade."""
        if self.unauthorized:
            raise UnauthorizedError("Нет авторизации на FunPay")
        response = await self._request("GET", "/orders/trade")
        if not response.text or "user-link-name" not in response.text:
            self._mark_unauthorized()
            raise UnauthorizedError("Страница продаж не содержит данных аккаунта")
        return parse_sales_page(response.text, self_id=self.user_id)

    async def raise_category(self, category: Category) -> RaiseResult:
        """Поднимает все подкатегории категории; возвращает время следующей попытки."""
        now = time.time()
        if not category.subcategory_ids:
            raise RaiseError("У категории нет подкатегорий")
        payload: dict[str, Any] = {
            "game_id": category.id,
            "node_id": category.subcategory_ids[0],
            "node_ids[]": list(category.subcategory_ids),
        }
        response = await self._request(
            "POST",
            "/lots/raise",
            data=payload,
            headers=RUNNER_HEADERS,
        )
        try:
            result = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ParseError("lots/raise вернул не JSON") from exc
        parsed = parse_raise_response(
            result,
            category_id=category.id,
            now=now,
            fallback_wait_seconds=int(self._settings.account_refresh_minutes * 60),
        )
        if not parsed.ok:
            raise RaiseError(parsed.message, parsed.wait_seconds)
        return parsed


__all__ = ["FunPayClient"]
