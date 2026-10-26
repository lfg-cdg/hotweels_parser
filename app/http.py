"""Общий HTTP-клиент: не больше одного запроса одновременно, пауза между запросами, повторы с backoff."""
from __future__ import annotations

import asyncio
import logging
import random
import time

import httpx

log = logging.getLogger(__name__)


class SiteError(Exception):
    """Сайт недоступен или ответил ошибкой."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class FormatError(SiteError):
    """Сайт ответил, но формат ответа не тот, что мы ожидаем."""


class PoliteClient:
    def __init__(
        self,
        base_url: str,
        headers: dict[str, str],
        *,
        min_pause: float = 1.0,
        max_pause: float = 3.0,
        retries: int = 4,
        backoff_base: float = 2.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=30.0, transport=transport, follow_redirects=True
        )
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self._min_pause, self._max_pause = min_pause, max_pause
        self._retries = retries
        self._backoff_base = backoff_base

    async def close(self) -> None:
        await self._client.aclose()

    async def request_json(self, method: str, path: str, *, params: dict | None = None,
                           json_body: dict | None = None) -> object:
        async with self._lock:
            for attempt in range(1, self._retries + 1):
                wait = self._last_request + random.uniform(self._min_pause, self._max_pause) - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_request = time.monotonic()
                try:
                    resp = await self._client.request(method, path, params=params, json=json_body)
                except httpx.HTTPError as e:
                    err = SiteError(f"сетевая ошибка: {e!r}")
                else:
                    if resp.status_code == 200:
                        try:
                            return resp.json()
                        except ValueError as e:
                            raise FormatError(f"ответ не JSON: {resp.text[:200]!r}") from e
                    err = SiteError(f"HTTP {resp.status_code} на {path}: {resp.text[:200]!r}", resp.status_code)
                    # 4xx (кроме 429) повторять бессмысленно
                    if 400 <= resp.status_code < 500 and resp.status_code != 429:
                        raise err
                if attempt == self._retries:
                    raise err
                delay = self._backoff_base * 2 ** (attempt - 1) + random.uniform(0, 1)
                log.warning("%s; попытка %d/%d, повтор через %.1f с", err, attempt, self._retries, delay)
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")
