"""Точка входа: python -m app.main"""
from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot
from aiogram.types import BotCommand

from .auchan import AuchanClient
from .bot import build
from .config import ConfigError, load_config
from .db import Database
from .detmir import DetmirClient

COMMANDS = [
    BotCommand(command="soon", description="Ближайшие поставки"),
    BotCommand(command="status", description="Где нет или мало + выбранные магазины"),
    BotCommand(command="check", description="Проверить сейчас"),
    BotCommand(command="list", description="Отслеживаемые товары"),
    BotCommand(command="history", description="История поставок"),
    BotCommand(command="log", description="Записать остаток со сканера"),
    BotCommand(command="stores", description="Выбор магазинов"),
    BotCommand(command="add", description="Добавить товар по штрихкоду"),
    BotCommand(command="remove", description="Удалить товар"),
    BotCommand(command="help", description="Справка"),
]


async def run() -> None:
    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"Ошибка настроек: {e}", file=sys.stderr)
        sys.exit(2)

    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    db = Database(cfg.db_path)
    await db.connect()
    client = DetmirClient()
    auchan = AuchanClient() if cfg.auchan_enabled else None
    bot = Bot(cfg.bot_token)
    app, dp = build(cfg, db, client, bot, auchan)

    await bot.set_my_commands(COMMANDS)
    me = await bot.get_me()
    logging.info("Бот @%s запущен, проверка каждые %d–%d мин", me.username,
                 cfg.check_interval_min, cfg.check_interval_max)
    try:
        await app.send("🤖 Бот запущен. Справка: /help")
    except Exception:
        logging.exception("Не удалось написать в ADMIN_CHAT_ID — проверьте, что вы нажали /start у бота")

    scheduler = asyncio.create_task(app.scheduler())
    try:
        await dp.start_polling(bot, handle_signals=True)
    finally:
        scheduler.cancel()
        await client.close()
        if auchan:
            await auchan.close()
        await db.close()
        await bot.session.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
