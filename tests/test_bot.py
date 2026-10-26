"""Сценарий работы бота целиком: Telegram, detmir.ru и API Ашана подменены."""
import json
from datetime import datetime

import httpx
import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app.auchan import AuchanClient
from app.bot import build
from app.config import Config
from app.db import Database
from app.detmir import DetmirClient

ADMIN = 777
PID = "143502"
GIMA = "527760"
BARCODE = "0074299057854"


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage):
            return Message(message_id=len(self.calls), date=datetime.now(),
                           chat=Chat(id=method.chat_id, type="private"), text=method.text)
        return True

    async def stream_content(self, *a, **kw):  # pragma: no cover
        yield b""

    async def close(self):
        pass

    def texts(self):
        return [c.text for c in self.calls if isinstance(c, SendMessage)]


def store(code, title, level, pickup="2026-10-26T15:00:00+03:00"):
    prod = {"skuId": PID, "qty": 1, "noQty": 0}
    if level:
        prod["slvl"] = level
    return {"store": {"code": code, "title": title, "address": f"Москва, {title}", "kind": "DM", "type": "store",
                      "metro": []},
            "availability": "all_today" if level else "all", "collectedAt": pickup, "products": [prod]}


class DetmirSite:
    def __init__(self):
        self.stores = [store("157", "ТРЦ «Гагаринский»", "HIGH"), store("2171", "ТРЦ «Колумбус»", "HIGH")]

    def __call__(self, req: httpx.Request):
        if req.url.path == "/v4/products":
            items = [{"id": PID, "title": "Автомобиль Hot Wheels в ассортименте", "article": "5785/DTV55",
                      "bar_codes": [BARCODE]}] if req.url.params["search"] == BARCODE else []
            return httpx.Response(200, json=items)
        return httpx.Response(200, text=json.dumps({"stores": self.stores}))


class AuchanSite:
    STORES = [
        {"external_id": "701", "name": "АШАН Сити Севастопольский", "address": "г. Москва, Севастопольский пр-т, 11Е",
         "store_format": "Гипермаркет"},
        {"external_id": "512", "name": "АШАН Академика Анохина", "address": "г. Москва, ул. Академика Анохина, 2",
         "store_format": "Супермаркет"},
        {"external_id": "009", "name": "АШАН Европарк", "address": "г. Москва, Рублёвское ш., 62",
         "store_format": ""},
        {"external_id": "712", "name": "АШАН Сити Подольск", "address": "МО, г. Подольск, ул. Серпуховская, 45",
         "store_format": "Гипермаркет"},
    ]

    def __init__(self):
        self.qty = {"701": 35, "512": 46, "009": 3750, "712": 10}
        self.detail_calls = []
        self.fail = False

    def __call__(self, req: httpx.Request):
        if self.fail:
            return httpx.Response(500, text="oops")
        if req.url.path == "/api/stores":
            return httpx.Response(200, json=self.STORES)
        if req.url.path == "/ecom/catalog/product-by-ean":
            body = json.loads(req.content)
            if str(body["ean"]) != BARCODE.lstrip("0"):
                return httpx.Response(404, json={"data": None, "errors": [{"code": "ProductNotFound"}]})
            return httpx.Response(200, json={"data": {"ean": body["ean"], "gima_id": int(GIMA),
                                                      "name": "МАШИНКА HW 5785"}})
        if req.url.path == "/ecom/catalog/product-detail":
            m = req.url.params["merchantId"]
            self.detail_calls.append(m)
            return httpx.Response(200, json={"title": "Машинка Hot Wheels 1:64, 1 шт в ассортименте",
                                             "stock": {"qty": self.qty[m], "storeId": int(m),
                                                       "not_available": self.qty[m] == 0}})
        return httpx.Response(404)


@pytest.fixture
async def world(tmp_path):
    session = RecordingSession()
    bot = Bot("42:TEST", session=session)
    dm, au = DetmirSite(), AuchanSite()
    client = DetmirClient(min_pause=0, max_pause=0, transport=httpx.MockTransport(dm))
    auchan = AuchanClient(min_pause=0, max_pause=0, retries=2, backoff_base=0, transport=httpx.MockTransport(au))
    db = Database(":memory:")
    await db.connect()
    cfg = Config("42:TEST", ADMIN, 20, 30, "RU-MOW", "Москва", 50, True, tmp_path / "x.db", "INFO")
    app, dp = build(cfg, db, client, bot, auchan)
    n = {"id": 0}

    async def say(text, chat_id=ADMIN):
        n["id"] += 1
        msg = Message(message_id=n["id"], date=datetime.now(), chat=Chat(id=chat_id, type="private"),
                      from_user=User(id=chat_id, is_bot=False, first_name="u"), text=text)
        session.calls.clear()
        await dp.feed_update(bot, Update(update_id=n["id"], message=msg))
        return session.texts()

    async def click(data, markup=None):
        n["id"] += 1
        msg = Message(message_id=1, date=datetime.now(), chat=Chat(id=ADMIN, type="private"), text="x",
                      reply_markup=markup)
        cb = CallbackQuery(id=str(n["id"]), from_user=User(id=ADMIN, is_bot=False, first_name="u"),
                           chat_instance="c", message=msg, data=data)
        session.calls.clear()
        await dp.feed_update(bot, Update(update_id=n["id"], callback_query=cb))
        return session.calls

    yield app, dm, au, say, click, session
    await client.close()
    await auchan.close()
    await db.close()


def joined(out):
    return "\n".join(out)


async def test_add_and_auchan_flow(world):
    app, dm, au, say, click, session = world

    # чужие сообщения игнорируются
    assert await say("/help", chat_id=1) == []

    out = joined(await say("/add 0000"))
    assert "Детский мир: товар с таким штрихкодом не найден" in out and "Ашан: товар" in out

    out = joined(await say(f"/add {BARCODE}"))
    assert "✅ Детский мир" in out and "✅ Ашан" in out and "отслеживание запущено" in out
    # Европарк (009) и Подольск (не Москва) не проверяются
    assert sorted(set(au.detail_calls)) == ["512", "701"]
    assert "Уже отслеживается в сети Ашан" in joined(await say(f"/add {BARCODE}"))

    out = joined(await say("/list"))
    assert "всего 81 шт" in out and "Детский мир" in out

    out = joined(await say(f"/status {BARCODE}"))
    assert "<b>46 шт</b> — <b>АШАН Академика Анохина</b>" in out and "Европарк" not in out
    assert out.index("46 шт") < out.index("35 шт")  # по убыванию

    # Ашан: Севастопольский +72 (новый кейс), Анохина +2, Детский мир без изменений
    au.qty.update({"701": 107, "512": 48})
    out = await say("/check")
    case = [t for t in out if "Новый кейс" in t]
    assert case and "35 → <b>107 шт</b> (+72) 🆕" in case[0] and "46 → <b>48 шт</b> (+2)" in case[0]
    assert case[0].index("107") < case[0].index("48")  # сначала самый большой прирост

    # раскупили — тихое «закончился», затем снова привезли — рост с нуля
    au.qty["512"] = 0
    out = await say("/check")
    assert any("Закончился" in t and "Анохина" in t for t in out)
    au.qty["512"] = 20
    out = await say("/check")
    grow = [t for t in out if "Остаток вырос" in t]
    assert grow and "0 → <b>20 шт</b> (+20)" in grow[0] and "🆕" not in grow[0]

    # уменьшение — не событие
    au.qty["701"] = 100
    out = await say("/check")
    assert not any("вырос" in t or "кейс" in t for t in out)

    out = joined(await say(f"/history {BARCODE}"))
    assert "35 → 107 шт (+72) 🆕" in out and "0 → 20 шт (+20)" in out

    # /soon — только «Детский мир»
    out = joined(await say(f"/soon {BARCODE}"))
    assert "Ближайшие поставки" in out and "Детский мир" in out

    # ручной журнал в магазине Ашана пишется в товар Ашана
    out = joined(await say("/log анохина 25"))
    assert "Анохина" in out and "первая запись" in out


async def test_detmir_flow(world):
    app, dm, au, say, click, session = world
    await say(f"/add {BARCODE}")

    # Колумбус пропал — тихое уведомление «закончился»
    dm.stores = [store("157", "ТРЦ «Гагаринский»", "HIGH")]
    out = await say("/check")
    assert any("Закончился" in t and "Колумбус" in t for t in out)
    assert any(isinstance(c, SendMessage) and c.disable_notification for c in session.calls)

    out = joined(await say("/status"))
    assert "нет в наличии, самовывоз не везут" in out and "Колумбус" in out

    # в Колумбус начали возить под заказ
    dm.stores = [store("157", "ТРЦ «Гагаринский»", "HIGH"),
                 store("2171", "ТРЦ «Колумбус»", None, pickup="2099-01-02T00:00:00+03:00")]
    out = await say("/check")
    transit = [t for t in out if "Везут под заказ" in t]
    assert transit and "Колумбус" in transit[0] and "02.01" in transit[0]
    out = joined(await say("/soon"))
    assert "02.01:</b>" in out and "Колумбус" in out and "Гагаринский" not in out

    # Колумбус снова появился — поставка
    dm.stores = [store("157", "ТРЦ «Гагаринский»", "HIGH"), store("2171", "ТРЦ «Колумбус»", "LOW")]
    out = await say("/check")
    supply = [t for t in out if "Поставка" in t]
    assert supply and "Колумбус" in supply[0] and "нет в наличии → <b>мало</b>" in supply[0]

    # ручной журнал
    assert "первая запись" in (await say("/log гагар 130"))[0]
    out = await say(f"/log 157 210 {BARCODE}")
    assert "+80" in out[0] and "новый кейс" in out[0]
    assert "несколько магазинов" in (await say("/log ТРЦ 5"))[0]
    assert "Использование" in (await say("/log гагаринский"))[0]

    out = joined(await say(f"/history {BARCODE}"))
    assert "Колумбус" in out and "нет в наличии → мало" in out and "новый кейс" in out

    # выбор магазинов: только Гагаринский → уведомления по Колумбусу и Ашану не приходят
    await say("/stores гагар")
    kb = [c for c in session.calls if isinstance(c, SendMessage)][0].reply_markup
    calls = await click("st:157", kb)
    assert any(isinstance(c, AnswerCallbackQuery) and "Добавлен" in c.text for c in calls)
    assert any(isinstance(c, EditMessageReplyMarkup) for c in calls)
    calls = await click("mode:selected")
    assert any(isinstance(c, AnswerCallbackQuery) and "только выбранные" in c.text for c in calls)

    dm.stores = [store("157", "ТРЦ «Гагаринский»", "HIGH")]
    au.qty["701"] = 500
    out = await say("/check")
    assert not any("Закончился" in t or "вырос" in t or "кейс" in t for t in out)

    # /stores без аргумента — счётчики по сетям, Европарк не считается
    out = joined(await say("/stores"))
    assert "Детский мир — 2" in out and "Ашан — 2" in out

    for t in await say(f"/remove {BARCODE}"):
        assert "Удалён" in t
    assert "Пока ничего" in (await say("/list"))[0]


async def test_error_notified_once(world):
    app, dm, au, say, click, session = world
    await say(f"/add {BARCODE}")
    dm.stores = None  # ломаем формат ответа
    out = await say("/check")
    assert any("Проверка прошла с ошибками" in t and "формат" in t for t in out)
    out = await say("/check")
    assert not any("ошибками" in t for t in out)  # не спамим
    dm.stores = [store("157", "ТРЦ «Гагаринский»", "HIGH")]
    out = await say("/check")
    assert any("снова проходят" in t for t in out)

    au.fail = True  # Ашан лёг целиком
    out = await say("/check")
    assert any("ошибками" in t and "Ашан" in t for t in out)


def test_fmt_day():
    from app.bot import MSK, fmt_day
    now = datetime(2026, 10, 26, 15, 0, tzinfo=MSK)
    assert fmt_day("", now) == "не везут"
    assert fmt_day("2026-10-26T15:38:00+03:00", now) == "сегодня"
    assert fmt_day("2026-10-27T00:00:00+03:00", now) == "завтра"
    assert fmt_day("2026-10-27T12:30:00+03:00", now) == "завтра с 12:30"
    assert fmt_day("2026-10-28T00:00:00+03:00", now) == "послезавтра"
    assert fmt_day("2026-10-30T00:00:00+03:00", now) == "пт 30.10"
    assert fmt_day("2026-10-25T21:00:00.000Z", now) == "сегодня"
