"""Клиент API мобильного приложения «Мой АШАН» (mobile.auchan.ru).

Сайт auchan.ru закрыт антибот-защитой, поэтому используются открытые запросы приложения:
  GET  /api/stores                                         — список магазинов
  POST /ecom/catalog/product-by-ean {ean, merchant_id}     — найти товар по штрихкоду
  GET  /ecom/catalog/product-detail?gima_id=&merchantId=   — карточка товара с остатком в магазине:
       "stock": {"qty": 35, "storeId": 701, "not_available": false}

merchant_id / merchantId — это external_id магазина из /api/stores (например, 701).
Остаток — по учёту магазина, приложение иногда его не показывает, но сервер отдаёт.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from .detmir import Product, Store
from .http import FormatError, PoliteClient, SiteError

log = logging.getLogger(__name__)

API = "https://mobile.auchan.ru"
HEADERS = {
    "User-Agent": "okhttp/4.12.0",
    "Accept": "application/json",
    "Accept-Language": "ru-RU,ru;q=0.9",
}
PREFIX = "au:"  # коды магазинов и товаров Ашана в базе: au:701, au:527760


def store_code(external_id: str) -> str:
    return PREFIX + str(external_id)


def external_id(code: str) -> str:
    return code.removeprefix(PREFIX)


def normalize_address(addr: str) -> str:
    """«г. Москва, ул. …» → «Москва, ул. …» (как у «Детского мира», чтобы работал фильтр CITY_PREFIX)."""
    addr = " ".join(addr.split())
    for p in ("г. ", "г.", "г "):
        if addr.startswith(p):
            return addr[len(p):].lstrip()
    return addr


@dataclass(frozen=True)
class StoreQty:
    code: str  # au:701
    qty: int


class AuchanClient(PoliteClient):
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None, **kw) -> None:
        super().__init__(API, HEADERS, transport=transport, **kw)

    async def list_stores(self) -> list[Store]:
        data = await self.request_json("GET", "/api/stores")
        if not isinstance(data, list):
            raise FormatError(f"список магазинов Ашана — не список: {str(data)[:200]}")
        out = []
        for s in data:
            try:
                out.append(Store(
                    code=store_code(s["external_id"]),
                    title=str(s.get("name") or ""),
                    address=normalize_address(str(s.get("address") or "")),
                    metro="",
                    kind=str(s.get("store_format") or ""),
                ))
            except (KeyError, TypeError) as e:
                raise FormatError(f"неожиданный формат магазина Ашана: {e!r}") from e
        return out

    async def find_by_barcode(self, barcode: str, merchant: str) -> Product | None:
        ean = barcode.lstrip("0")
        if not ean.isdigit():
            return None
        try:
            data = await self.request_json("POST", "/ecom/catalog/product-by-ean",
                                           json_body={"ean": int(ean), "merchant_id": int(merchant)})
        except SiteError as e:
            if e.status == 404:  # ProductNotFound
                return None
            raise
        item = data.get("data") if isinstance(data, dict) else None
        if not isinstance(item, dict) or "gima_id" not in item:
            raise FormatError(f"неожиданный ответ поиска по штрихкоду: {str(data)[:200]}")
        gima = str(item["gima_id"])
        title = str(item.get("name") or "")
        try:  # в карточке название понятнее («Машинка Hot Wheels 1:64…», а не «МАШИНКА HW 5785»)
            detail = await self._detail(gima, merchant)
            title = str(detail.get("title") or title)
        except SiteError:
            pass
        return Product(id=PREFIX + gima, title=title, article=gima,
                       barcodes=tuple(dict.fromkeys((barcode, str(item.get("ean") or ean)))))

    async def _detail(self, gima: str, merchant: str) -> dict:
        data = await self.request_json("GET", "/ecom/catalog/product-detail",
                                       params={"gima_id": gima, "merchantId": merchant})
        if not isinstance(data, dict):
            raise FormatError(f"карточка товара — не объект: {str(data)[:200]}")
        return data

    async def get_qty(self, gima: str, code: str) -> int:
        data = await self._detail(gima, external_id(code))
        stock = data.get("stock")
        if not isinstance(stock, dict) or "qty" not in stock:
            raise FormatError(f"в карточке нет stock.qty: {str(data)[:200]}")
        try:
            qty = int(stock["qty"])
        except (TypeError, ValueError) as e:
            raise FormatError(f"stock.qty не число: {stock!r}") from e
        return max(qty, 0)
