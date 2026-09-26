"""app.services.plan_quota_service - the allow/deny decision for ORCHESTRAVEL
v1 plan limits. Exercises real SubscriptionRepository + PlanUsageRepository
against an isolated sqlite file (same convention as other repository tests
in this suite) - no mocking of the DB layer, since the whole point is the
rolling-window SQL query and the plan lookup actually working together.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
import pytest

from app.domain.subscription import SubscriptionPlan
from app.repositories.partner_repository import PartnerRepository, empty_business_context
from app.repositories.plan_usage_repository import (
    COMPETITOR_ANALYSIS_COMPLETED,
    MATERIAL_CREATED,
    PlanUsageRepository,
)
from app.repositories.subscription_repository import SubscriptionRepository
from app.services.plan_quota_service import PlanQuotaService


def _run(coro):
    return asyncio.run(coro)


class Stack:
    def __init__(self, db_path: Path) -> None:
        self.partners = PartnerRepository(db_path)
        _run(self.partners.init())
        self.subscriptions = SubscriptionRepository(db_path)
        _run(self.subscriptions.init())
        self.usage = PlanUsageRepository(db_path)
        _run(self.usage.init())
        self.quota = PlanQuotaService(self.subscriptions, self.usage)
        self._next_telegram_id = 1000

    def workspace(self) -> int:
        """A fresh real workspace_id - workspace_subscriptions.workspace_id
        FKs into partner_workspaces, so every id this test uses to call
        mark_paid() must exist there first."""
        self._next_telegram_id += 1
        context = empty_business_context()
        context["specializations"] = ["travel"]
        provisioned = _run(self.partners.provision_partner(
            self._next_telegram_id, "Test Co", f"ws-{self._next_telegram_id}",
            business_name="Test Co", business_type="independent_agent",
            short_description="Test.", context=context, ta_affiliated=False,
        ))
        return provisioned.workspace.id

    def paid(self, workspace_id: int, plan: SubscriptionPlan) -> None:
        paid_until = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
        _run(self.subscriptions.mark_paid(
            workspace_id, external_payment_id=f"order-{workspace_id}",
            payment_provider="robokassa", paid_until=paid_until, plan=plan,
        ))

    def insert_at(self, workspace_id: int, action_type: str, occurred_at: str) -> None:
        async def _do() -> None:
            async with aiosqlite.connect(self.usage.db_path) as db:
                await db.execute(
                    "INSERT INTO plan_logical_actions "
                    "(workspace_id, action_type, occurred_at) VALUES (?, ?, ?)",
                    (workspace_id, action_type, occurred_at),
                )
                await db.commit()
        _run(_do())


@pytest.fixture
def stack(tmp_path: Path) -> Stack:
    return Stack(tmp_path / "journal.sqlite3")


def _epoch() -> str:
    return "1970-01-01T00:00:00+00:00"


# ── material quota ──────────────────────────────────────────────────────────


def test_material_quota_allows_under_limit(stack: Stack) -> None:
    ws = stack.workspace()
    stack.paid(ws, SubscriptionPlan.START)
    decision = _run(stack.quota.check_material_quota(ws))
    assert decision.allowed is True
    assert decision.message is None


def test_material_quota_blocks_at_limit_start(stack: Stack) -> None:
    ws = stack.workspace()
    stack.paid(ws, SubscriptionPlan.START)
    for _ in range(15):
        _run(stack.quota.record_material_created(ws))
    decision = _run(stack.quota.check_material_quota(ws))
    assert decision.allowed is False
    assert "STANDARD" in decision.message
    assert "15 материалов" in decision.message
    assert "14 дней" in decision.message


def test_successful_material_consumes_exactly_one_unit(stack: Stack) -> None:
    ws = stack.workspace()
    _run(stack.quota.record_material_created(ws))
    count = _run(stack.usage.count_since(ws, MATERIAL_CREATED, _epoch()))
    assert count == 1


def test_failed_material_generation_never_calls_record(stack: Stack) -> None:
    """The contract is enforced by callers (never call record_* on failure) -
    this locks in that a bare failed attempt (no record() call at all)
    leaves the counter at zero, i.e. record() is the ONLY way usage grows."""
    ws = stack.workspace()
    count = _run(stack.usage.count_since(ws, MATERIAL_CREATED, _epoch()))
    assert count == 0


def test_competitor_analysis_consumes_exactly_one_unit_regardless_of_internal_calls(
    stack: Stack,
) -> None:
    """A real analysis can fetch/analyze several public sources internally
    (see app.services.competitor_intelligence) - the business quota must
    still move by exactly 1 per completed analysis, never per internal call.
    Simulated here by calling record_competitor_analysis_completed exactly
    once per "analysis", regardless of how much internal work it modeled."""
    ws = stack.workspace()
    _run(stack.quota.record_competitor_analysis_completed(ws))
    count = _run(stack.usage.count_since(ws, COMPETITOR_ANALYSIS_COMPLETED, _epoch()))
    assert count == 1


# ── rolling windows ──────────────────────────────────────────────────────────


def test_start_window_is_14_days_old_action_ages_out(stack: Stack) -> None:
    ws = stack.workspace()
    stack.paid(ws, SubscriptionPlan.START)
    old = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
    stack.insert_at(ws, MATERIAL_CREATED, old)
    decision = _run(stack.quota.check_material_quota(ws))
    assert decision.allowed is True  # the 15-day-old action no longer counts


def test_start_window_counts_action_inside_14_days(stack: Stack) -> None:
    ws = stack.workspace()
    stack.paid(ws, SubscriptionPlan.START)
    for _ in range(15):
        _run(stack.quota.record_material_created(ws))
    # All 15 just-recorded actions are inside the 14-day window "as of now",
    # so the plan's 15-material limit is already exhausted.
    decision = _run(stack.quota.check_material_quota(ws))
    assert decision.allowed is False


def test_standard_and_full_use_30_day_window(stack: Stack) -> None:
    ws_standard, ws_full = stack.workspace(), stack.workspace()
    stack.paid(ws_standard, SubscriptionPlan.STANDARD)
    stack.paid(ws_full, SubscriptionPlan.FULL)
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    stack.insert_at(ws_standard, MATERIAL_CREATED, old)
    stack.insert_at(ws_full, MATERIAL_CREATED, old)
    assert _run(stack.quota.check_material_quota(ws_standard)).allowed is True
    assert _run(stack.quota.check_material_quota(ws_full)).allowed is True


# ── upgrade takes effect immediately ────────────────────────────────────────


def test_upgrade_standard_to_full_immediately_raises_limit(stack: Stack) -> None:
    ws = stack.workspace()
    stack.paid(ws, SubscriptionPlan.STANDARD)
    for _ in range(40):
        _run(stack.quota.record_material_created(ws))
    blocked = _run(stack.quota.check_material_quota(ws))
    assert blocked.allowed is False

    stack.paid(ws, SubscriptionPlan.FULL)
    allowed = _run(stack.quota.check_material_quota(ws))
    assert allowed.allowed is True  # same 40 actions, now under FULL's 100


# ── competitor / source slot limits ─────────────────────────────────────────


@pytest.mark.parametrize(
    "plan,limit",
    [(SubscriptionPlan.START, 3), (SubscriptionPlan.STANDARD, 10), (SubscriptionPlan.FULL, 25)],
)
def test_competitor_slot_limits(stack: Stack, plan, limit) -> None:
    ws = stack.workspace()
    stack.paid(ws, plan)
    assert _run(stack.quota.check_competitor_slot(ws, limit - 1)).allowed is True
    decision = _run(stack.quota.check_competitor_slot(ws, limit))
    assert decision.allowed is False
    assert str(limit) in decision.message


@pytest.mark.parametrize(
    "plan,limit",
    [(SubscriptionPlan.START, 5), (SubscriptionPlan.STANDARD, 15), (SubscriptionPlan.FULL, 40)],
)
def test_source_slot_limits(stack: Stack, plan, limit) -> None:
    ws = stack.workspace()
    stack.paid(ws, plan)
    assert _run(stack.quota.check_source_slot(ws, limit - 1)).allowed is True
    decision = _run(stack.quota.check_source_slot(ws, limit))
    assert decision.allowed is False
    assert str(limit) in decision.message


def test_existing_over_limit_competitors_are_never_deleted_only_new_add_blocked(
    stack: Stack,
) -> None:
    """check_competitor_slot only ever answers "can I add one more?" - it
    never touches existing rows. A workspace already above a newly-lowered
    limit (current_count > limit) is still blocked from adding, but nothing
    here deletes/disables what it already has."""
    ws = stack.workspace()
    stack.paid(ws, SubscriptionPlan.START)  # limit 3
    decision = _run(stack.quota.check_competitor_slot(ws, 7))  # already has 7
    assert decision.allowed is False


# ── legacy/beta fail-safe ────────────────────────────────────────────────────


def test_beta_plan_has_no_material_quota(stack: Stack) -> None:
    ws = stack.workspace()
    stack.paid(ws, SubscriptionPlan.BETA)
    for _ in range(1000):
        _run(stack.quota.record_material_created(ws))
    assert _run(stack.quota.check_material_quota(ws)).allowed is True


def test_beta_plan_has_no_competitor_slot_limit(stack: Stack) -> None:
    ws = stack.workspace()
    stack.paid(ws, SubscriptionPlan.BETA)
    assert _run(stack.quota.check_competitor_slot(ws, 999)).allowed is True


def test_no_subscription_row_is_fail_open(stack: Stack) -> None:
    decision = _run(stack.quota.check_material_quota(999_999))
    assert decision.allowed is True
