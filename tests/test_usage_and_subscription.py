from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.domain.subscription import SubscriptionStatus
from app.domain.usage import LLMUsage, UsageStatus
from app.repositories.partner_repository import PartnerRepository
from app.repositories.subscription_repository import SubscriptionRepository
from app.repositories.usage_ledger_repository import UsageLedgerRepository
from app.services.unit_economics import CostInputs, compute_unit_cost
from app.services.usage_pricing import (
    PROVIDER_MODEL_PRICING_USD_PER_1K_TOKENS,
    estimate_cost_usd,
)
from app.services.usage_recorder import record_llm_call


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
