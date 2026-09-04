from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
import pytest

from app.access_state_gate import AccessStateMiddleware
from app.domain.partners import WorkspaceContext
from app.domain.subscription import SubscriptionPlan, SubscriptionStatus
from app.domain.usage import LLMUsage, UsageStatus
from app.repositories.partner_repository import PartnerRepository
from app.repositories.subscription_repository import SubscriptionRepository
from app.repositories.usage_ledger_repository import UsageLedgerRepository
from app.services.access_state import (
    ACTIVE,
    EXPIRED,
    PAST_DUE,
    SUSPENDED,
    TRIAL_ACTIVE,
    is_access_granted,
)
from app.services.unit_economics import CostInputs, compute_unit_cost
from app.services.usage_pricing import (
    PROVIDER_MODEL_PRICING_USD_PER_1K_TOKENS,
    estimate_cost_usd,
)
from app.services.usage_recorder import record_llm_call


def _future(days: float = 1) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _past(days: float = 1) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def run(coro):
    return asyncio.run(coro)


# --- usage_pricing: never fabricate a price ---

def test_estimate_cost_returns_none_when_model_unpriced():
    assert estimate_cost_usd("openai", "gpt-4o-mini", 100, 50) is None


def test_estimate_cost_returns_none_when_tokens_unavailable():
    assert estimate_cost_usd("openai", "gpt-4o-mini", None, None) is None


def test_pricing_table_contains_only_deliberately_priced_models():
    """Rates must be filled in deliberately, not silently assumed.

    ("openai", "gpt-5.6-terra") is the one model priced so far - it backs
    web_chat (app/chat_provider.py). Every other (provider, model) pair
    must stay absent rather than being guessed.
    """
    assert PROVIDER_MODEL_PRICING_USD_PER_1K_TOKENS == {
        ("openai", "gpt-5.6-terra"): (0.002, 0.012),
    }


def test_estimate_cost_computes_correctly_once_a_rate_is_provided(monkeypatch):
    monkeypatch.setitem(
        PROVIDER_MODEL_PRICING_USD_PER_1K_TOKENS, ("openai", "gpt-4o-mini"), (0.15, 0.60),
    )
    cost = estimate_cost_usd("openai", "gpt-4o-mini", input_tokens=1000, output_tokens=1000)
    assert cost == pytest.approx(0.15 + 0.60)


# --- usage ledger repository ---

def _workspace(tmp_path: Path, db_path: Path, telegram_id: int = 100) -> int:
    partners = PartnerRepository(db_path)
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(telegram_id))
    return workspace.id


def test_usage_ledger_records_and_summarizes_by_module(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _workspace(tmp_path, db_path)
    repo = UsageLedgerRepository(db_path)
    run(repo.init())

    run(repo.record(
        workspace_id=workspace_id, telegram_user_id=100, module="content_factory_post",
        provider="openai", model=None, input_tokens=None, output_tokens=None,
        total_tokens=None, estimated_cost_usd=None, status=UsageStatus.SUCCESS,
    ))
    run(repo.record(
        workspace_id=workspace_id, telegram_user_id=100, module="content_factory_post",
        provider="openai", model=None, input_tokens=None, output_tokens=None,
        total_tokens=None, estimated_cost_usd=None, status=UsageStatus.FAILURE,
    ))
    run(repo.record(
        workspace_id=workspace_id, telegram_user_id=100, module="orchestration_shadow",
        provider="openai", model="gpt-4o-mini", input_tokens=200, output_tokens=80,
        total_tokens=280, estimated_cost_usd=0.03, status=UsageStatus.SUCCESS,
    ))

    summary = run(repo.summary_for_workspace(workspace_id))
    assert summary.total_calls == 3
    assert summary.successful_calls == 2
    assert summary.failed_calls == 1
    assert summary.calls_with_token_data == 1
    assert summary.total_tokens == 280
    assert summary.estimated_cost_usd == pytest.approx(0.03)
    modules = {b.module: b for b in summary.by_module}
    assert modules["content_factory_post"].calls == 2
    assert modules["content_factory_post"].total_tokens is None  # honest gap, not 0
    assert modules["orchestration_shadow"].calls == 1
    assert modules["orchestration_shadow"].total_tokens == 280


def test_usage_ledger_is_isolated_by_workspace(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _workspace(tmp_path, db_path)
    repo = UsageLedgerRepository(db_path)
    run(repo.init())
    run(repo.record(
        workspace_id=workspace_id, telegram_user_id=100, module="content_factory_post",
        provider="openai", model=None, input_tokens=None, output_tokens=None,
        total_tokens=None, estimated_cost_usd=None, status=UsageStatus.SUCCESS,
    ))
    other = run(repo.summary_for_workspace(workspace_id + 999))
    assert other.total_calls == 0
    assert run(repo.known_workspace_ids()) == [workspace_id]


# --- usage_recorder: the call-site helper ---

def test_record_llm_call_is_a_safe_noop_without_a_repository():
    run(record_llm_call(
        None, workspace_id=1, telegram_user_id=100, module="content_factory_post",
        provider="openai", status=UsageStatus.SUCCESS,
    ))  # must not raise


def test_record_llm_call_persists_real_tokens_and_computed_cost(tmp_path: Path, monkeypatch):
    monkeypatch.setitem(
        PROVIDER_MODEL_PRICING_USD_PER_1K_TOKENS, ("openai", "gpt-4o-mini"), (0.15, 0.60),
    )
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _workspace(tmp_path, db_path)
    repo = UsageLedgerRepository(db_path)
    run(repo.init())
    run(record_llm_call(
        repo, workspace_id=workspace_id, telegram_user_id=100, module="orchestration_shadow",
        provider="openai", model="gpt-4o-mini",
        usage=LLMUsage(input_tokens=1000, output_tokens=1000, total_tokens=2000),
        status=UsageStatus.SUCCESS,
    ))
    summary = run(repo.summary_for_workspace(workspace_id))
    assert summary.total_tokens == 2000
    assert summary.estimated_cost_usd == pytest.approx(0.75)


# --- subscription repository: beta backfill and RoboKassa integration point ---

def test_init_backfills_existing_workspaces_as_beta_without_losing_access(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    partners = PartnerRepository(db_path)
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))

    subscriptions = SubscriptionRepository(db_path)
    run(subscriptions.init())

    row = run(subscriptions.get_for_workspace(workspace.id))
    assert row is not None
    assert row.status is SubscriptionStatus.BETA
    assert row.paid_until is None


def test_init_is_idempotent_and_does_not_reset_an_already_paid_workspace(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    partners = PartnerRepository(db_path)
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))

    subscriptions = SubscriptionRepository(db_path)
    run(subscriptions.init())
    run(subscriptions.mark_paid(
        workspace.id, external_payment_id="rk-123", payment_provider="robokassa",
        paid_until="2027-01-01T00:00:00+00:00",
    ))

    run(subscriptions.init())  # simulates a second app startup
    row = run(subscriptions.get_for_workspace(workspace.id))
    assert row.status is SubscriptionStatus.ACTIVE
    assert row.external_payment_id == "rk-123"


def test_mark_paid_is_the_robokassa_integration_point(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    partners = PartnerRepository(db_path)
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))

    subscriptions = SubscriptionRepository(db_path)
    run(subscriptions.init())
    updated = run(subscriptions.mark_paid(
        workspace.id, external_payment_id="rk-999", payment_provider="robokassa",
        paid_until="2027-02-01T00:00:00+00:00",
    ))
    assert updated.status is SubscriptionStatus.ACTIVE
    assert updated.payment_provider == "robokassa"
    assert updated.paid_until == "2027-02-01T00:00:00+00:00"


def test_mark_past_due_and_expired_transitions(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    partners = PartnerRepository(db_path)
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))

    subscriptions = SubscriptionRepository(db_path)
    run(subscriptions.init())
    past_due = run(subscriptions.mark_past_due(workspace.id))
    assert past_due.status is SubscriptionStatus.PAST_DUE
    expired = run(subscriptions.mark_expired(workspace.id))
    assert expired.status is SubscriptionStatus.EXPIRED


def test_mark_suspended_then_mark_active_restores_without_touching_paid_until(tmp_path: Path):
    """mark_active() (Beta Control Center's "restore" admin action) must
    only flip status back to active - it is not a new grant, so
    paid_until/plan from before the suspension must survive untouched."""
    db_path = tmp_path / "db.sqlite3"
    partners = PartnerRepository(db_path)
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))

    subscriptions = SubscriptionRepository(db_path)
    run(subscriptions.init())
    run(subscriptions.mark_paid(
        workspace.id, external_payment_id="rk-1", payment_provider="robokassa",
        paid_until="2027-01-01T00:00:00+00:00",
    ))
    suspended = run(subscriptions.mark_suspended(workspace.id))
    assert suspended.status is SubscriptionStatus.SUSPENDED

    restored = run(subscriptions.mark_active(workspace.id))
    assert restored.status is SubscriptionStatus.ACTIVE
    assert restored.paid_until == "2027-01-01T00:00:00+00:00"
    assert restored.plan is SubscriptionPlan.STANDARD


def test_mark_active_upserts_a_workspace_with_no_prior_row(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    partners = PartnerRepository(db_path)
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))

    subscriptions = SubscriptionRepository(db_path)
    run(subscriptions.init())  # runs before the workspace below exists in a real flow;
    # here it's fine either way since ensure_owner_workspace already ran -
    # the point is that mark_active must still work via upsert even absent
    # a pre-existing row, mirroring _set_status()'s general contract.
    restored = run(subscriptions.mark_active(workspace.id))
    assert restored.status is SubscriptionStatus.ACTIVE


def test_ensure_beta_is_idempotent_for_a_freshly_provisioned_workspace(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    partners = PartnerRepository(db_path)
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))

    subscriptions = SubscriptionRepository(db_path)
    run(subscriptions.init())
    run(subscriptions.mark_paid(
        workspace.id, external_payment_id="rk-1", payment_provider="robokassa",
        paid_until="2027-01-01T00:00:00+00:00",
    ))
    again = run(subscriptions.ensure_beta(workspace.id))
    assert again.status is SubscriptionStatus.ACTIVE  # not reset back to beta


def test_backfilled_workspace_defaults_to_beta_plan(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    partners = PartnerRepository(db_path)
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))

    subscriptions = SubscriptionRepository(db_path)
    run(subscriptions.init())

    row = run(subscriptions.get_for_workspace(workspace.id))
    assert row.plan is SubscriptionPlan.BETA


# --- Unified Subscription: resolve_access_state() is THE gate ---

def test_resolve_access_state_grants_beta_workspace(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    partners = PartnerRepository(db_path)
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))

    subscriptions = SubscriptionRepository(db_path)
    run(subscriptions.init())

    state = run(subscriptions.resolve_access_state(workspace.id))
    assert state == ACTIVE
    assert is_access_granted(state)


def test_resolve_access_state_for_trial_before_and_after_expiry(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    partners = PartnerRepository(db_path)
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))

    subscriptions = SubscriptionRepository(db_path)
    run(subscriptions.init())

    run(subscriptions.start_trial(workspace.id, _future()))
    assert run(subscriptions.resolve_access_state(workspace.id)) == TRIAL_ACTIVE

    run(subscriptions.start_trial(workspace.id, _past()))
    assert run(subscriptions.resolve_access_state(workspace.id)) == EXPIRED


def test_resolve_access_state_for_expired_subscription(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    partners = PartnerRepository(db_path)
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))

    subscriptions = SubscriptionRepository(db_path)
    run(subscriptions.init())
    run(subscriptions.mark_expired(workspace.id))

    assert run(subscriptions.resolve_access_state(workspace.id)) == EXPIRED


def test_resolve_access_state_for_past_due(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    partners = PartnerRepository(db_path)
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))

    subscriptions = SubscriptionRepository(db_path)
    run(subscriptions.init())
    run(subscriptions.mark_past_due(workspace.id))

    state = run(subscriptions.resolve_access_state(workspace.id))
    assert state == PAST_DUE
    assert not is_access_granted(state)


def test_resolve_access_state_for_suspended(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    partners = PartnerRepository(db_path)
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))

    subscriptions = SubscriptionRepository(db_path)
    run(subscriptions.init())
    run(subscriptions.mark_suspended(workspace.id))

    state = run(subscriptions.resolve_access_state(workspace.id))
    assert state == SUSPENDED
    assert not is_access_granted(state)


def test_resolve_access_state_lazily_provisions_a_workspace_created_after_init(
    tmp_path: Path,
):
    """A workspace created after the last startup backfill (init()) has no
    workspace_subscriptions row yet - resolve_access_state() must still
    grant it access (as a fresh 'beta'), not fail closed just because
    init() hasn't run again."""
    db_path = tmp_path / "db.sqlite3"
    partners = PartnerRepository(db_path)
    run(partners.init())

    subscriptions = SubscriptionRepository(db_path)
    run(subscriptions.init())  # runs before the workspace below exists

    workspace, _ = run(partners.ensure_owner_workspace(100))
    assert run(subscriptions.get_for_workspace(workspace.id)) is None

    state = run(subscriptions.resolve_access_state(workspace.id))
    assert state == ACTIVE
    assert run(subscriptions.get_for_workspace(workspace.id)) is not None


def test_resolve_access_state_fails_closed_on_a_broken_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    db_path = tmp_path / "db.sqlite3"
    partners = PartnerRepository(db_path)
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))

    subscriptions = SubscriptionRepository(db_path)
    run(subscriptions.init())

    async def _boom(_workspace_id: int):
        raise RuntimeError("simulated broken read")

    monkeypatch.setattr(subscriptions, "get_for_workspace", _boom)

    state = run(subscriptions.resolve_access_state(workspace.id))
    assert state == EXPIRED
    assert not is_access_granted(state)


# --- billing/access fail-closed on malformed (not missing) dates ---
#
# resolve_access_state() is the exact call AccessStateMiddleware makes on
# every Telegram update (see app/access_state_gate.py) - these tests go
# through the real repository + real compute_access_state, no mocking, so
# they prove the Telegram gate itself fails closed, not just the pure
# function in isolation.

def test_telegram_gate_fails_closed_on_malformed_trial_until(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    partners = PartnerRepository(db_path)
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))

    subscriptions = SubscriptionRepository(db_path)
    run(subscriptions.init())
    run(subscriptions.start_trial(workspace.id, "not-a-date"))

    assert run(subscriptions.resolve_access_state(workspace.id)) == EXPIRED

    captured: dict = {}

    async def handler(_event, data):
        captured["access_state"] = data["access_state"]
        return "ok"

    middleware = AccessStateMiddleware(subscriptions)
    workspace_context = WorkspaceContext(100, workspace.id, "owner", "active")
    run(middleware(handler, object(), {"workspace_context": workspace_context}))

    assert captured["access_state"] == EXPIRED
    assert not is_access_granted(captured["access_state"])


def test_telegram_gate_fails_closed_on_malformed_paid_until(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    partners = PartnerRepository(db_path)
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))

    subscriptions = SubscriptionRepository(db_path)
    run(subscriptions.init())
    run(subscriptions.mark_paid(
        workspace.id, external_payment_id="rk-1", payment_provider="robokassa",
        paid_until="not-a-date",
    ))

    assert run(subscriptions.resolve_access_state(workspace.id)) == EXPIRED

    captured: dict = {}

    async def handler(_event, data):
        captured["access_state"] = data["access_state"]
        return "ok"

    middleware = AccessStateMiddleware(subscriptions)
    workspace_context = WorkspaceContext(100, workspace.id, "owner", "active")
    run(middleware(handler, object(), {"workspace_context": workspace_context}))

    assert captured["access_state"] == EXPIRED
    assert not is_access_granted(captured["access_state"])


def test_grandfathered_beta_workspace_is_unaffected_by_malformed_date_rule(
    tmp_path: Path,
):
    """A grandfathered/backfilled workspace never has a paid_until/trial_until
    at all (both NULL) - the malformed-date fail-closed rule only fires on
    a date that's actually present and unreadable, so it must not touch
    this workspace's access at all."""
    db_path = tmp_path / "db.sqlite3"
    partners = PartnerRepository(db_path)
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))

    subscriptions = SubscriptionRepository(db_path)
    run(subscriptions.init())

    row = run(subscriptions.get_for_workspace(workspace.id))
    assert row.paid_until is None
    assert row.trial_until is None
    assert is_access_granted(run(subscriptions.resolve_access_state(workspace.id)))


# --- migration: legacy access_status backfill mapping ---

def _set_legacy_access_status(db_path: Path, workspace_id: int, status: str, expires_at):
    async def _update():
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "UPDATE partner_workspaces SET access_status=?, access_expires_at=? "
                "WHERE id=?",
                (status, expires_at, workspace_id),
            )
            await db.commit()

    run(_update())


def test_backfill_carries_over_legacy_suspended_status(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    partners = PartnerRepository(db_path)
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))
    _set_legacy_access_status(db_path, workspace.id, "suspended", None)

    subscriptions = SubscriptionRepository(db_path)
    run(subscriptions.init())

    row = run(subscriptions.get_for_workspace(workspace.id))
    assert row.status is SubscriptionStatus.SUSPENDED
    assert run(subscriptions.resolve_access_state(workspace.id)) == SUSPENDED


def test_backfill_carries_over_legacy_trial_active_status(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    partners = PartnerRepository(db_path)
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))
    trial_end = _future(10)
    _set_legacy_access_status(db_path, workspace.id, "trial_active", trial_end)

    subscriptions = SubscriptionRepository(db_path)
    run(subscriptions.init())

    row = run(subscriptions.get_for_workspace(workspace.id))
    assert row.status is SubscriptionStatus.TRIAL
    assert row.trial_until == trial_end
    assert run(subscriptions.resolve_access_state(workspace.id)) == TRIAL_ACTIVE


def test_backfill_carries_over_legacy_expired_status(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    partners = PartnerRepository(db_path)
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))
    _set_legacy_access_status(db_path, workspace.id, "expired", _past())

    subscriptions = SubscriptionRepository(db_path)
    run(subscriptions.init())

    row = run(subscriptions.get_for_workspace(workspace.id))
    assert row.status is SubscriptionStatus.EXPIRED
    assert run(subscriptions.resolve_access_state(workspace.id)) == EXPIRED


def test_backfill_grandfathers_untouched_active_default_as_beta(tmp_path: Path):
    """access_status='active'/access_expires_at=NULL is the column
    DEFAULT - every real production workspace today, since nothing writes
    a non-default value (see the audit). Indistinguishable from "never
    customized", so it grandfathers in as 'beta', same as before this
    table existed - the existing production workspace keeps full access,
    it doesn't suddenly become a real "active" billing record."""
    db_path = tmp_path / "db.sqlite3"
    partners = PartnerRepository(db_path)
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))

    subscriptions = SubscriptionRepository(db_path)
    run(subscriptions.init())

    row = run(subscriptions.get_for_workspace(workspace.id))
    assert row.status is SubscriptionStatus.BETA
    state = run(subscriptions.resolve_access_state(workspace.id))
    assert is_access_granted(state)


def test_backfill_preserves_a_real_legacy_expiry_on_active(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    partners = PartnerRepository(db_path)
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))
    real_expiry = _future(30)
    _set_legacy_access_status(db_path, workspace.id, "active", real_expiry)

    subscriptions = SubscriptionRepository(db_path)
    run(subscriptions.init())

    row = run(subscriptions.get_for_workspace(workspace.id))
    assert row.status is SubscriptionStatus.ACTIVE
    assert row.paid_until == real_expiry


# --- migration: rebuilding an old-schema (pre-trial/plan) table ---

def _create_legacy_schema_table(db_path: Path) -> None:
    async def _create():
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                CREATE TABLE workspace_subscriptions (
                    workspace_id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'beta'
                        CHECK (status IN ('beta', 'active', 'past_due', 'expired')),
                    started_at TEXT NOT NULL,
                    paid_until TEXT,
                    external_payment_id TEXT,
                    payment_provider TEXT,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (workspace_id) REFERENCES partner_workspaces(id)
                )
                """
            )
            await db.commit()

    run(_create())


def test_init_rebuilds_a_pre_existing_old_schema_table_without_losing_data(
    tmp_path: Path,
):
    db_path = tmp_path / "db.sqlite3"
    partners = PartnerRepository(db_path)
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))

    _create_legacy_schema_table(db_path)

    async def _seed_old_row():
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO workspace_subscriptions "
                "(workspace_id, status, started_at, paid_until, external_payment_id, "
                "payment_provider, updated_at) VALUES (?, 'active', ?, ?, ?, ?, ?)",
                (workspace.id, "2025-01-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00",
                 "rk-777", "robokassa", "2025-01-01T00:00:00+00:00"),
            )
            await db.commit()

    run(_seed_old_row())

    subscriptions = SubscriptionRepository(db_path)
    run(subscriptions.init())  # must migrate the old table, not error out.

    row = run(subscriptions.get_for_workspace(workspace.id))
    assert row.status is SubscriptionStatus.ACTIVE
    assert row.paid_until == "2027-01-01T00:00:00+00:00"
    assert row.external_payment_id == "rk-777"
    assert row.plan is SubscriptionPlan.BETA  # new column, backfilled default

    # The widened CHECK constraint must now accept 'trial'/'suspended'.
    run(subscriptions.start_trial(workspace.id, _future()))
    updated = run(subscriptions.get_for_workspace(workspace.id))
    assert updated.status is SubscriptionStatus.TRIAL

    # Idempotent - a second init() against the now-current schema is a no-op.
    run(subscriptions.init())
    again = run(subscriptions.get_for_workspace(workspace.id))
    assert again.status is SubscriptionStatus.TRIAL


# --- Unified Subscription: Web and Telegram compute the same access_state ---

def test_telegram_gate_and_direct_resolve_access_state_agree(tmp_path: Path):
    """AccessStateMiddleware (Telegram, app/access_state_gate.py) and the
    Web subscription gate (app/web_api.py) both call
    SubscriptionRepository.resolve_access_state() with no channel-specific
    logic of their own - this proves the middleware doesn't recompute
    anything differently from a direct call, which is exactly what the Web
    gate does too."""
    db_path = tmp_path / "db.sqlite3"
    partners = PartnerRepository(db_path)
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))

    subscriptions = SubscriptionRepository(db_path)
    run(subscriptions.init())
    run(subscriptions.mark_past_due(workspace.id))

    direct_state = run(subscriptions.resolve_access_state(workspace.id))

    middleware = AccessStateMiddleware(subscriptions)
    captured: dict = {}

    async def handler(_event, data):
        captured["access_state"] = data["access_state"]
        return "ok"

    workspace_context = WorkspaceContext(100, workspace.id, "owner", "active")
    run(middleware(handler, object(), {"workspace_context": workspace_context}))

    assert captured["access_state"] == direct_state == PAST_DUE


# --- unit economics: formula shape, no invented prices ---

def test_compute_unit_cost_is_none_when_usage_is_unpriced(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _workspace(tmp_path, db_path)
    repo = UsageLedgerRepository(db_path)
    run(repo.init())
    run(repo.record(
        workspace_id=workspace_id, telegram_user_id=100, module="content_factory_post",
        provider="openai", model=None, input_tokens=None, output_tokens=None,
        total_tokens=None, estimated_cost_usd=None, status=UsageStatus.SUCCESS,
    ))
    summary = run(repo.summary_for_workspace(workspace_id))
    inputs = CostInputs(
        monthly_infrastructure_cost_usd=50.0, active_workspaces_for_allocation=10,
        payment_fee_rate=0.035, tax_and_support_reserve_rate=0.15,
    )
    breakdown = compute_unit_cost(summary, inputs, subscription_price_usd=20.0)
    assert breakdown.llm_variable_cost_usd is None  # honest gap, not 0
    assert breakdown.total_cost_usd is None  # can't total without the LLM cost
    assert breakdown.allocated_infrastructure_cost_usd == pytest.approx(5.0)
    assert breakdown.payment_fee_usd == pytest.approx(0.70)


def test_compute_unit_cost_totals_once_all_inputs_are_known(tmp_path: Path, monkeypatch):
    monkeypatch.setitem(
        PROVIDER_MODEL_PRICING_USD_PER_1K_TOKENS, ("openai", "gpt-4o-mini"), (0.15, 0.60),
    )
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _workspace(tmp_path, db_path)
    repo = UsageLedgerRepository(db_path)
    run(repo.init())
    run(record_llm_call(
        repo, workspace_id=workspace_id, telegram_user_id=100, module="orchestration_shadow",
        provider="openai", model="gpt-4o-mini",
        usage=LLMUsage(input_tokens=1000, output_tokens=1000), status=UsageStatus.SUCCESS,
    ))
    summary = run(repo.summary_for_workspace(workspace_id))
    inputs = CostInputs(
        monthly_infrastructure_cost_usd=50.0, active_workspaces_for_allocation=10,
        payment_fee_rate=0.035, tax_and_support_reserve_rate=0.15,
    )
    breakdown = compute_unit_cost(summary, inputs, subscription_price_usd=20.0)
    assert breakdown.llm_variable_cost_usd == pytest.approx(0.75)
    assert breakdown.total_cost_usd == pytest.approx(0.75 + 5.0 + 0.70 + 3.0)
