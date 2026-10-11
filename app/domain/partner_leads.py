"""Partner Discovery 2.0: a minimal, persistent lead record for a candidate
the user has actually engaged with (saved/approved/skipped/drafted) -
everything upstream of this (search, enrichment, qualification, scoring)
stays exactly as stateless as before, see app.services.lead_qualification.

Deliberately NOT a CRM: one table, one status field, no pipeline stages
beyond the fixed set below, no analytics, no outreach history log beyond
last_contact_at/next_action. See app.repositories.partner_lead_repository.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from urllib.parse import urlsplit

from app.domain.competitor_discovery import canonical_domain

# Same multi-tenant exception app.services.partner_discovery._identity_key
# already uses for candidate dedup - one domain legitimately hosts many
# unrelated subjects, so identity there is domain+path, not domain alone.
_MULTI_TENANT_DOMAINS: frozenset[str] = frozenset({"vk.ru", "vk.com", "t.me"})


def compute_lead_identity_key(source_url: str) -> str:
    """The stable dedup key for a saved partner lead within one workspace
    - re-discovering the same candidate later must upsert, never create a
    second row. Mirrors app.services.partner_discovery._identity_key
    exactly (same multi-tenant exception) so a candidate that was already
    deduped once upstream keeps the same identity here."""
    domain = canonical_domain(source_url) if source_url else ""
    if domain in _MULTI_TENANT_DOMAINS:
        path = urlsplit(source_url).path.rstrip("/")
        return f"{domain}{path}"
    return domain


class PartnerLeadStatus(str, Enum):
    NEW = "new"
    REVIEWED = "reviewed"
    APPROVED = "approved"
    CONTACTED = "contacted"
    REPLIED = "replied"
    INTERESTED = "interested"
    DECLINED = "declined"
    FOLLOW_UP = "follow_up"


# Allowed forward transitions - deliberately not a generic state-machine
# framework, just a small lookup table. Anything not listed here is
# refused by PartnerLeadRepository.set_status (e.g. INTERESTED -> NEW is
# never in this map, so it can never happen by accident).
ALLOWED_TRANSITIONS: dict[PartnerLeadStatus, frozenset[PartnerLeadStatus]] = {
    PartnerLeadStatus.NEW: frozenset({PartnerLeadStatus.REVIEWED, PartnerLeadStatus.APPROVED, PartnerLeadStatus.DECLINED}),
    PartnerLeadStatus.REVIEWED: frozenset({PartnerLeadStatus.APPROVED, PartnerLeadStatus.DECLINED}),
    PartnerLeadStatus.APPROVED: frozenset({PartnerLeadStatus.CONTACTED, PartnerLeadStatus.DECLINED}),
    PartnerLeadStatus.CONTACTED: frozenset({PartnerLeadStatus.REPLIED, PartnerLeadStatus.FOLLOW_UP}),
    PartnerLeadStatus.FOLLOW_UP: frozenset({PartnerLeadStatus.CONTACTED, PartnerLeadStatus.DECLINED}),
    PartnerLeadStatus.REPLIED: frozenset({PartnerLeadStatus.INTERESTED, PartnerLeadStatus.DECLINED}),
    PartnerLeadStatus.INTERESTED: frozenset(),  # terminal-ish: never auto-demoted back
    PartnerLeadStatus.DECLINED: frozenset({PartnerLeadStatus.REVIEWED}),  # "skip" can be undone
}


@dataclass(frozen=True)
class PartnerLead:
    lead_id: int
    workspace_id: int
    identity_key: str
    name: str
    source_url: str
    score: int | None
    confidence: float | None
    why_fit: tuple[str, ...]
    hook: str
    contact_type: str | None
    contact_value: str | None
    status: PartnerLeadStatus
    created_at: datetime
    updated_at: datetime
    last_contact_at: datetime | None = None
    next_action: str | None = None
    notes: str | None = None
