from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_telegram_id: int
    journal_db_path: Path
    log_level: str
    content_factory_url: str
    content_factory_token: str
    content_factory_timeout_seconds: float
    lead_radar_db_path: Path


def load_settings() -> Settings:
    load_dotenv()

    token = os.environ.get("BOT_TOKEN", "").strip()
    admin_raw = os.environ.get("ADMIN_TELEGRAM_ID", "").strip()
    db_path_raw = os.environ.get("JOURNAL_DB_PATH", "data/journal.sqlite3").strip()
    log_level = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    cf_url = os.environ.get("CONTENT_FACTORY_INTERNAL_URL", "").strip()
    cf_token = os.environ.get("CONTENT_FACTORY_INTERNAL_TOKEN", "").strip()
    cf_timeout_raw = os.environ.get("CONTENT_FACTORY_TIMEOUT_SECONDS", "").strip()
    # Старые HTTP-настройки Lead Radar (LEAD_RADAR_INTERNAL_URL / _TOKEN /
    # _TIMEOUT_SECONDS) больше не читаются — Lead Radar работает на том же
    # VPS как локальная SQLite-база, доступная по пути LEAD_RADAR_DB_PATH.
    lr_db_path_raw = os.environ.get(
        "LEAD_RADAR_DB_PATH", "/opt/travel_lead_radar/data/leads.db"
    ).strip()

    if not token:
        raise RuntimeError("BOT_TOKEN не задан. Заполните .env")
    if not admin_raw:
        raise RuntimeError("ADMIN_TELEGRAM_ID не задан. Заполните .env")
    try:
        admin_id = int(admin_raw)
    except ValueError as exc:
        raise RuntimeError("ADMIN_TELEGRAM_ID должен быть целым числом") from exc

    try:
        cf_timeout = float(cf_timeout_raw) if cf_timeout_raw else 20.0
    except ValueError:
        cf_timeout = 20.0
    if cf_timeout <= 0:
        cf_timeout = 20.0

    return Settings(
        bot_token=token,
        admin_telegram_id=admin_id,
        journal_db_path=Path(db_path_raw),
        log_level=log_level,
        content_factory_url=cf_url,
        content_factory_token=cf_token,
        content_factory_timeout_seconds=cf_timeout,
        lead_radar_db_path=Path(lr_db_path_raw),
    )
