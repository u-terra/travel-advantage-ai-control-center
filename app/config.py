from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import load_dotenv

from app.access import parse_allowed_user_ids
from app.orchestration.factory import normalize_orchestration_provider_name
from app.planner.cost import normalize_max_llm_calls
from app.planner.factory import normalize_planner_provider_name
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
    content_factory_topics_url: str
    content_factory_token: str
    content_factory_timeout_seconds: float
    lead_radar_db_path: Path
    # Рабочий файл реестра источников. Отдельно от стартового набора в
    # config/sources.json: деплой не должен затирать добавленные источники.
    sources_registry_path: Path
    v2_menu_enabled: bool
    # Phase 1 LLM orchestration - shadow mode only (see app.orchestration).
    # Default "null" -> NullOrchestrationLLMProvider, fully inert: the old
    # keyword/regex router keeps driving every reply either way, this flag
    # only controls whether a parallel comparison gets logged.
    orchestration_llm_provider: str
    # Настройки первого живого orchestration-провайдера ("openai") - прямой
    # HTTPS-вызов OpenAI, отдельный ключ от production CONTENT_FACTORY_*
    # (см. app.orchestration.openai_provider). Пустой api_key оставляет
    # provider.is_configured=False - shadow остаётся инертным, как с "null".
    orchestration_openai_api_key: str
    orchestration_openai_model: str
    orchestration_openai_timeout_seconds: float
    # Порог обязательного Business Onboarding: workspace, созданные ДО этого
    # момента, никогда не блокируются онбордингом, даже с incomplete-профилем
    # (legacy-совместимость). None (переменная не задана) — fail-safe в
    # сторону НЕ требовать онбординг ни у кого, пока оператор явно не
    # настроит момент rollout — см. app/services/business_profile_context.py:
    # is_onboarding_required.
    onboarding_rollout_at: datetime | None
    # Stage 3 Planner MVP - OFF by default (see app.planner). When False, or
    # when planner_llm_provider resolves to "null"/is unconfigured, or when
    # the requesting user is not in planner_allowed_telegram_user_ids,
    # behavior is 100% identical to before Planner existed - the old
    # keyword router drives every reply, exactly as today.
    planner_enabled: bool
    planner_llm_provider: str
    # Settings for the first live Planner provider ("openai") - direct
    # HTTPS call, same pattern as ORCHESTRATION_OPENAI_*. If
    # PLANNER_OPENAI_API_KEY is not set, falls back to
    # ORCHESTRATION_OPENAI_API_KEY (same vendor secret already configured
    # for shadow-mode routing) rather than requiring operators to provision
    # and manage a second identical secret - see load_settings().
    planner_openai_api_key: str
    planner_openai_model: str
    planner_openai_timeout_seconds: float
    # Staged rollout allowlist: empty/unset means Planner is allowed for
    # NOBODY, even with planner_enabled=True and a configured provider - see
    # app.planner.eligibility.is_planner_allowed_for_user. This is
    # deliberately NOT "no restriction" - staged rollout must never
    # accidentally become "enabled for everyone".
    planner_allowed_telegram_user_ids: frozenset[int]
    # Stage 3.1: hard per-run LLM call budget - see app.planner.cost.
    # Invalid/missing/out-of-range falls back to
    # app.planner.cost.DEFAULT_MAX_LLM_CALLS_PER_PLANNER_RUN (4).
    planner_max_llm_calls: int
    # RoboKassa billing (see app.services.robokassa/app.services.billing_service).
    # Password1/Password2 never leave this Settings object except into
    # RoboKassaConfig (built once in app.web_api) - never logged, never
    # persisted, never returned to a client. Empty strings are a valid,
    # expected state (billing is simply "not configured" - see
    # RoboKassaConfig.is_configured) - this app must start and run its test
    # suite without any real RoboKassa secret ever existing.
    robokassa_merchant_login: str
    robokassa_password1: str
    robokassa_password2: str
    # Defaults to True (test mode) when unset - "не включать реальные
    # платежи автоматически": going live requires an explicit
    # ROBOKASSA_IS_TEST=false in the environment, never a code change.
    robokassa_is_test: bool
    # None (not configured) rather than any hardcoded fallback - this
    # product's price is never invented in code, only read from
    # ORCHESTRAVEL_STANDARD_PRICE_RUB.
    robokassa_standard_price_rub: Decimal | None
    orchestravel_subscription_days: int
    # https://app.orchestravel.ru by default (the product's real domain);
    # overridable for local/staging so SuccessURL/FailURL never have to
    # point at production while testing.
    orchestravel_public_base_url: str
    # Beta Control Center (see app/admin_api.py): platform-admin allowlist,
    # completely separate from any workspace membership role - a
    # workspace owner/admin is NEVER a platform admin just by being one.
    # Normalized lowercase emails. Empty (unset) = nobody is a platform
    # admin - fail-closed by construction, never hardcoded in code.
    orchestravel_admin_emails: frozenset[str]


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


def _parse_robokassa_is_test(raw: str | None) -> bool:
    """Defaults to TEST MODE when unset/empty/garbage - going live
    requires an explicit, deliberate ROBOKASSA_IS_TEST=false."""
    if raw is None:
        return True
    normalized = raw.strip().lower()
    if not normalized:
        return True
    return normalized not in {"false", "0", "no"}


def _parse_positive_decimal(raw: str | None) -> Decimal | None:
    if not raw or not raw.strip():
        return None
    try:
        value = Decimal(raw.strip())
    except InvalidOperation:
        return None
    return value if value > 0 else None


def _parse_positive_int(raw: str | None, *, default: int) -> int:
    if not raw or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if value > 0 else default


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
    # F2D: optional override, same convention as cf_analysis_url above -
    # empty means auto-derive from cf_url (see content_factory._topics_endpoint).
    cf_topics_url = os.environ.get("CONTENT_FACTORY_TOPICS_URL", "").strip()
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
    orchestration_llm_provider = normalize_orchestration_provider_name(
        os.environ.get("ORCHESTRATION_LLM_PROVIDER")
    )
    orchestration_openai_api_key = os.environ.get("ORCHESTRATION_OPENAI_API_KEY", "").strip()
    orchestration_openai_model = (
        os.environ.get("ORCHESTRATION_OPENAI_MODEL", "").strip() or "gpt-4o-mini"
    )
    orchestration_openai_timeout_raw = os.environ.get(
        "ORCHESTRATION_OPENAI_TIMEOUT_SECONDS", ""
    ).strip()
    onboarding_rollout_at = _parse_datetime(os.environ.get("ONBOARDING_ROLLOUT_AT"))

    planner_enabled = _parse_bool(os.environ.get("PLANNER_ENABLED"))
    planner_llm_provider = normalize_planner_provider_name(
        os.environ.get("PLANNER_LLM_PROVIDER")
    )
    planner_openai_api_key = (
        os.environ.get("PLANNER_OPENAI_API_KEY", "").strip()
        or orchestration_openai_api_key
    )
    planner_openai_model = (
        os.environ.get("PLANNER_OPENAI_MODEL", "").strip() or "gpt-4o-mini"
    )
    planner_openai_timeout_raw = os.environ.get(
        "PLANNER_OPENAI_TIMEOUT_SECONDS", ""
    ).strip()
    planner_allowed_raw = os.environ.get(
        "PLANNER_ALLOWED_TELEGRAM_USER_IDS", ""
    ).strip()
    # Same parser as the main bot allowlist (TELEGRAM_ALLOWED_USER_IDS) -
    # unset/empty/malformed all fail closed to an empty set, which
    # is_planner_allowed_for_user treats as "allowed for nobody".
    planner_allowed_telegram_user_ids = parse_allowed_user_ids(planner_allowed_raw)
    # Stage 3.1 hard cost cap - invalid/missing/out-of-range falls back to a
    # safe default (4), never raises at startup. See app.planner.cost.
    planner_max_llm_calls = normalize_max_llm_calls(
        os.environ.get("PLANNER_MAX_LLM_CALLS")
    )

    robokassa_merchant_login = os.environ.get("ROBOKASSA_MERCHANT_LOGIN", "").strip()
    robokassa_password1 = os.environ.get("ROBOKASSA_PASSWORD1", "").strip()
    robokassa_password2 = os.environ.get("ROBOKASSA_PASSWORD2", "").strip()
    robokassa_is_test = _parse_robokassa_is_test(os.environ.get("ROBOKASSA_IS_TEST"))
    robokassa_standard_price_rub = _parse_positive_decimal(
        os.environ.get("ORCHESTRAVEL_STANDARD_PRICE_RUB")
    )
    orchestravel_subscription_days = _parse_positive_int(
        os.environ.get("ORCHESTRAVEL_SUBSCRIPTION_DAYS"), default=30,
    )
    orchestravel_public_base_url = (
        os.environ.get("ORCHESTRAVEL_PUBLIC_BASE_URL", "").strip()
        or "https://app.orchestravel.ru"
    )
    orchestravel_admin_emails = frozenset(
        email.strip().lower()
        for email in os.environ.get("ORCHESTRAVEL_ADMIN_EMAILS", "").split(",")
        if email.strip()
    )

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

    try:
        orchestration_openai_timeout = (
            float(orchestration_openai_timeout_raw) if orchestration_openai_timeout_raw else 10.0
        )
    except ValueError:
        orchestration_openai_timeout = 10.0
    if orchestration_openai_timeout <= 0:
        orchestration_openai_timeout = 10.0

    try:
        planner_openai_timeout = (
            float(planner_openai_timeout_raw) if planner_openai_timeout_raw else 20.0
        )
    except ValueError:
        planner_openai_timeout = 20.0
    if planner_openai_timeout <= 0:
        planner_openai_timeout = 20.0

    return Settings(
        bot_token=token,
        admin_telegram_id=admin_id,
        allowed_user_ids=allowed_user_ids,
        journal_db_path=Path(db_path_raw),
        log_level=log_level,
        llm_provider=llm_provider,
        content_factory_url=cf_url,
        content_factory_source_analysis_url=cf_analysis_url,
        content_factory_topics_url=cf_topics_url,
        content_factory_token=cf_token,
        content_factory_timeout_seconds=cf_timeout,
        lead_radar_db_path=Path(lr_db_path_raw),
        # Значение по умолчанию (data/sources.json) и переменная
        # SOURCE_REGISTRY_PATH определены в одном месте — в самом реестре,
        # чтобы консольные скрипты видели тот же путь без сборки Settings.
        sources_registry_path=runtime_registry_path(),
        v2_menu_enabled=v2_menu_enabled,
        orchestration_llm_provider=orchestration_llm_provider,
        orchestration_openai_api_key=orchestration_openai_api_key,
        orchestration_openai_model=orchestration_openai_model,
        orchestration_openai_timeout_seconds=orchestration_openai_timeout,
        onboarding_rollout_at=onboarding_rollout_at,
        planner_enabled=planner_enabled,
        planner_llm_provider=planner_llm_provider,
        planner_openai_api_key=planner_openai_api_key,
        planner_openai_model=planner_openai_model,
        planner_openai_timeout_seconds=planner_openai_timeout,
        planner_allowed_telegram_user_ids=planner_allowed_telegram_user_ids,
        planner_max_llm_calls=planner_max_llm_calls,
        robokassa_merchant_login=robokassa_merchant_login,
        robokassa_password1=robokassa_password1,
        robokassa_password2=robokassa_password2,
        robokassa_is_test=robokassa_is_test,
        robokassa_standard_price_rub=robokassa_standard_price_rub,
        orchestravel_subscription_days=orchestravel_subscription_days,
        orchestravel_public_base_url=orchestravel_public_base_url,
        orchestravel_admin_emails=orchestravel_admin_emails,
    )
