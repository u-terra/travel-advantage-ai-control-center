"""ORCHESTRAVEL v1 plan limits - THE single source of truth for what each
real paid tariff (START/STANDARD/FULL) actually allows.

Deliberately separate from app.services.plans.PLAN_CATALOG: that module
owns price/duration (what a payment charges/extends), this one owns
*product* limits (what the workspace may do while on that plan). Neither
duplicates the other's numbers - PLAN_CATALOG is still the only place an
amount/duration is written, this is the only place a limit is written, and
app.services.plan_quota_service is the only place either is READ to make an
allow/deny decision. No limit number is hardcoded anywhere else.

Only SubscriptionPlan.START/STANDARD/FULL are enforced tariffs here.
SubscriptionPlan.BETA (every legacy/grandfathered/CLI-provisioned workspace,
see app.domain.subscription) deliberately has NO entry - get_plan_limits()
returns None for it, and every caller in app.services.plan_quota_service
treats None as "no limit enforced" (fail-open), so no existing legacy/beta
workspace is newly blocked by this feature.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.content import ARTIFACT_TYPES
from app.domain.subscription import SubscriptionPlan

# Which of app.domain.content.ARTIFACT_TYPES count as a quota-relevant
# "material" (per the product rule: "1 успешно созданный пользователем
# материал = 1 единица"). Everything ELSE in ARTIFACT_TYPES ("post",
# "video_script", "stories", "objection_reply", "faq", "content_plan_item" -
# checked against the actual domain list, never invented here) is a real
# user-facing content material and counts. Excluded on purpose:
#   - "client_message": «Ответить клиенту» / assistant client-reply drafts -
#     explicitly NOT a material per the product spec, regardless of how
#     much Content Factory work went into it.
#   - "other": app.handlers.text_review's "save already-reviewed text"
#     Artifact - not a Content Factory/material-orchestration generation at
#     all (no LLM call happens in that save path), so it is not a
#     "generated material" in the first place.
# Today only "post" is ever actually produced by any real code path (the
# rest are schema-reserved for future material types - see
# app.handlers.materials._ARTIFACT_TYPE_LABELS) - this predicate stays
# correct without another edit if/when one of them ships.
_NON_MATERIAL_ARTIFACT_TYPES = frozenset({"client_message", "other"})


def is_quota_counted_material(artifact_type: str) -> bool:
    """True for an Artifact type that must consume 1 unit of the workspace's
    material quota when successfully created - see
    app.services.plan_quota_service.check_material_quota/
    record_material_created, the only intended callers of this predicate."""
    return artifact_type in ARTIFACT_TYPES and artifact_type not in _NON_MATERIAL_ARTIFACT_TYPES

# Display names shown to users - deliberately just the bare tariff name
# (matches the UX copy the product spec asks for: "Лимит тарифа STANDARD:
# ..."), not app.services.plans.PlanDefinition.label (which reads
# "STANDARD / 30 дней" - meant for the billing plan-picker list, not for a
# quota message).
PLAN_DISPLAY_NAMES: dict[str, str] = {
    "start": "START",
    "standard": "STANDARD",
    "full": "FULL",
}

# The plan a blocked user on this tariff would upgrade to - None for FULL
# (top tier, nothing to upsell to). Used only for the UX message's "или
# перейти на <TIER>" suffix.
NEXT_TIER: dict[str, str] = {
    "start": "standard",
    "standard": "full",
}


@dataclass(frozen=True)
class PlanLimits:
    plan_code: str
    # Rolling-window logical-action quotas (app.services.plan_quota_service
    # counts app.repositories.plan_usage_repository rows, never raw LLM/
    # provider call counts - see that module's docstring).
    materials_limit: int
    competitor_analyses_limit: int
    # Same rolling window for both quotas on a given plan - per product spec
    # (START: 14 days, STANDARD/FULL: 30 days).
    window_days: int
    # Slot limits: how many CURRENTLY CONNECTED competitors/sources a
    # workspace may have. Enforced only on the ADD action (see
    # app.services.plan_quota_service.check_competitor_slot/
    # check_source_slot) - never by removing/disabling anything a workspace
    # already has, even if it is already above a new, lower limit.
    competitor_slot_limit: int
    source_slot_limit: int


PLAN_LIMITS: dict[str, PlanLimits] = {
    "start": PlanLimits(
        plan_code="start",
        materials_limit=15,
        competitor_analyses_limit=3,
        window_days=14,
        competitor_slot_limit=3,
        source_slot_limit=5,
    ),
    "standard": PlanLimits(
        plan_code="standard",
        materials_limit=40,
        competitor_analyses_limit=10,
        window_days=30,
        competitor_slot_limit=10,
        source_slot_limit=15,
    ),
    "full": PlanLimits(
        plan_code="full",
        materials_limit=100,
        competitor_analyses_limit=30,
        window_days=30,
        competitor_slot_limit=25,
        source_slot_limit=40,
    ),
}


def get_plan_limits(plan: SubscriptionPlan | str | None) -> PlanLimits | None:
    """None means "no enforced limit for this plan" - the plan is
    beta/legacy, or unrecognized. Callers must treat None as fail-open, not
    as a zero limit."""
    if plan is None:
        return None
    code = plan.value if isinstance(plan, SubscriptionPlan) else str(plan)
    return PLAN_LIMITS.get(code)


def plan_display_name(plan_code: str) -> str:
    return PLAN_DISPLAY_NAMES.get(plan_code, plan_code.upper())
