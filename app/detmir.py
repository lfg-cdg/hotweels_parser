"""Клиент открытого API detmir.ru.

Используются два запроса (те же, что делает сам сайт):
  GET https://api.detmir.ru/v4/products?search=<штрихкод>        — найти товар
  GET https://api.detmir.ru/v4/products/<id>/delivery?filter=region.iso:<ISO>
                                                                   — наличие и самовывоз по магазинам

Точного количества API не отдаёт: только уровень slvl (HIGH / MEDIUM / LOW / LAST) у магазинов с наличием.
У магазинов без товара slvl нет, зато есть collectedAt — дата, когда заказ можно будет забрать
(товар привезут со склада). Если магазина нет в ответе совсем — товар туда даже под заказ не везут.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from .http import FormatError, PoliteClient, SiteError

log = logging.getLogger(__name__)

API = "https://api.detmir.ru"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Origin": "https://www.detmir.ru",
    "Referer": "https://www.detmir.ru/",
}

# Уровни по возрастанию. NONE — магазина нет в ответе (товара нет).
LEVELS = {"NONE": 0, "LAST": 1, "LOW": 2, "MEDIUM": 3, "HIGH": 4}
LEVEL_TITLES = {"NONE": "нет в наличии", "LAST": "последние штуки", "LOW": "мало", "MEDIUM": "средне",
                "HIGH": "много"}
# какие точки считаем магазинами: обычные и мини «Детского мира» (без пунктов выдачи и «Зоозавра»)
STORE_KINDS = ("DM", "MINI")


DetmirError = SiteError  # старое имя
__all__ = ["DetmirClient", "DetmirError", "FormatError", "LEVELS", "LEVEL_TITLES", "Product", "Store", "StoreStock"]


@dataclass(frozen=True)
class Product:
    id: str
    title: str
    article: str
    barcodes: tuple[str, ...]


@dataclass(frozen=True)
class Store:
    code: str
    title: str
    address: str
    metro: str
    kind: str


@dataclass(frozen=True)
class StoreStock:
    store: Store
    level: str  # NONE / LAST / LOW / MEDIUM / HIGH
    availability: str  # all_today — забрать сегодня, all — под заказ
    pickup_at: str  # collectedAt: когда можно забрать заказ (ISO с часовым поясом)


class DetmirClient(PoliteClient):
    """Не больше одного запроса одновременно, пауза 1–3 с между запросами, повторы с backoff."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None, **kw) -> None:
        super().__init__(API, HEADERS, transport=transport, **kw)

    async def _get_json(self, path: str, params: dict) -> object:
        return await self.request_json("GET", path, params=params)

    async def find_by_barcode(self, barcode: str) -> Product | None:
        data = await self._get_json("/v4/products", {"search": barcode, "limit": 5})
        if not isinstance(data, list):
            raise FormatError(f"поиск вернул не список: {str(data)[:200]}")
        for item in data:
            try:
                p = parse_product(item)
            except (KeyError, TypeError) as e:
                raise FormatError(f"неожиданный формат товара: {e!r}") from e
            if barcode in p.barcodes:
                return p
        return None

    async def get_stock(self, product_id: str, region_iso: str) -> list[StoreStock]:
        data = await self._get_json(
            f"/v4/products/{product_id}/delivery", {"filter": f"region.iso:{region_iso}"}
        )
        return parse_stock(data, product_id)


def parse_product(item: dict) -> Product:
    return Product(
        id=str(item["id"]),
        title=str(item.get("title") or ""),
        article=str(item.get("article") or ""),
        barcodes=tuple(str(b) for b in item.get("bar_codes") or ()),
    )


def parse_stock(data: object, product_id: str) -> list[StoreStock]:
    if not isinstance(data, dict) or not isinstance(data.get("stores"), list):
        raise FormatError(f"нет списка stores в ответе: {str(data)[:200]}")
    result = []
    for entry in data["stores"]:
        try:
            s = entry["store"]
            if s.get("type") != "store" or s.get("kind") not in STORE_KINDS:
                continue
            prods = [p for p in entry.get("products") or [] if str(p.get("skuId")) == str(product_id)]
            if not prods:
                continue
            level = str(prods[0].get("slvl") or "NONE").upper()
            if level not in LEVELS:
                raise FormatError(f"неизвестный уровень остатка slvl={level!r}")
            metro = ", ".join(m.get("title", "").strip() for m in s.get("metro") or [] if m.get("title"))
            result.append(
                StoreStock(
                    store=Store(
                        code=str(s["code"]),
                        title=str(s.get("title") or ""),
                        address=str(s.get("address") or ""),
                        metro=metro,
                        kind=str(s.get("kind") or ""),
                    ),
                    level=level,
                    availability=str(entry.get("availability") or ""),
                    pickup_at=str(entry.get("collectedAt") or ""),
                )
            )
        except (KeyError, TypeError) as e:
            raise FormatError(f"неожиданный формат магазина: {e!r}") from e
    return result
