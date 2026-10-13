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
see app.domain.subscription) deliberately has NO entry and is the ONLY plan
that runs unlimited, by explicit decision (see resolve_plan below). A missing
subscription or an unrecognized plan code is NOT unlimited: resolve_plan
blocks the action with a clear reason (fail-closed).
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
    """Raw table lookup: None means "no START/STANDARD/FULL entry" (beta or
    unrecognized). Enforcement code must use resolve_plan(), which tells
    explicit beta (unlimited) apart from unknown (blocked)."""
    if plan is None:
        return None
    code = plan.value if isinstance(plan, SubscriptionPlan) else str(plan)
    return PLAN_LIMITS.get(code)


def plan_display_name(plan_code: str) -> str:
    return PLAN_DISPLAY_NAMES.get(plan_code, plan_code.upper())


# ── what a workspace's plan means for enforcement ───────────────────────────
#
# Fail-CLOSED for anything that is not an explicit decision:
#   - explicit 'beta'               -> unlimited (the intended legacy mode);
#   - START / STANDARD / FULL       -> enforced limits;
#   - no subscription row, or any plan code this module does not know
#                                   -> the action is BLOCKED with a reason.
# The message never echoes the stored plan code.

NO_SUBSCRIPTION_MESSAGE = (
    "Не удалось определить тариф рабочего пространства: подписка не найдена. "
    "Действие временно недоступно - обратитесь в поддержку."
)
UNKNOWN_PLAN_MESSAGE = (
    "Тариф рабочего пространства не распознан. "
    "Действие временно недоступно - обратитесь в поддержку."
)


@dataclass(frozen=True)
class PlanResolution:
    limits: PlanLimits | None = None
    unlimited: bool = False
    block_message: str | None = None


def resolve_plan(plan: SubscriptionPlan | str | None) -> PlanResolution:
    """THE single place a plan code becomes an enforcement mode - used by
    app.services.plan_quota_service and by the atomic slot guard in
    app.repositories.slot_guard, so every channel and every write path
    interprets a plan identically."""
    if plan is None:
        return PlanResolution(block_message=NO_SUBSCRIPTION_MESSAGE)
    code = plan.value if isinstance(plan, SubscriptionPlan) else str(plan)
    if code == SubscriptionPlan.BETA.value:
        return PlanResolution(unlimited=True)
    limits = PLAN_LIMITS.get(code)
    if limits is None:
        return PlanResolution(block_message=UNKNOWN_PLAN_MESSAGE)
    return PlanResolution(limits=limits)


# ── personal owner exception ────────────────────────────────────────────────
#
# Workspaces that carry NO tariff quotas (materials, competitor analyses,
# competitor slots, source slots) because they belong to the platform owner.
# Verified against the production database, never guessed: workspace_id 1
# ("Основное рабочее пространство") is bound to u-terra@yandex.ru and to the
# owner's Telegram account (active 'owner' membership). This is a per-
# workspace exception - NOT a global switch: every other workspace keeps
# resolving its own plan through resolve_plan().
#
# It lifts QUOTAS only. Subscription status/expiry (access_state), workspace
# isolation, billing, prices and external-API limits are untouched: the
# access gate still decides whether the workspace may use the product at all.
_PRODUCTION_OWNER_WORKSPACE_IDS: frozenset[int] = frozenset({1})
OWNER_UNLIMITED_WORKSPACE_IDS: frozenset[int] = _PRODUCTION_OWNER_WORKSPACE_IDS
OWNER_ACCESS_LABEL = "Доступ владельца — без тарифных ограничений"


def is_owner_unlimited_workspace(workspace_id: int | None) -> bool:
    return workspace_id is not None and workspace_id in OWNER_UNLIMITED_WORKSPACE_IDS


def resolve_workspace_plan(
    workspace_id: int | None, plan: SubscriptionPlan | str | None,
) -> PlanResolution:
    """resolve_plan() plus the personal owner exception - the entry point
    every quota/slot decision uses (PlanQuotaService, slot_guard)."""
    if is_owner_unlimited_workspace(workspace_id):
        return PlanResolution(unlimited=True)
    return resolve_plan(plan)


def _free_slot_hint(plan_code: str) -> str:
    next_code = NEXT_TIER.get(plan_code)
    if next_code is None:
        return " Освободите место, чтобы добавить новый."
    return (
        " Освободите место или перейдите на "
        f"{plan_display_name(next_code)}."
    )


def competitor_slot_message(limits: PlanLimits) -> str:
    return (
        f"В тарифе {plan_display_name(limits.plan_code)} можно подключить до "
        f"{limits.competitor_slot_limit} конкурентов.{_free_slot_hint(limits.plan_code)}"
    )


def source_slot_message(limits: PlanLimits) -> str:
    return (
        f"В тарифе {plan_display_name(limits.plan_code)} можно подключить до "
        f"{limits.source_slot_limit} источников мониторинга."
        f"{_free_slot_hint(limits.plan_code)}"
    )
