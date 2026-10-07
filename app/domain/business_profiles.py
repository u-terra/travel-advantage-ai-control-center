from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


BUSINESS_TYPES = frozenset(
    {"independent_agent", "club_partner", "agency", "travel_company", "other"}
)
PROFILE_STATUSES = frozenset({"incomplete", "usable"})
CLAIM_STATUSES = frozenset({"unverified", "verified"})
BUSINESS_PROFILE_SCHEMA_VERSION = 1


class BusinessProfileValidationError(ValueError):
    pass


class StaleBusinessProfileError(RuntimeError):
    pass


@dataclass(frozen=True)
class BusinessClaim:
    text: str
    verification_status: str
    evidence_reference: str | None
    updated_at: str
    verified_at: str | None


@dataclass(frozen=True)
class BusinessContext:
    specializations: tuple[str, ...]
    destinations: tuple[str, ...]
    audiences: tuple[str, ...]
    markets: tuple[str, ...]
    positioning: Mapping[str, Any]
    communication: Mapping[str, Any]
    goals: tuple[str, ...]
    content_preferences: Mapping[str, Any]
    public_contacts: Mapping[str, str]
    claims: tuple[BusinessClaim, ...]
    # Stage 3B1: регион работы бизнеса (свободный текст, например "Москва
    # и область" или "вся Россия"). Отдельно от markets (тот про
    # рынки/валюты, а не про географию обслуживания клиентов). Значение по
    # умолчанию сохраняет обратную совместимость существующих конструкторов
    # BusinessContext(...) без этого поля.
    region: str = ""


@dataclass(frozen=True)
class BusinessProfile:
    id: int
    workspace_id: int
    business_name: str
    business_type: str
    short_description: str
    profile_status: str
    schema_version: int
    revision: int
    context: BusinessContext
    created_at: str
    updated_at: str
    # Явный признак аффилиации с Travel Advantage. Не выводится из business_type,
    # названия или описания — устанавливается только системными путями создания
    # workspace (владелец сервиса, provisioning), никогда через самостоятельное
    # редактирование профиля.
    ta_affiliated: bool = False
    # Персональная referral/enrollment ссылка ВЛАДЕЛЬЦА ИМЕННО ЭТОГО workspace
    # на Travel Advantage (2026-10-07). В отличие от ta_affiliated, это поле
    # САМОСТОЯТЕЛЬНО редактируется владельцем/админом через «⚙️ Профиль»
    # (app/handlers/profile.py) - см. PartnerRepository.update_ta_enrollment_url.
    # Строго tenant-specific: никогда не используется как общий/дефолтный URL
    # для другого workspace, никогда не имеет глобального fallback. Пусто,
    # пока владелец не укажет свою ссылку - Оркестратор не придумывает и не
    # подставляет чужую ссылку вместо неё (см. app/handlers/partner_discovery.py).
    ta_enrollment_url: str | None = None


class InvalidTaEnrollmentUrlError(ValueError):
    pass


def validate_ta_enrollment_url(raw: str) -> str:
    """Minimal validation only - http(s):// scheme, not arbitrary text. No
    referral-path/domain pattern is enforced (MWR Life's real enrollment
    link format is not confirmed yet - see the task this was built for),
    so any http(s) URL is accepted as-is, trimmed of whitespace."""
    value = (raw or "").strip()
    if not value.lower().startswith(("http://", "https://")):
        raise InvalidTaEnrollmentUrlError(
            "Ссылка должна начинаться с http:// или https://"
        )
    if " " in value or len(value) > 2048:
        raise InvalidTaEnrollmentUrlError("Похоже, это не ссылка - проверьте формат")
    return value
