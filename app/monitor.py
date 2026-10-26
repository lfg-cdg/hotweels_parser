"""Проверка остатков и поиск событий."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .auchan import AuchanClient
from .config import Config
from .db import Database, TrackedProduct
from .detmir import LEVELS, DetmirClient, Store, StoreStock
from .http import FormatError, SiteError

log = logging.getLogger(__name__)

APPEARED = "appeared"      # было NONE → стало в наличии (поставка в пустой магазин)
LEVEL_UP = "level_up"      # LAST → LOW/HIGH, LOW → HIGH
LEVEL_DOWN = "level_down"
SOLD_OUT = "sold_out"      # было в наличии → NONE
IN_TRANSIT = "in_transit"  # товара нет, но появилась дата самовывоза (раньше в магазин не везли)
QTY_UP = "qty_up"          # Ашан: точный остаток вырос (было больше 0)

STORES_REFRESH = timedelta(hours=24)  # как часто обновлять список магазинов Ашана

State = tuple[str, str]  # (уровень, дата самовывоза)


@dataclass(frozen=True)
class Event:
    kind: str
    store_code: str
    old: str
    new: str
    pickup_at: str = ""
    old_qty: int | None = None
    new_qty: int | None = None

    @property
    def delta(self) -> int:
        return (self.new_qty or 0) - (self.old_qty or 0)


def diff_states(prev: dict[str, State], curr: dict[str, State], baseline_done: bool) -> list[Event]:
    """prev/curr: код магазина → (уровень NONE/LAST/LOW/HIGH, дата самовывоза или '').

    Если базового снимка ещё нет — событий нет (иначе первая проверка выдаст «поставку» во все магазины).
    Если снимок есть, а пары товар/магазин в prev нет — считаем, что было NONE без даты.
    """
    if not baseline_done:
        return []
    events = []
    for code, (new, pickup) in curr.items():
        old, old_pickup = prev.get(code, ("NONE", ""))
        if old == new:
            if new == "NONE" and pickup and not old_pickup:
                events.append(Event(IN_TRANSIT, code, old, new, pickup))
            continue
        if old == "NONE":
            kind = APPEARED
        elif new == "NONE":
            kind = SOLD_OUT
        elif LEVELS[new] > LEVELS[old]:
            kind = LEVEL_UP
        else:
            kind = LEVEL_DOWN
        events.append(Event(kind, code, old, new, pickup))
    return events


def diff_qty(prev: dict[str, int], curr: dict[str, int], baseline_done: bool, min_increase: int = 1
             ) -> list[Event]:
    """Ашан: prev/curr — код магазина → количество. Магазина нет в prev — считаем, что было 0."""
    if not baseline_done:
        return []
    events = []
    for code, new in curr.items():
        old = prev.get(code, 0)
        if new == old:
            continue
        lvl_old, lvl_new = ("HIGH" if old > 0 else "NONE"), ("HIGH" if new > 0 else "NONE")
        if old == 0:
            kind = APPEARED
        elif new == 0:
            kind = SOLD_OUT
        elif new - old >= min_increase:
            kind = QTY_UP
        elif new > old:
            continue  # прирост меньше порога — не событие, но сохранится в состоянии
        else:
            kind = LEVEL_DOWN
        events.append(Event(kind, code, lvl_old, lvl_new, old_qty=old, new_qty=new))
    return events


@dataclass
class ProductReport:
    product: TrackedProduct
    ok: bool
    events: list[Event] = field(default_factory=list)
    stores: dict[str, Store] = field(default_factory=dict)
    states: dict[str, State] = field(default_factory=dict)
    baseline: bool = False  # это была первая (базовая) проверка
    error: str = ""

    qtys: dict[str, int] = field(default_factory=dict)  # Ашан: точные остатки

    @property
    def levels(self) -> dict[str, str]:
        if self.qtys:
            return {code: ("HIGH" if q > 0 else "NONE") for code, q in self.qtys.items()}
        return {code: lvl for code, (lvl, _) in self.states.items()}


def _error_text(e: SiteError) -> str:
    kind = "формат ответа изменился" if isinstance(e, FormatError) else "ошибка запроса"
    return f"{kind}: {e}"


class Monitor:
    def __init__(self, cfg: Config, db: Database, client: DetmirClient, auchan: AuchanClient | None = None) -> None:
        self.cfg, self.db, self.client, self.auchan = cfg, db, client, auchan
        self._lock = asyncio.Lock()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    async def check_all(self) -> list[ProductReport]:
        async with self._lock:
            # Сначала скачиваем всё: список магазинов пополняется из ответов по всем товарам.
            fetched: list[tuple[TrackedProduct, list[StoreStock]]] = []
            reports = []
            products = await self.db.list_products()
            for p in (p for p in products if p.network == "dm"):
                try:
                    stock = await self.client.get_stock(p.id, self.cfg.region_iso)
                except SiteError as e:
                    msg = _error_text(e)
                    log.error("Товар %s: %s", p.id, msg)
                    await self.db.log_check(p.id, ok=False, error=msg[:500])
                    reports.append(ProductReport(p, ok=False, error=msg))
                    continue
                await self.db.upsert_stores([s.store for s in stock])
                fetched.append((p, stock))
            for p, stock in fetched:
                reports.append(await self._process(p, stock))
            if self.auchan and self.cfg.auchan_enabled:
                for p in (p for p in products if p.network == "au"):
                    reports.append(await self._check_auchan(p))
            return reports

    async def auchan_stores(self) -> dict[str, Store]:
        """Магазины Ашана в городе (без исключённых); список обновляется раз в сутки."""
        assert self.auchan is not None
        refreshed = await self.db.stores_refreshed_at("au")
        stale = not refreshed or datetime.fromisoformat(refreshed) < datetime.now(timezone.utc) - STORES_REFRESH
        if stale:
            await self.db.upsert_stores(await self.auchan.list_stores(), network="au")
        stores = await self.db.stores(self.cfg.city_prefix, network="au")
        return {c: s for c, s in stores.items() if c not in self.cfg.auchan_exclude}

    async def _check_auchan(self, p: TrackedProduct) -> ProductReport:
        assert self.auchan is not None
        try:
            stores = await self.auchan_stores()
        except SiteError as e:
            msg = "Ашан, список магазинов — " + _error_text(e)
            log.error(msg)
            await self.db.log_check(p.id, ok=False, error=msg[:500])
            return ProductReport(p, ok=False, error=msg)

        curr: dict[str, int] = {}
        errors: list[str] = []
        for code in stores:
            try:
                curr[code] = await self.auchan.get_qty(p.article, code)
            except SiteError as e:
                errors.append(f"{stores[code].title}: {_error_text(e)}")
                log.error("Ашан %s, %s: %s", p.id, code, e)
        if not curr:
            msg = "Ашан: ни один магазин не ответил. " + "; ".join(errors[:3])
            await self.db.log_check(p.id, ok=False, error=msg[:500])
            return ProductReport(p, ok=False, error=msg)

        prev = await self.db.stock_qty(p.id)
        baseline_done = await self.db.has_baseline(p.id)
        events = diff_qty(prev, curr, baseline_done, self.cfg.auchan_min_increase)
        await self.db.save_qty(p.id, curr, [(e.store_code, e.old_qty or 0, e.new_qty or 0) for e in events])
        have = sum(1 for q in curr.values() if q > 0)
        await self.db.log_check(p.id, ok=True, stores_count=have,
                                error=("; ".join(errors)[:500] if errors else None))
        log.info("Ашан %s: в наличии в %d из %d магазинов, всего %d шт, событий %d%s", p.id, have, len(curr),
                 sum(curr.values()), len(events), " (базовый снимок)" if not baseline_done else "")
        # если часть магазинов не ответила — проверка удалась, но об ошибке сообщим
        error = f"Ашан: не ответили {len(errors)} из {len(stores)} магазинов. " + "; ".join(errors[:3]) if errors else ""
        return ProductReport(p, ok=True, events=events, stores=stores, qtys=curr,
                             baseline=not baseline_done, error=error)

    async def _process(self, p: TrackedProduct, stock: list[StoreStock]) -> ProductReport:
        in_city = [s for s in stock if s.store.address.startswith(self.cfg.city_prefix)]
        known = await self.db.stores(self.cfg.city_prefix, network="dm")
        curr: dict[str, State] = {code: ("NONE", "") for code in known}
        curr.update({s.store.code: (s.level, s.pickup_at) for s in in_city})

        prev = await self.db.stock_full(p.id)
        baseline_done = await self.db.has_baseline(p.id)
        events = diff_states(prev, curr, baseline_done)

        await self.db.save_state(
            p.id, curr, [(e.store_code, e.old, e.new, e.pickup_at) for e in events if e.kind != IN_TRANSIT]
        )
        have = sum(1 for lvl, _ in curr.values() if lvl != "NONE")
        await self.db.log_check(p.id, ok=True, stores_count=have)
        log.info("Товар %s: в наличии в %d из %d магазинов, событий %d%s",
                 p.id, have, len(curr), len(events), " (базовый снимок)" if not baseline_done else "")
        return ProductReport(p, ok=True, events=events, stores=known, states=curr, baseline=not baseline_done)
