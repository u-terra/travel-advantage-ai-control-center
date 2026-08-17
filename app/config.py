from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from app.access import parse_allowed_user_ids
from app.services.llm.factory import normalize_provider_name
from app.services.source_registry import runtime_registry_path


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_telegram_id: int
    allowed_user_ids: frozenset[int]
    journal_db_path: Path
    log_level: str
    llm_provider: str
    content_factory_url: str
    content_factory_source_analysis_url: str
    content_factory_token: str
    content_factory_timeout_seconds: float
    lead_radar_db_path: Path
    # Рабочий файл реестра источников. Отдельно от стартового набора в
    # config/sources.json: деплой не должен затирать добавленные источники.
    sources_registry_path: Path
    v2_menu_enabled: bool
    # Порог обязательного Business Onboarding: workspace, созданные ДО этого
    # момента, никогда не блокируются онбордингом, даже с incomplete-профилем
    # (legacy-совместимость). None (переменная не задана) — fail-safe в
    # сторону НЕ требовать онбординг ни у кого, пока оператор явно не
    # настроит момент rollout — см. app/services/business_profile_context.py:
    # is_onboarding_required.
    onboarding_rollout_at: datetime | None


def _parse_bool(raw: str | None) -> bool:
    return bool(raw) and raw.strip().lower() == "true"


def _parse_datetime(raw: str | None) -> datetime | None:
    if not raw or not raw.strip():
        return None
    try:
        value = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def load_settings() -> Settings:
    load_dotenv()

    token = os.environ.get("BOT_TOKEN", "").strip()
    admin_raw = os.environ.get("ADMIN_TELEGRAM_ID", "").strip()
    allowed_raw = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    db_path_raw = os.environ.get("JOURNAL_DB_PATH", "data/journal.sqlite3").strip()
    log_level = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    # Пустое или отсутствующее значение = провайдер по умолчанию (openai),
    # поэтому существующий .env продолжает работать без изменений.
    llm_provider = normalize_provider_name(os.environ.get("LLM_PROVIDER"))
    cf_url = os.environ.get("CONTENT_FACTORY_INTERNAL_URL", "").strip()
    cf_analysis_url = os.environ.get("CONTENT_FACTORY_SOURCE_ANALYSIS_URL", "").strip()
    cf_token = os.environ.get("CONTENT_FACTORY_INTERNAL_TOKEN", "").strip()
    cf_timeout_raw = os.environ.get("CONTENT_FACTORY_TIMEOUT_SECONDS", "").strip()
    # Старые HTTP-настройки Lead Radar (LEAD_RADAR_INTERNAL_URL / _TOKEN /
    # _TIMEOUT_SECONDS) больше не читаются — Lead Radar работает на том же
    # VPS как локальная SQLite-база, доступная по пути LEAD_RADAR_DB_PATH.
    lr_db_path_raw = os.environ.get(
        "LEAD_RADAR_DB_PATH", "/opt/travel_lead_radar/data/leads.db"
    ).strip()
    v2_menu_enabled = _parse_bool(
        os.environ.get("TA_CONTROL_CENTER_V2_MENU_ENABLED")
    )
    onboarding_rollout_at = _parse_datetime(os.environ.get("ONBOARDING_ROLLOUT_AT"))

    if not token:
        raise RuntimeError("BOT_TOKEN не задан. Заполните .env")
    if not admin_raw:
        raise RuntimeError("ADMIN_TELEGRAM_ID не задан. Заполните .env")
    try:
        admin_id = int(admin_raw)
    except ValueError as exc:
        raise RuntimeError("ADMIN_TELEGRAM_ID должен быть целым числом") from exc

    # Единственный источник доступа к панели — TELEGRAM_ALLOWED_USER_IDS.
    # ADMIN_TELEGRAM_ID НЕ добавляется в allowlist автоматически: политика
    # fail-closed. Если переменная отсутствует, пуста или некорректна —
    # parse_allowed_user_ids вернёт пустой набор и доступ закрыт для всех.
    allowed_user_ids = parse_allowed_user_ids(allowed_raw)

    try:
        cf_timeout = float(cf_timeout_raw) if cf_timeout_raw else 20.0
    except ValueError:
        cf_timeout = 20.0
    if cf_timeout <= 0:
        cf_timeout = 20.0

    return Settings(
        bot_token=token,
        admin_telegram_id=admin_id,
        allowed_user_ids=allowed_user_ids,
        journal_db_path=Path(db_path_raw),
        log_level=log_level,
        llm_provider=llm_provider,
        content_factory_url=cf_url,
        content_factory_source_analysis_url=cf_analysis_url,
        content_factory_token=cf_token,
        content_factory_timeout_seconds=cf_timeout,
        lead_radar_db_path=Path(lr_db_path_raw),
        # Значение по умолчанию (data/sources.json) и переменная
        # SOURCE_REGISTRY_PATH определены в одном месте — в самом реестре,
        # чтобы консольные скрипты видели тот же путь без сборки Settings.
        sources_registry_path=runtime_registry_path(),
        v2_menu_enabled=v2_menu_enabled,
        onboarding_rollout_at=onboarding_rollout_at,
    )
