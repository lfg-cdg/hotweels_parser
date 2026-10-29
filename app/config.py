"""Настройки из .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_chat_id: int
    check_interval_min: int  # минуты
    check_interval_max: int  # минуты
    region_iso: str
    city_prefix: str  # фильтр по началу адреса, пусто = все магазины региона
    case_threshold: int  # прирост в ручном журнале, считающийся новым кейсом
    notify_sold_out: bool
    db_path: Path
    log_level: str
    auchan_enabled: bool = True
    auchan_exclude: frozenset[str] = frozenset({"au:009"})  # коды магазинов Ашана, которые не проверяем
    auchan_min_increase: int = 1  # с какого прироста в Ашане присылать уведомление
    recent_hours: int = 24  # сколько часов показывать последние поставки в /check и /status


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "да")


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ConfigError(f"{name} должно быть числом, сейчас: {raw!r}") from e


def load_config(env_file: str | os.PathLike | None = None) -> Config:
    load_dotenv(env_file or ".env")

    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("Не задан BOT_TOKEN в .env")
    chat_id_raw = os.getenv("ADMIN_CHAT_ID", "").strip()
    if not chat_id_raw.lstrip("-").isdigit():
        raise ConfigError("Не задан или неверен ADMIN_CHAT_ID в .env")

    imin = _int("CHECK_INTERVAL_MIN", 20)
    imax = _int("CHECK_INTERVAL_MAX", 30)
    if imin < 5 or imax < imin:
        raise ConfigError("Нужно 5 <= CHECK_INTERVAL_MIN <= CHECK_INTERVAL_MAX")

    return Config(
        bot_token=token,
        admin_chat_id=int(chat_id_raw),
        check_interval_min=imin,
        check_interval_max=imax,
        region_iso=os.getenv("REGION_ISO", "RU-MOW").strip(),
        city_prefix=os.getenv("CITY_PREFIX", "Москва").strip(),
        case_threshold=_int("CASE_THRESHOLD", 50),
        notify_sold_out=_bool("NOTIFY_SOLD_OUT", True),
        db_path=Path(os.getenv("DB_PATH", "data/hotwheels.db")),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        auchan_enabled=_bool("AUCHAN_ENABLED", True),
        # по умолчанию исключён «АШАН Европарк» (009): похоже на склад доставки, тысячи штук
        auchan_exclude=frozenset(
            "au:" + c.strip().removeprefix("au:") for c in os.getenv("AUCHAN_EXCLUDE", "009").split(",") if c.strip()
        ),
        auchan_min_increase=max(1, _int("AUCHAN_MIN_INCREASE", 1)),
        recent_hours=max(0, _int("RECENT_HOURS", 24)),
    )
