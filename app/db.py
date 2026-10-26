"""Хранилище SQLite."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from .detmir import LEVELS, Product, Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id        TEXT PRIMARY KEY,
    barcode   TEXT NOT NULL,          -- штрихкод, по которому добавили
    barcodes  TEXT NOT NULL,          -- все штрихкоды товара (JSON)
    title     TEXT NOT NULL,
    article   TEXT NOT NULL,
    added_at  TEXT NOT NULL,
    network   TEXT NOT NULL DEFAULT 'dm'  -- dm = Детский мир, au = Ашан
);
CREATE TABLE IF NOT EXISTS stores (
    code       TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    address    TEXT NOT NULL,
    metro      TEXT NOT NULL,
    kind       TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    network    TEXT NOT NULL DEFAULT 'dm'
);
CREATE TABLE IF NOT EXISTS selected_stores (code TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- текущий уровень по паре товар/магазин (NONE = товара нет)
CREATE TABLE IF NOT EXISTS stock_state (
    product_id TEXT NOT NULL,
    store_code TEXT NOT NULL,
    level      TEXT NOT NULL,
    since      TEXT NOT NULL,
    pickup_at  TEXT NOT NULL DEFAULT '',  -- когда можно забрать заказ ('' — не везут)
    qty        INTEGER,                   -- точное количество (Ашан), NULL — неизвестно
    PRIMARY KEY (product_id, store_code)
);
-- история: только изменения уровня
CREATE TABLE IF NOT EXISTS stock_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id   TEXT NOT NULL,
    store_code   TEXT NOT NULL,
    old_level    TEXT NOT NULL,
    new_level    TEXT NOT NULL,
    collected_at TEXT NOT NULL,           -- дата самовывоза на момент изменения
    checked_at   TEXT NOT NULL,
    old_qty      INTEGER,
    new_qty      INTEGER
);
CREATE INDEX IF NOT EXISTS ix_history_product ON stock_history(product_id, checked_at);
-- каждая проверка (в том числе неудачная)
CREATE TABLE IF NOT EXISTS checks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id   TEXT NOT NULL,
    checked_at   TEXT NOT NULL,
    collected_at TEXT,
    stores_count INTEGER,
    ok           INTEGER NOT NULL,
    error        TEXT
);
-- ручной журнал точных остатков со сканера в магазине
CREATE TABLE IF NOT EXISTS manual_counts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  TEXT NOT NULL,
    store_code  TEXT NOT NULL,
    qty         INTEGER NOT NULL,
    delta       INTEGER,
    is_new_case INTEGER NOT NULL DEFAULT 0,
    logged_at   TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


NETWORK_TITLES = {"dm": "Детский мир", "au": "Ашан"}


@dataclass(frozen=True)
class TrackedProduct:
    id: str
    barcode: str
    barcodes: tuple[str, ...]
    title: str
    article: str
    network: str = "dm"

    @property
    def network_title(self) -> str:
        return NETWORK_TITLES.get(self.network, self.network)


@dataclass(frozen=True)
class HistoryRow:
    store_code: str
    old_level: str
    new_level: str
    checked_at: str
    collected_at: str
    old_qty: int | None = None
    new_qty: int | None = None


@dataclass(frozen=True)
class ManualRow:
    store_code: str
    qty: int
    delta: int | None
    is_new_case: bool
    logged_at: str


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        # миграции баз от прошлых версий бота
        for table, col, ddl in (
            ("stock_state", "pickup_at", "TEXT NOT NULL DEFAULT ''"),
            ("stock_state", "qty", "INTEGER"),
            ("stock_history", "old_qty", "INTEGER"),
            ("stock_history", "new_qty", "INTEGER"),
            ("products", "network", "TEXT NOT NULL DEFAULT 'dm'"),
            ("stores", "network", "TEXT NOT NULL DEFAULT 'dm'"),
        ):
            cols = {r["name"] for r in await self._db.execute_fetchall(f"PRAGMA table_info({table})")}
            if col not in cols:
                await self._db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "connect() не вызван"
        return self._db

    # --- товары ---
    async def add_product(self, p: Product, barcode: str, network: str = "dm") -> bool:
        cur = await self.db.execute(
            "INSERT OR IGNORE INTO products(id, barcode, barcodes, title, article, added_at, network)"
            " VALUES (?,?,?,?,?,?,?)",
            (p.id, barcode, json.dumps(list(p.barcodes)), p.title, p.article, now_iso(), network),
        )
        await self.db.commit()
        return cur.rowcount > 0

    async def remove_product(self, product_id: str) -> None:
        for table in ("products", "stock_state"):
            col = "id" if table == "products" else "product_id"
            await self.db.execute(f"DELETE FROM {table} WHERE {col}=?", (product_id,))
        await self.db.commit()

    async def list_products(self) -> list[TrackedProduct]:
        rows = await self.db.execute_fetchall("SELECT * FROM products ORDER BY added_at, network")
        return [
            TrackedProduct(r["id"], r["barcode"], tuple(json.loads(r["barcodes"])), r["title"], r["article"],
                           r["network"])
            for r in rows
        ]

    async def find_products(self, code: str) -> list[TrackedProduct]:
        """Отслеживаемые товары (по одному на сеть) по любому штрихкоду, id или артикулу."""
        code = code.strip()
        norm = code.lstrip("0")
        return [
            p for p in await self.list_products()
            if code in (p.id, p.barcode, p.article) or norm in {b.lstrip("0") for b in (*p.barcodes, p.barcode)}
        ]

    async def find_product(self, code: str) -> TrackedProduct | None:
        found = await self.find_products(code)
        return found[0] if found else None

    # --- магазины ---
    async def upsert_stores(self, stores: list[Store], network: str = "dm") -> None:
        ts = now_iso()
        await self.db.executemany(
            """INSERT INTO stores(code, title, address, metro, kind, first_seen, last_seen, network)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(code) DO UPDATE SET title=excluded.title, address=excluded.address,
                   metro=excluded.metro, kind=excluded.kind, last_seen=excluded.last_seen""",
            [(s.code, s.title, s.address, s.metro, s.kind, ts, ts, network) for s in stores],
        )
        await self.db.commit()

    async def stores_refreshed_at(self, network: str) -> str | None:
        rows = await self.db.execute_fetchall("SELECT MAX(last_seen) AS t FROM stores WHERE network=?", (network,))
        return rows[0]["t"] if rows else None

    async def stores(self, city_prefix: str = "", network: str | None = None) -> dict[str, Store]:
        rows = await self.db.execute_fetchall(
            "SELECT * FROM stores WHERE address LIKE ? AND (? IS NULL OR network=?) ORDER BY title",
            (city_prefix + "%", network, network),
        )
        return {r["code"]: Store(r["code"], r["title"], r["address"], r["metro"], r["kind"]) for r in rows}

    async def get_store(self, code: str) -> Store | None:
        rows = await self.db.execute_fetchall("SELECT * FROM stores WHERE code=?", (code,))
        if not rows:
            return None
        r = rows[0]
        return Store(r["code"], r["title"], r["address"], r["metro"], r["kind"])

    async def search_stores(self, query: str, city_prefix: str = "", exclude: frozenset[str] = frozenset()
                            ) -> list[Store]:
        q = query.strip().lower()
        return [
            s for s in (await self.stores(city_prefix)).values()
            if s.code not in exclude
            and (q == s.code or q in s.title.lower() or q in s.address.lower() or q in s.metro.lower())
        ]

    # --- выбор магазинов ---
    async def stores_mode(self) -> str:
        rows = await self.db.execute_fetchall("SELECT value FROM settings WHERE key='stores_mode'")
        return rows[0]["value"] if rows else "all"

    async def set_stores_mode(self, mode: str) -> None:
        await self.db.execute(
            "INSERT INTO settings(key, value) VALUES ('stores_mode', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (mode,),
        )
        await self.db.commit()

    async def selected_stores(self) -> set[str]:
        rows = await self.db.execute_fetchall("SELECT code FROM selected_stores")
        return {r["code"] for r in rows}

    async def select_store(self, code: str, on: bool) -> None:
        if on:
            await self.db.execute("INSERT OR IGNORE INTO selected_stores(code) VALUES (?)", (code,))
        else:
            await self.db.execute("DELETE FROM selected_stores WHERE code=?", (code,))
        await self.db.commit()

    # --- остатки ---
    async def has_baseline(self, product_id: str) -> bool:
        rows = await self.db.execute_fetchall(
            "SELECT 1 FROM checks WHERE product_id=? AND ok=1 LIMIT 1", (product_id,)
        )
        return bool(rows)

    async def stock_state(self, product_id: str) -> dict[str, str]:
        rows = await self.db.execute_fetchall(
            "SELECT store_code, level FROM stock_state WHERE product_id=?", (product_id,)
        )
        return {r["store_code"]: r["level"] for r in rows}

    async def stock_qty(self, product_id: str) -> dict[str, int]:
        """код магазина → точное количество (только где оно известно)."""
        rows = await self.db.execute_fetchall(
            "SELECT store_code, qty FROM stock_state WHERE product_id=? AND qty IS NOT NULL", (product_id,)
        )
        return {r["store_code"]: r["qty"] for r in rows}

    async def save_qty(self, product_id: str, qtys: dict[str, int], changes: list[tuple[str, int, int]]) -> None:
        """qtys — количество по магазинам; changes — (магазин, было, стало) для истории."""
        ts = now_iso()
        prev = await self.stock_qty(product_id)
        lvl = lambda q: "HIGH" if q > 0 else "NONE"  # noqa: E731 — уровень для общих отчётов
        await self.db.executemany(
            """INSERT INTO stock_state(product_id, store_code, level, since, qty) VALUES (?,?,?,?,?)
               ON CONFLICT(product_id, store_code) DO UPDATE SET level=excluded.level, qty=excluded.qty,
                   since=CASE WHEN stock_state.qty=excluded.qty THEN stock_state.since ELSE excluded.since END""",
            [(product_id, code, lvl(q), ts, q) for code, q in qtys.items() if prev.get(code) != q],
        )
        await self.db.executemany(
            "INSERT INTO stock_history(product_id, store_code, old_level, new_level, collected_at, checked_at,"
            " old_qty, new_qty) VALUES (?,?,?,?,?,?,?,?)",
            [(product_id, code, lvl(old), lvl(new), "", ts, old, new) for code, old, new in changes],
        )
        await self.db.commit()

    async def stock_full(self, product_id: str) -> dict[str, tuple[str, str]]:
        """код магазина → (уровень, дата самовывоза)."""
        rows = await self.db.execute_fetchall(
            "SELECT store_code, level, pickup_at FROM stock_state WHERE product_id=?", (product_id,)
        )
        return {r["store_code"]: (r["level"], r["pickup_at"]) for r in rows}

    async def save_state(
        self, product_id: str, states: dict[str, tuple[str, str]], changes: list[tuple[str, str, str, str]]
    ) -> None:
        """states — (уровень, дата самовывоза) по каждому магазину;
        changes — (магазин, старый уровень, новый уровень, дата самовывоза) для истории."""
        ts = now_iso()
        prev = await self.stock_full(product_id)
        await self.db.executemany(
            """INSERT INTO stock_state(product_id, store_code, level, since, pickup_at) VALUES (?,?,?,?,?)
               ON CONFLICT(product_id, store_code) DO UPDATE SET level=excluded.level, pickup_at=excluded.pickup_at,
                   since=CASE WHEN stock_state.level=excluded.level THEN stock_state.since ELSE excluded.since END""",
            [(product_id, code, lvl, ts, pickup) for code, (lvl, pickup) in states.items()
             if prev.get(code) != (lvl, pickup)],
        )
        await self.db.executemany(
            "INSERT INTO stock_history(product_id, store_code, old_level, new_level, collected_at, checked_at)"
            " VALUES (?,?,?,?,?,?)",
            [(product_id, code, old, new, pickup, ts) for code, old, new, pickup in changes],
        )
        await self.db.commit()

    async def log_check(
        self, product_id: str, ok: bool, stores_count: int | None = None, error: str | None = None,
    ) -> None:
        await self.db.execute(
            "INSERT INTO checks(product_id, checked_at, stores_count, ok, error) VALUES (?,?,?,?,?)",
            (product_id, now_iso(), stores_count, int(ok), error),
        )
        await self.db.commit()

    async def history(self, product_id: str, only_increase: bool = True, limit: int = 200) -> list[HistoryRow]:
        rows = await self.db.execute_fetchall(
            "SELECT * FROM stock_history WHERE product_id=? ORDER BY checked_at DESC, id DESC LIMIT ?",
            (product_id, limit),
        )
        out = [HistoryRow(r["store_code"], r["old_level"], r["new_level"], r["checked_at"], r["collected_at"],
                          r["old_qty"], r["new_qty"]) for r in rows]
        if only_increase:
            out = [h for h in out
                   if (h.new_qty > h.old_qty if h.new_qty is not None and h.old_qty is not None
                       else LEVELS[h.new_level] > LEVELS[h.old_level])]
        return out

    # --- ручной журнал ---
    async def add_manual_count(self, product_id: str, store_code: str, qty: int, threshold: int) -> ManualRow:
        rows = await self.db.execute_fetchall(
            "SELECT qty FROM manual_counts WHERE product_id=? AND store_code=? ORDER BY id DESC LIMIT 1",
            (product_id, store_code),
        )
        delta = qty - rows[0]["qty"] if rows else None
        is_case = delta is not None and delta >= threshold
        ts = now_iso()
        await self.db.execute(
            "INSERT INTO manual_counts(product_id, store_code, qty, delta, is_new_case, logged_at) VALUES (?,?,?,?,?,?)",
            (product_id, store_code, qty, delta, int(is_case), ts),
        )
        await self.db.commit()
        return ManualRow(store_code, qty, delta, is_case, ts)

    async def manual_history(self, product_id: str, limit: int = 200) -> list[ManualRow]:
        rows = await self.db.execute_fetchall(
            "SELECT * FROM manual_counts WHERE product_id=? ORDER BY id DESC LIMIT ?", (product_id, limit)
        )
        return [ManualRow(r["store_code"], r["qty"], r["delta"], bool(r["is_new_case"]), r["logged_at"])
                for r in rows]
