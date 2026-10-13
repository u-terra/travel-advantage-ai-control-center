"""ORCHESTRAVEL v1 plan quota enforcement - the single place that turns
(workspace's current plan) + (workspace's recorded usage) into an allow/deny
decision, with the user-facing message already attached.

Design contract (see app.services.plan_limits / app.repositories.
plan_usage_repository for the two things this composes):

- An explicit 'beta' plan (legacy/grandfathered workspaces) gets NO limit
  enforced anywhere in this module - every check_* method returns
  allowed=True. This is the one intended unlimited mode.
- A workspace with NO subscription row, or a plan code this product does not
  recognize, is NOT unlimited: every check_* method blocks with a clear
  reason (fail-closed, see app.services.plan_limits.resolve_plan). Access
  state (trial/active/expired/...) is checked separately and unchanged.
- Rolling windows (14 days for START, 30 for STANDARD/FULL - see
  PlanLimits.window_days) are computed as "now - window_days", not tracked
  by any scheduled job - an action ages out of the count the moment enough
  wall-clock time has passed.
- check_material_quota / check_competitor_analysis_quota must be called
  BEFORE the expensive provider/LLM call, so a blocked request never reaches
  the provider. record_material_created / record_competitor_analysis_
  completed must be called ONLY after the action has genuinely succeeded
  end-to-end (an Artifact was actually created / analyze() actually
  returned intelligence) - never on failure.
- check_competitor_slot / check_source_slot take the workspace's CURRENT
  count as an argument and are a fast, friendly PRE-check only. The
  authoritative, race-free check happens inside the write transaction (see
  app.repositories.slot_guard), which uses the same resolve_plan() rules
  and the same messages. Nothing here ever deletes or disables an existing
  over-limit competitor/source.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.domain.subscription import Subscription
from app.repositories.plan_usage_repository import (
    COMPETITOR_ANALYSIS_COMPLETED,
    MATERIAL_CREATED,
    PlanUsageRepository,
)
from app.repositories.subscription_repository import SubscriptionRepository
from app.services.plan_limits import (
    NEXT_TIER,
    PlanResolution,
    competitor_slot_message,
    is_owner_unlimited_workspace,
    plan_display_name,
    resolve_workspace_plan,
    source_slot_message,
)


@dataclass(frozen=True)
class QuotaDecision:
    allowed: bool
    # Plain-language, user-facing text - never tokens/provider/internal call
    # counts. None when allowed is True.
    message: str | None = None


_ALLOWED = QuotaDecision(allowed=True)


def _upgrade_suffix(plan_code: str) -> str:
    next_code = NEXT_TIER.get(plan_code)
    if next_code is None:
        return "."
    return f" или перейти на {plan_display_name(next_code)}."


class PlanQuotaService:
    def __init__(
        self,
        subscription_repository: SubscriptionRepository,
        plan_usage_repository: PlanUsageRepository,
    ) -> None:
        self._subscriptions = subscription_repository
        self._usage = plan_usage_repository

    async def _resolution_for_workspace(self, workspace_id: int) -> PlanResolution:
        if is_owner_unlimited_workspace(workspace_id):
            return resolve_workspace_plan(workspace_id, None)  # personal owner exception
        subscription: Subscription | None = await self._subscriptions.get_for_workspace(
            workspace_id
        )
        return resolve_workspace_plan(
            workspace_id, subscription.plan if subscription is not None else None,
        )

    async def check_material_quota(self, workspace_id: int) -> QuotaDecision:
        resolution = await self._resolution_for_workspace(workspace_id)
        if resolution.block_message is not None:
            return QuotaDecision(allowed=False, message=resolution.block_message)
        limits = resolution.limits
        if limits is None:
            return _ALLOWED
        since = _window_start(limits.window_days)
        used = await self._usage.count_since(workspace_id, MATERIAL_CREATED, since)
        if used < limits.materials_limit:
            return _ALLOWED
        name = plan_display_name(limits.plan_code)
        message = (
            f"Лимит тарифа {name}: {limits.materials_limit} материалов за "
            f"{limits.window_days} дней исчерпан.\n"
            f"Можно дождаться освобождения лимита{_upgrade_suffix(limits.plan_code)}"
        )
        return QuotaDecision(allowed=False, message=message)

    async def record_material_created(self, workspace_id: int) -> None:
        await self._usage.record(workspace_id, MATERIAL_CREATED)

    async def check_competitor_analysis_quota(self, workspace_id: int) -> QuotaDecision:
        resolution = await self._resolution_for_workspace(workspace_id)
        if resolution.block_message is not None:
            return QuotaDecision(allowed=False, message=resolution.block_message)
        limits = resolution.limits
        if limits is None:
            return _ALLOWED
        since = _window_start(limits.window_days)
        used = await self._usage.count_since(
            workspace_id, COMPETITOR_ANALYSIS_COMPLETED, since,
        )
        if used < limits.competitor_analyses_limit:
            return _ALLOWED
        name = plan_display_name(limits.plan_code)
        message = (
            f"Лимит тарифа {name}: {limits.competitor_analyses_limit} "
            f"анализов конкурентов за {limits.window_days} дней исчерпан.\n"
            f"Можно дождаться освобождения лимита{_upgrade_suffix(limits.plan_code)}"
        )
        return QuotaDecision(allowed=False, message=message)

    async def record_competitor_analysis_completed(self, workspace_id: int) -> None:
        await self._usage.record(workspace_id, COMPETITOR_ANALYSIS_COMPLETED)

    async def check_competitor_slot(
        self, workspace_id: int, current_count: int,
    ) -> QuotaDecision:
        resolution = await self._resolution_for_workspace(workspace_id)
        if resolution.block_message is not None:
            return QuotaDecision(allowed=False, message=resolution.block_message)
        limits = resolution.limits
        if limits is None:
            return _ALLOWED
        if current_count < limits.competitor_slot_limit:
            return _ALLOWED
        return QuotaDecision(allowed=False, message=competitor_slot_message(limits))

    async def check_source_slot(
        self, workspace_id: int, current_count: int,
    ) -> QuotaDecision:
        resolution = await self._resolution_for_workspace(workspace_id)
        if resolution.block_message is not None:
            return QuotaDecision(allowed=False, message=resolution.block_message)
        limits = resolution.limits
        if limits is None:
            return _ALLOWED
        if current_count < limits.source_slot_limit:
            return _ALLOWED
        return QuotaDecision(allowed=False, message=source_slot_message(limits))


def _window_start(window_days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
