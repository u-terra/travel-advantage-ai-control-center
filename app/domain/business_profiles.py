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
