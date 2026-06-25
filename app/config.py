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


def load_settings() -> Settings:
    load_dotenv()

    token = os.environ.get("BOT_TOKEN", "").strip()
    admin_raw = os.environ.get("ADMIN_TELEGRAM_ID", "").strip()
    db_path_raw = os.environ.get("JOURNAL_DB_PATH", "data/journal.sqlite3").strip()
    log_level = os.environ.get("LOG_LEVEL", "INFO").strip().upper()

    if not token:
        raise RuntimeError("BOT_TOKEN не задан. Заполните .env")
    if not admin_raw:
        raise RuntimeError("ADMIN_TELEGRAM_ID не задан. Заполните .env")
    try:
        admin_id = int(admin_raw)
    except ValueError as exc:
        raise RuntimeError("ADMIN_TELEGRAM_ID должен быть целым числом") from exc

    return Settings(
        bot_token=token,
        admin_telegram_id=admin_id,
        journal_db_path=Path(db_path_raw),
        log_level=log_level,
    )
