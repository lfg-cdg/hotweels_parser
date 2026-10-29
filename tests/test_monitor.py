import json

import httpx
import pytest

from app.config import Config
from app.db import Database
from app.detmir import DetmirClient, Product
from app.monitor import APPEARED, IN_TRANSIT, LEVEL_DOWN, LEVEL_UP, SOLD_OUT, Monitor, diff_states

PID = "143502"


def store(code, level, address="Москва, ул. Тестовая, 1", pickup="2026-10-26T15:00:00+03:00", kind="DM"):
    """level=None — товара нет, но можно заказать с самовывозом в дату pickup."""
    prod = {"skuId": PID, "qty": 1, "noQty": 0}
    if level:
        prod["slvl"] = level
    return {
        "store": {"code": code, "title": f"ТЦ {code}", "address": address, "kind": kind, "type": "store",
                  "metro": [{"title": "Тестовая"}], "location": {"lat": 0, "lon": 0}},
        "availability": "all_today" if level else "all",
        "products": [prod],
        "collectedAt": pickup,
    }


class FakeSite:
    """Подменяет api.detmir.ru: отдаёт заранее заданный ответ."""

    def __init__(self):
        self.payload: object = {"stores": []}
        self.status = 200
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.status != 200:
            return httpx.Response(self.status, text="error")
        return httpx.Response(200, text=json.dumps(self.payload))


def make_cfg(tmp_path):
    return Config("t", 1, 20, 30, "RU-MOW", "Москва", 50, True, tmp_path / "db.sqlite", "INFO")


@pytest.fixture
async def env(tmp_path):
    site = FakeSite()
    client = DetmirClient(min_pause=0, max_pause=0, retries=3, backoff_base=0,
                          transport=httpx.MockTransport(site))
    db = Database(":memory:")
    await db.connect()
    await db.add_product(Product(PID, "Hot Wheels", "5785", ("0074299057854",)), "0074299057854")
    yield site, db, Monitor(make_cfg(tmp_path), db, client)
    await client.close()
    await db.close()


def test_diff_no_events_without_baseline():
    assert diff_states({}, {"1": ("HIGH", "x")}, baseline_done=False) == []


def test_diff_kinds():
    d = "2026-10-28T00:00:00+03:00"
    prev = {"a": ("NONE", d), "b": ("LAST", ""), "c": ("HIGH", ""), "d": ("HIGH", ""), "e": ("LOW", ""),
            "g": ("NONE", ""), "h": ("NONE", d)}
    curr = {"a": ("HIGH", ""), "b": ("HIGH", ""), "c": ("LOW", ""), "d": ("NONE", d), "e": ("LOW", ""),
            "f": ("LAST", ""), "g": ("NONE", d), "h": ("NONE", "2026-10-27T00:00:00+03:00")}
    ev = {e.store_code: e.kind for e in diff_states(prev, curr, baseline_done=True)}
    # h: дата сдвинулась — это не событие (иначе спам каждый день)
    assert ev == {"a": APPEARED, "b": LEVEL_UP, "c": LEVEL_DOWN, "d": SOLD_OUT, "f": APPEARED, "g": IN_TRANSIT}


async def test_full_cycle(env):
    site, db, mon = env
    # 1) базовый снимок: магазин 1 в наличии, 2 — мало, 3 — в Подмосковье, 4 — «Зоозавр», 5 — пункт выдачи
    pp = store("5", None)
    pp["store"]["type"] = "pickup_point"
    site.payload = {"stores": [store("1", "HIGH"), store("2", "LOW"), store("3", "HIGH", "Реутов, ул. 1"),
                               store("4", None, kind="ZOO"), pp]}
    [r] = await mon.check_all()
    assert r.ok and r.baseline and r.events == []
    assert r.levels == {"1": "HIGH", "2": "LOW"}

    # 2) магазин 1 пропал (раскупили), магазин 2 вырос до HIGH
    site.payload = {"stores": [store("2", "HIGH")]}
    [r] = await mon.check_all()
    assert {(e.store_code, e.kind) for e in r.events} == {("1", SOLD_OUT), ("2", LEVEL_UP)}

    # 3) магазин 1 снова появился — поставка
    site.payload = {"stores": [store("1", "LAST"), store("2", "HIGH")]}
    [r] = await mon.check_all()
    assert [(e.store_code, e.kind, e.old, e.new) for e in r.events] == [("1", APPEARED, "NONE", "LAST")]

    # 4) новый магазин, которого мы раньше не видели, — тоже поставка (товара там не было)
    site.payload = {"stores": [store("1", "LAST"), store("2", "HIGH"), store("9", "MEDIUM")]}
    [r] = await mon.check_all()
    assert [(e.store_code, e.kind) for e in r.events] == [("9", APPEARED)]

    hist = await db.history(PID)
    assert {(h.store_code, h.new_level) for h in hist} == {("2", "HIGH"), ("1", "LAST"), ("9", "MEDIUM")}

    # 5) магазин 1 раскупили, но туда везут под заказ — «закончился», не «в пути»
    d = "2026-10-28T00:00:00+03:00"
    site.payload = {"stores": [store("1", None, pickup=d), store("2", "HIGH"), store("9", "MEDIUM")]}
    [r] = await mon.check_all()
    assert [(e.store_code, e.kind, e.pickup_at) for e in r.events] == [("1", SOLD_OUT, d)]
    assert r.states["1"] == ("NONE", d)

    # 6) магазин 9 раскупили, и туда не везут; потом начали везти — «в пути»
    site.payload = {"stores": [store("1", None, pickup=d), store("2", "HIGH")]}
    [r] = await mon.check_all()
    assert [(e.store_code, e.kind) for e in r.events] == [("9", SOLD_OUT)]
    site.payload = {"stores": [store("1", None, pickup=d), store("2", "HIGH"), store("9", None, pickup=d)]}
    [r] = await mon.check_all()
    assert [(e.store_code, e.kind, e.pickup_at) for e in r.events] == [("9", IN_TRANSIT, d)]
    assert (await db.stock_full(PID))["9"] == ("NONE", d)


async def test_errors_do_not_crash(env):
    site, db, mon = env
    site.status = 503
    [r] = await mon.check_all()
    assert not r.ok and "503" in r.error
    assert site.calls == 3  # повторы

    site.status = 404  # 4xx не повторяем
    site.calls = 0
    [r] = await mon.check_all()
    assert not r.ok and site.calls == 1

    site.status = 200
    site.payload = {"something": "else"}
    [r] = await mon.check_all()
    assert not r.ok and "формат" in r.error

    site.payload = {"stores": [store("1", "SUPER")]}
    [r] = await mon.check_all()
    assert not r.ok and "slvl" in r.error

    # неудачные проверки не считаются базовым снимком
    assert not await db.has_baseline(PID)


async def test_manual_counts(env):
    _, db, _ = env
    r1 = await db.add_manual_count(PID, "157", 130, threshold=50)
    assert r1.delta is None and not r1.is_new_case
    r2 = await db.add_manual_count(PID, "157", 120, threshold=50)
    assert r2.delta == -10 and not r2.is_new_case
    r3 = await db.add_manual_count(PID, "157", 195, threshold=50)
    assert r3.delta == 75 and r3.is_new_case
    assert [m.qty for m in await db.manual_history(PID)] == [195, 120, 130]


async def test_find_product_by_any_code(env):
    _, db, _ = env
    for code in ("0074299057854", PID, "5785"):
        assert (await db.find_product(code)).id == PID
    assert await db.find_product("000") is None


async def test_store_from_other_product_counts_as_none(env):
    site, db, mon = env
    await db.add_product(Product("999", "Другой", "C4982", ("0027084120134",)), "0027084120134")

    def by_product(request):
        pid = request.url.path.split("/")[3]
        stores = [store("1", "HIGH")] if pid == PID else [store("1", "HIGH"), store("2", "HIGH")]
        for s in stores:
            s["products"][0]["skuId"] = pid
        return httpx.Response(200, text=json.dumps({"stores": stores}))

    mon.client._client._transport = httpx.MockTransport(by_product)
    reports = {r.product.id: r for r in await mon.check_all()}
    assert reports[PID].levels == {"1": "HIGH", "2": "NONE"}
    assert reports["999"].levels == {"1": "HIGH", "2": "HIGH"}


async def test_migrates_old_database(tmp_path):
    import aiosqlite
    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as old:
        await old.execute("CREATE TABLE stock_state (product_id TEXT NOT NULL, store_code TEXT NOT NULL,"
                          " level TEXT NOT NULL, since TEXT NOT NULL, PRIMARY KEY (product_id, store_code))")
        await old.execute("INSERT INTO stock_state VALUES ('1', '157', 'HIGH', 'x')")
        await old.commit()
    db = Database(path)
    await db.connect()
    assert await db.stock_full("1") == {"157": ("HIGH", "")}
    await db.close()


def test_diff_qty():
    from app.monitor import QTY_UP, diff_qty
    assert diff_qty({}, {"a": 5}, baseline_done=False) == []
    prev = {"a": 0, "b": 35, "c": 46, "d": 10, "e": 7}
    curr = {"a": 72, "b": 107, "c": 47, "d": 0, "e": 3, "f": 9}
    ev = {e.store_code: (e.kind, e.delta) for e in diff_qty(prev, curr, True, min_increase=1)}
    assert ev == {"a": (APPEARED, 72), "b": (QTY_UP, 72), "c": (QTY_UP, 1), "d": (SOLD_OUT, -10),
                  "e": (LEVEL_DOWN, -4), "f": (APPEARED, 9)}
    # порог прироста: +1 при пороге 5 — не событие
    ev = {e.store_code for e in diff_qty(prev, curr, True, min_increase=5)}
    assert "c" not in ev and "b" in ev


async def test_migrates_v2_database(tmp_path):
    """База второй версии (без network/qty) открывается и работает."""
    import aiosqlite
    path = tmp_path / "v2.db"
    async with aiosqlite.connect(path) as old:
        await old.executescript("""
            CREATE TABLE products (id TEXT PRIMARY KEY, barcode TEXT NOT NULL, barcodes TEXT NOT NULL,
                title TEXT NOT NULL, article TEXT NOT NULL, added_at TEXT NOT NULL);
            CREATE TABLE stores (code TEXT PRIMARY KEY, title TEXT NOT NULL, address TEXT NOT NULL,
                metro TEXT NOT NULL, kind TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
            CREATE TABLE stock_history (id INTEGER PRIMARY KEY AUTOINCREMENT, product_id TEXT NOT NULL,
                store_code TEXT NOT NULL, old_level TEXT NOT NULL, new_level TEXT NOT NULL,
                collected_at TEXT NOT NULL, checked_at TEXT NOT NULL);
            INSERT INTO products VALUES ('143502', '0074299057854', '["0074299057854"]', 'HW', '5785', 'x');
            INSERT INTO stores VALUES ('157', 'Гагаринский', 'Москва, Вавилова', '', 'DM', 'x', 'x');
            INSERT INTO stock_history VALUES (1, '143502', '157', 'NONE', 'HIGH', '', 'x');
        """)
    db = Database(path)
    await db.connect()
    [p] = await db.list_products()
    assert p.network == "dm"
    assert list(await db.stores("Москва", network="dm")) == ["157"]
    assert [h.new_level for h in await db.history("143502")] == ["HIGH"]
    await db.close()


def test_diff_medium_level():
    prev = {"a": ("LOW", ""), "b": ("MEDIUM", ""), "c": ("HIGH", "")}
    curr = {"a": ("MEDIUM", ""), "b": ("HIGH", ""), "c": ("MEDIUM", "")}
    ev = {e.store_code: e.kind for e in diff_states(prev, curr, baseline_done=True)}
    assert ev == {"a": LEVEL_UP, "b": LEVEL_UP, "c": LEVEL_DOWN}
