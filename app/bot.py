"""Telegram-бот: команды, уведомления, периодическая проверка."""
from __future__ import annotations

import asyncio
import html
import logging
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .auchan import AuchanClient
from .config import Config
from .db import NETWORK_TITLES, Database, TrackedProduct
from .detmir import LEVEL_TITLES, DetmirClient, Store
from .http import SiteError
from .monitor import APPEARED, IN_TRANSIT, LEVEL_UP, QTY_UP, SOLD_OUT, Monitor, ProductReport

log = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))
MAX_LEN = 4000

HELP = """<b>Отслеживание Hot Wheels: «Детский мир» и Ашан</b>

<b>Ашан</b> отдаёт точное количество по каждому магазину. Бот пишет, когда остаток вырос (🆕 — если сразу на {case}+ штук, похоже на новый кейс), когда товар появился там, где было 0, и когда закончился.

<b>«Детский мир»</b> показывает только уровень: <i>много / мало / последние</i>, а для магазинов без товара — дату самовывоза под заказ. Бот пишет, когда товар появился или уровень вырос, когда в магазин стали возить под заказ, и когда товар закончился.

<b>Команды</b>
/add &lt;штрихкод&gt; — добавить товар (ищется в обеих сетях)
/remove &lt;штрихкод&gt; — удалить товар
/list — отслеживаемые товары
/status [штрихкод] — остатки: Ашан — по количеству, «Детский мир» — где нет или мало
/soon [штрихкод] — «Детский мир»: ближайшие поставки по дате самовывоза
/stores — режим: все магазины / выбранные
/stores &lt;поиск&gt; — найти магазин и добавить/убрать (например: /stores гагаринский)
/history &lt;штрихкод&gt; — история поставок по магазинам
/log &lt;магазин&gt; &lt;кол-во&gt; [штрихкод] — записать остаток со сканера (например: /log гагаринский 630)
/check — проверить прямо сейчас"""


def fmt_time(iso: str) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    return dt.astimezone(MSK).strftime("%d.%m %H:%M")


WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def fmt_day(iso: str, today: datetime | None = None) -> str:
    """Дата самовывоза по-человечески: «сегодня», «завтра с 12:30», «чт 30.10»."""
    if not iso:
        return "не везут"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(MSK)
    except ValueError:
        return iso
    days = (dt.date() - (today or datetime.now(MSK)).astimezone(MSK).date()).days
    day = {0: "сегодня", 1: "завтра", 2: "послезавтра"}.get(days, f"{WEEKDAYS[dt.weekday()]} {dt:%d.%m}")
    if days >= 1 and (dt.hour, dt.minute) != (0, 0):
        day += f" с {dt:%H:%M}"
    return day


def store_line(s: Store | None, code: str) -> str:
    if s is None:
        return f"магазин {code}"
    metro = f" (м. {html.escape(s.metro)})" if s.metro else ""
    return f"<b>{html.escape(s.title)}</b> [{s.code}] — {html.escape(s.address)}{metro}"


def product_head(p: TrackedProduct) -> str:
    return f"<b>{html.escape(p.title)}</b> — {p.network_title} ({p.barcode})"


def chunks(lines: list[str], header: str = "") -> list[str]:
    out, cur = [], header
    for line in lines:
        if len(cur) + len(line) + 1 > MAX_LEN:
            out.append(cur)
            cur = ""
        cur += ("\n" if cur else "") + line
    if cur:
        out.append(cur)
    return out


class App:
    def __init__(self, cfg: Config, db: Database, client: DetmirClient, bot: Bot,
                 auchan: AuchanClient | None = None) -> None:
        self.cfg, self.db, self.client, self.bot, self.auchan = cfg, db, client, bot, auchan
        self.monitor = Monitor(cfg, db, client, auchan)
        self._failing = False
        self.router = Router()
        self.router.message.filter(F.chat.id == cfg.admin_chat_id)
        self.router.callback_query.filter(F.message.chat.id == cfg.admin_chat_id)
        self._register()

    # ---------- отправка ----------
    async def send(self, text: str, silent: bool = False, **kw) -> None:
        for part in chunks(text.split("\n")):
            await self.bot.send_message(self.cfg.admin_chat_id, part, parse_mode="HTML",
                                        disable_notification=silent, disable_web_page_preview=True, **kw)

    async def notify_reports(self, reports: list[ProductReport]) -> None:
        mode = await self.db.stores_mode()
        selected = await self.db.selected_stores()
        footer = f"\n<i>Проверено {fmt_time(datetime.now(timezone.utc).isoformat())} (МСК)</i>"

        for r in reports:
            if not r.ok:
                continue
            events = [e for e in r.events if mode == "all" or e.store_code in selected]
            gone = [e for e in events if e.kind == SOLD_OUT]
            head = product_head(r.product)
            if r.product.network == "au":
                up = sorted((e for e in events if e.kind in (APPEARED, QTY_UP)), key=lambda e: -e.delta)
                if up:
                    new_case = any(e.delta >= self.cfg.case_threshold for e in up)
                    lines = [("🆕 <b>Новый кейс!</b> " if new_case else "📈 <b>Остаток вырос</b> ") + head, ""]
                    for e in up:
                        mark = " 🆕" if e.delta >= self.cfg.case_threshold else ""
                        lines.append(store_line(r.stores.get(e.store_code), e.store_code))
                        lines.append(f"   {e.old_qty} → <b>{e.new_qty} шт</b> (+{e.delta}){mark}")
                    await self.send("\n".join(lines) + footer)
            else:
                up = [e for e in events if e.kind in (APPEARED, LEVEL_UP)]
                transit = sorted((e for e in events if e.kind == IN_TRANSIT), key=lambda e: e.pickup_at)
                if up:
                    lines = [f"🚚 <b>Поставка!</b> {head}", ""]
                    for e in up:
                        lines.append(store_line(r.stores.get(e.store_code), e.store_code))
                        lines.append(f"   {LEVEL_TITLES[e.old]} → <b>{LEVEL_TITLES[e.new]}</b>, "
                                     f"забрать: {fmt_day(e.pickup_at)}")
                    await self.send("\n".join(lines) + footer)
                if transit:
                    lines = [f"🛻 <b>Везут под заказ</b> {head}", "Товара в магазине нет, но его можно заказать:", ""]
                    for e in transit:
                        lines.append(store_line(r.stores.get(e.store_code), e.store_code))
                        lines.append(f"   самовывоз: <b>{fmt_day(e.pickup_at)}</b>")
                    await self.send("\n".join(lines) + footer)
            if gone and self.cfg.notify_sold_out:
                lines = [f"🕳 Закончился: {head}", ""]
                lines += [store_line(r.stores.get(e.store_code), e.store_code) for e in gone]
                await self.send("\n".join(lines) + footer, silent=True)

    async def notify_errors(self, reports: list[ProductReport]) -> None:
        failed = [r for r in reports if r.error]
        if failed and not self._failing:
            self._failing = True
            lines = ["⚠️ <b>Проверка прошла с ошибками</b> (бот продолжает работать и повторит позже)"]
            lines += [f"• {html.escape(r.product.title)} ({r.product.network_title}): "
                      f"<code>{html.escape(r.error[:300])}</code>" for r in failed]
            await self.send("\n".join(lines))
        elif not failed and self._failing and reports:
            self._failing = False
            await self.send("✅ Проверки снова проходят успешно.")

    async def run_check(self) -> list[ProductReport]:
        reports = await self.monitor.check_all()
        await self.notify_reports(reports)
        await self.notify_errors(reports)
        return reports

    async def scheduler(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                if not self.monitor.busy:
                    await self.run_check()
            except Exception:  # бот не должен падать ни при каких ошибках проверки
                log.exception("Ошибка в периодической проверке")
                try:
                    await self.send("⚠️ Непредвиденная ошибка в проверке, подробности в логе.")
                except Exception:
                    log.exception("Не удалось отправить сообщение об ошибке")
            minutes = random.uniform(self.cfg.check_interval_min, self.cfg.check_interval_max)
            log.info("Следующая проверка через %.1f мин", minutes)
            await asyncio.sleep(minutes * 60)

    # ---------- вспомогательное ----------
    async def resolve_products(self, msg: Message, arg: str | None) -> list[TrackedProduct]:
        """Все отслеживаемые строки (по сетям) для штрихкода; без аргумента — если товар один."""
        products = await self.db.list_products()
        if not arg:
            if len({p.barcode for p in products}) == 1:
                return products
            await msg.answer("Укажите штрихкод товара. Список: /list")
            return []
        found = await self.db.find_products(arg)
        if not found:
            await msg.answer(f"Товар {html.escape(arg)} не отслеживается. Список: /list", parse_mode="HTML")
        return found

    async def resolve_store(self, msg: Message, query: str) -> Store | None:
        found = await self.db.search_stores(query, self.cfg.city_prefix, self.cfg.auchan_exclude)
        exact = [s for s in found if s.code == query.strip()]
        if exact:
            return exact[0]
        if len(found) == 1:
            return found[0]
        if not found:
            await msg.answer("Магазин не найден. Попробуйте часть названия, улицы или метро, "
                             "либо сначала выполните /check, чтобы бот загрузил список магазинов.")
        else:
            lines = ["Нашлось несколько магазинов, укажите код:"]
            lines += [f"[{s.code}] {html.escape(s.title)} — {html.escape(s.address)}" for s in found[:20]]
            await msg.answer("\n".join(lines), parse_mode="HTML")
        return None

    def stores_keyboard(self, stores: list[Store], selected: set[str]) -> InlineKeyboardMarkup:
        rows = [[InlineKeyboardButton(text=("✅ " if s.code in selected else "➕ ") + f"{s.title} [{s.code}]"[:60],
                                      callback_data=f"st:{s.code}")] for s in stores[:20]]
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def find_everywhere(self, barcode: str) -> tuple[list[tuple[str, object]], list[str]]:
        """Ищет товар в обеих сетях. Возвращает [(сеть, Product)] и ошибки."""
        found, errors = [], []
        try:
            p = await self.client.find_by_barcode(barcode)
            if p:
                found.append(("dm", p))
        except SiteError as e:
            errors.append(f"Детский мир: {e}")
        if self.auchan and self.cfg.auchan_enabled:
            try:
                stores = await self.monitor.auchan_stores()
                merchant = next(iter(sorted(stores)), "au:701").removeprefix("au:")
                p = await self.auchan.find_by_barcode(barcode, merchant)
                if p:
                    found.append(("au", p))
            except SiteError as e:
                errors.append(f"Ашан: {e}")
        return found, errors

    # ---------- команды ----------
    def _register(self) -> None:
        r = self.router

        @r.message(Command("start", "help"))
        async def cmd_help(msg: Message) -> None:
            await msg.answer(HELP.format(case=self.cfg.case_threshold), parse_mode="HTML")

        @r.message(Command("add"))
        async def cmd_add(msg: Message, command: CommandObject) -> None:
            code = (command.args or "").strip()
            if not code.isdigit():
                await msg.answer("Использование: /add &lt;штрихкод&gt;, например /add 0074299057854", parse_mode="HTML")
                return
            await msg.answer("Ищу товар в «Детском мире» и Ашане…")
            found, errors = await self.find_everywhere(code)
            lines = []
            added = 0
            for network, p in found:
                if await self.db.add_product(p, code, network):
                    added += 1
                    lines.append(f"✅ {NETWORK_TITLES[network]}: <b>{html.escape(p.title)}</b> (id {p.id})")
                else:
                    lines.append(f"Уже отслеживается в сети {NETWORK_TITLES[network]}: {html.escape(p.title)}")
            for network in ("dm", "au"):
                if network not in {n for n, _ in found} and not any(e.startswith(NETWORK_TITLES[network])
                                                                     for e in errors):
                    if network == "dm" or (self.auchan and self.cfg.auchan_enabled):
                        lines.append(f"— {NETWORK_TITLES[network]}: товар с таким штрихкодом не найден")
            lines += [f"⚠️ {html.escape(e[:300])}" for e in errors]
            await msg.answer("\n".join(lines), parse_mode="HTML")
            if added:
                await msg.answer("Делаю базовую проверку… (Ашан проверяется по каждому магазину, это 1–2 минуты)")
                if not self.monitor.busy:
                    await self.run_check()
                await msg.answer("Готово, отслеживание запущено. Текущее состояние: /status " + code)

        @r.message(Command("remove"))
        async def cmd_remove(msg: Message, command: CommandObject) -> None:
            if not command.args:
                await msg.answer("Использование: /remove &lt;штрихкод&gt;", parse_mode="HTML")
                return
            for p in await self.resolve_products(msg, command.args.strip()):
                await self.db.remove_product(p.id)
                await msg.answer(f"Удалён: {html.escape(p.title)} ({p.network_title})", parse_mode="HTML")

        @r.message(Command("list"))
        async def cmd_list(msg: Message) -> None:
            products = await self.db.list_products()
            if not products:
                await msg.answer("Пока ничего не отслеживается. Добавьте: /add &lt;штрихкод&gt;", parse_mode="HTML")
                return
            lines = []
            for p in products:
                if p.network == "au":
                    q = await self.db.stock_qty(p.id)
                    have = sum(1 for v in q.values() if v > 0)
                    stock = f"в наличии в {have} из {len(q)} магазинов, всего {sum(q.values())} шт"
                else:
                    state = await self.db.stock_state(p.id)
                    have = sum(1 for lvl in state.values() if lvl != "NONE")
                    stock = f"в наличии в {have} из {len(state)} магазинов"
                lines.append(f"• {product_head(p)}\n  {stock}")
            await msg.answer("\n".join(lines), parse_mode="HTML")

        @r.message(Command("status"))
        async def cmd_status(msg: Message, command: CommandObject) -> None:
            arg = (command.args or "").strip()
            products = await self.resolve_products(msg, arg) if arg else await self.db.list_products()
            if not products:
                if not arg:
                    await msg.answer("Пока ничего не отслеживается.")
                return
            stores = await self.db.stores(self.cfg.city_prefix)
            mode = await self.db.stores_mode()
            selected = await self.db.selected_stores()
            for p in products:
                lines = [product_head(p)]
                if p.network == "au":
                    q = {c: v for c, v in (await self.db.stock_qty(p.id)).items() if c not in self.cfg.auchan_exclude}
                    lines.append(f"Магазинов: {len(q)}, с товаром — {sum(v > 0 for v in q.values())}, "
                                 f"всего {sum(q.values())} шт")
                    lines.append("")
                    for c in sorted((c for c in q if q[c] > 0), key=lambda c: (-q[c], c)):
                        star = "⭐ " if c in selected else ""
                        lines.append(f"• <b>{q[c]} шт</b> — {star}{store_line(stores.get(c), c)}")
                    zero = sorted((stores[c].title if c in stores else c) for c in q if q[c] == 0)
                    if zero:
                        lines.append(f"\n<b>Нет в наличии ({len(zero)}):</b> " + html.escape(", ".join(zero)))
                else:
                    state = await self.db.stock_full(p.id)

                    def describe(code: str) -> str:
                        lvl, pickup = state.get(code, ("NONE", ""))
                        extra = f", самовывоз {fmt_day(pickup)}" if lvl == "NONE" else ""
                        return f"• {LEVEL_TITLES[lvl]}{extra}: {store_line(stores.get(code), code)}"

                    low = sorted((c for c, (lvl, _) in state.items() if lvl != "HIGH"),
                                 key=lambda c: (state[c][0], state[c][1] or "9", c))
                    lines.append(f"Магазинов: {len(state)}, «много» — {sum(v[0] == 'HIGH' for v in state.values())}")
                    if low:
                        lines.append("\n<b>Нет или мало:</b>")
                        lines += [describe(c) for c in low]
                    mine = sorted(c for c in selected if c in state)
                    if mode == "selected" and mine:
                        lines.append("\n<b>Выбранные магазины:</b>")
                        lines += [describe(c) for c in mine]
                for part in chunks(lines):
                    await msg.answer(part, parse_mode="HTML", disable_web_page_preview=True)

        @r.message(Command("soon"))
        async def cmd_soon(msg: Message, command: CommandObject) -> None:
            arg = (command.args or "").strip()
            products = await self.resolve_products(msg, arg) if arg else await self.db.list_products()
            products = [p for p in products if p.network == "dm"]
            if not products:
                await msg.answer("Даты самовывоза есть только у «Детского мира», а он сейчас не отслеживается "
                                 "по этому товару.")
                return
            stores = await self.db.stores(self.cfg.city_prefix)
            selected = await self.db.selected_stores()
            for p in products:
                state = await self.db.stock_full(p.id)
                empty = [(pickup, c) for c, (lvl, pickup) in state.items() if lvl == "NONE"]
                lines = [f"<b>Ближайшие поставки:</b> {product_head(p)}"]
                if not empty:
                    lines.append("Товар есть во всех магазинах — ждать нечего 🙂")
                else:
                    lines.append("<i>Магазины без товара по дате самовывоза под заказ. "
                                 "Это ориентир, когда туда поедет товар со склада, а не гарантия кейса на полке.</i>")
                    by_day: dict[str, list[str]] = defaultdict(list)
                    order: list[str] = []
                    for pickup, c in sorted(empty, key=lambda x: (x[0] or "9", x[1])):
                        day = fmt_day(pickup)
                        if day not in by_day:
                            order.append(day)
                        star = "⭐ " if c in selected else ""
                        by_day[day].append(f"• {star}{store_line(stores.get(c), c)}")
                    for day in order:
                        lines.append(f"\n<b>{day.capitalize()}:</b>")
                        lines += by_day[day]
                for part in chunks(lines):
                    await msg.answer(part, parse_mode="HTML", disable_web_page_preview=True)

        @r.message(Command("stores"))
        async def cmd_stores(msg: Message, command: CommandObject) -> None:
            selected = await self.db.selected_stores()
            if command.args:
                found = await self.db.search_stores(command.args, self.cfg.city_prefix, self.cfg.auchan_exclude)
                if not found:
                    await msg.answer("Ничего не найдено. Если список магазинов пуст — выполните /check.")
                    return
                await msg.answer(f"Найдено: {len(found)}" + (" (показаны первые 20)" if len(found) > 20 else "")
                                 + ". Нажмите, чтобы добавить/убрать:",
                                 reply_markup=self.stores_keyboard(found, selected))
                return
            mode = await self.db.stores_mode()
            stores = await self.db.stores(self.cfg.city_prefix)
            counts = defaultdict(int)
            for c in stores:
                if c not in self.cfg.auchan_exclude:
                    counts["au" if c.startswith("au:") else "dm"] += 1
            lines = [f"Режим уведомлений: <b>{'все магазины' if mode == 'all' else 'только выбранные'}</b>",
                     "Магазинов в базе: " + ", ".join(f"{NETWORK_TITLES[n]} — {k}" for n, k in sorted(counts.items()))]
            if selected:
                lines.append("\nВыбранные:")
                lines += [f"• {store_line(stores.get(c), c)}" for c in sorted(selected)]
            lines.append("\nДобавить магазин: /stores &lt;название, улица или метро&gt;")
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=("● " if mode == "all" else "") + "Все магазины", callback_data="mode:all"),
                InlineKeyboardButton(text=("● " if mode == "selected" else "") + "Только выбранные",
                                     callback_data="mode:selected"),
            ]])
            await msg.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb)

        @r.callback_query(F.data.startswith("mode:"))
        async def cb_mode(cb: CallbackQuery) -> None:
            mode = cb.data.split(":", 1)[1]
            if mode == "selected" and not await self.db.selected_stores():
                await cb.answer("Сначала выберите магазины: /stores <поиск>", show_alert=True)
                return
            await self.db.set_stores_mode(mode)
            await cb.answer("Режим: " + ("все магазины" if mode == "all" else "только выбранные"))
            await cb.message.edit_reply_markup(reply_markup=None)

        @r.callback_query(F.data.startswith("st:"))
        async def cb_store(cb: CallbackQuery) -> None:
            code = cb.data.split(":", 1)[1]
            selected = await self.db.selected_stores()
            on = code not in selected
            await self.db.select_store(code, on)
            s = await self.db.get_store(code)
            await cb.answer(("Добавлен: " if on else "Убран: ") + (s.title if s else code))
            # обновить галочки на кнопках
            kb = cb.message.reply_markup
            if kb:
                selected = await self.db.selected_stores()
                for row in kb.inline_keyboard:
                    for b in row:
                        c = b.callback_data.split(":", 1)[1]
                        b.text = ("✅ " if c in selected else "➕ ") + b.text[2:]
                await cb.message.edit_reply_markup(reply_markup=kb)
            if on and await self.db.stores_mode() == "all":
                await cb.message.answer("Сейчас уведомления идут по всем магазинам. "
                                        "Чтобы только по выбранным — /stores → «Только выбранные».")

        @r.message(Command("history"))
        async def cmd_history(msg: Message, command: CommandObject) -> None:
            products = await self.resolve_products(msg, (command.args or "").strip() or None)
            if not products:
                return
            stores = await self.db.stores()
            any_rows = False
            for p in products:
                by_store: dict[str, list[str]] = defaultdict(list)
                for h in await self.db.history(p.id):
                    if h.new_qty is not None and h.old_qty is not None:
                        d = h.new_qty - h.old_qty
                        mark = " 🆕" if d >= self.cfg.case_threshold else ""
                        by_store[h.store_code].append(
                            f"{fmt_time(h.checked_at)}: {h.old_qty} → {h.new_qty} шт (+{d}){mark}")
                    else:
                        by_store[h.store_code].append(
                            f"{fmt_time(h.checked_at)}: {LEVEL_TITLES[h.old_level]} → {LEVEL_TITLES[h.new_level]}")
                for m in await self.db.manual_history(p.id):
                    d = f" ({m.delta:+d})" if m.delta is not None else ""
                    mark = " 🆕 новый кейс" if m.is_new_case else ""
                    by_store[m.store_code].append(f"{fmt_time(m.logged_at)}: сканер {m.qty} шт{d}{mark}")
                if not by_store:
                    continue
                any_rows = True
                lines = [f"<b>История поставок:</b> {product_head(p)}"]
                for code, items in by_store.items():
                    lines.append("")
                    lines.append(store_line(stores.get(code), code))
                    lines += [f"   {i}" for i in sorted(items, reverse=True)[:15]]
                for part in chunks(lines):
                    await msg.answer(part, parse_mode="HTML", disable_web_page_preview=True)
            if not any_rows:
                await msg.answer("Поставок пока не зафиксировано.")

        @r.message(Command("log"))
        async def cmd_log(msg: Message, command: CommandObject) -> None:
            parts = (command.args or "").split()
            barcode = None
            if len(parts) >= 3 and parts[-1].isdigit() and len(parts[-1]) >= 8:
                barcode = parts.pop()
            if len(parts) < 2 or not parts[-1].isdigit():
                await msg.answer("Использование: /log &lt;магазин&gt; &lt;кол-во&gt; [штрихкод]\n"
                                 "Например: /log гагаринский 630", parse_mode="HTML")
                return
            qty = int(parts.pop())
            products = await self.resolve_products(msg, barcode)
            if not products:
                return
            s = await self.resolve_store(msg, " ".join(parts))
            if not s:
                return
            network = "au" if s.code.startswith("au:") else "dm"
            p = next((p for p in products if p.network == network), products[0])
            row = await self.db.add_manual_count(p.id, s.code, qty, self.cfg.case_threshold)
            text = f"Записал: {store_line(s, s.code)}\n{html.escape(p.title)} — <b>{qty} шт</b>"
            if row.delta is None:
                text += "\nЭто первая запись по магазину — прирост посчитаю со следующего раза."
            else:
                text += f" ({row.delta:+d} с прошлой записи)"
                if row.is_new_case:
                    text += f"\n🆕 <b>Похоже, приехал новый кейс</b> (прирост ≥ {self.cfg.case_threshold})"
            await msg.answer(text, parse_mode="HTML", disable_web_page_preview=True)

        @r.message(Command("check"))
        async def cmd_check(msg: Message) -> None:
            if not await self.db.list_products():
                await msg.answer("Нечего проверять — добавьте товар: /add &lt;штрихкод&gt;", parse_mode="HTML")
                return
            if self.monitor.busy:
                await msg.answer("Проверка уже идёт, результаты придут сообщением.")
                return
            await msg.answer("Проверяю… (Ашан — по каждому магазину, это 1–2 минуты)")
            reports = await self.run_check()
            lines = []
            for rep in reports:
                name = f"{html.escape(rep.product.title)} ({rep.product.network_title})"
                if not rep.ok:
                    lines.append(f"❌ {name}: ошибка")
                    continue
                have = sum(1 for v in rep.levels.values() if v != "NONE")
                note = "базовый снимок" if rep.baseline else f"изменений: {len(rep.events)}"
                total = f", всего {sum(rep.qtys.values())} шт" if rep.qtys else ""
                lines.append(f"✔️ {name}: в наличии в {have} из {len(rep.levels)} магазинов{total}, {note}")
            await msg.answer("\n".join(lines), parse_mode="HTML")


def build(cfg: Config, db: Database, client: DetmirClient, bot: Bot,
          auchan: AuchanClient | None = None) -> tuple[App, Dispatcher]:
    app = App(cfg, db, client, bot, auchan)
    dp = Dispatcher()
    dp.include_router(app.router)
    return app, dp
