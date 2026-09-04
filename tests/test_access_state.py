from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

from app.access_state_gate import AccessStateMiddleware
from app.domain.partners import WorkspaceContext
from app.domain.subscription import SubscriptionStatus
from app.services.access_state import (
    ACTIVE,
    EXPIRED,
    NO_WORKSPACE,
    PAST_DUE,
    SUSPENDED,
    TRIAL_ACTIVE,
    compute_access_state,
    is_access_granted,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# --- compute_access_state: чистая логика ------------------------------------

def test_active_without_expiry_stays_active() -> None:
    assert compute_access_state(SubscriptionStatus.ACTIVE, None, None) == ACTIVE


def test_beta_without_expiry_is_granted_as_active() -> None:
    """beta - тот же грант рабочего доступа, что и active, просто другой
    план/происхождение подписки (см. app/domain/subscription.py)."""
    assert compute_access_state(SubscriptionStatus.BETA, None, None) == ACTIVE


def test_trial_without_expiry_stays_trial_active() -> None:
    assert compute_access_state(SubscriptionStatus.TRIAL, None, None) == TRIAL_ACTIVE


def test_suspended_always_wins_even_with_future_dates() -> None:
    future = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    assert compute_access_state(SubscriptionStatus.SUSPENDED, future, future) == SUSPENDED


def test_past_due_blocks_regardless_of_dates() -> None:
    future = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    assert compute_access_state(SubscriptionStatus.PAST_DUE, None, future) == PAST_DUE
    assert compute_access_state(SubscriptionStatus.PAST_DUE, None, None) == PAST_DUE


def test_active_with_future_paid_until_stays_active() -> None:
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert compute_access_state(SubscriptionStatus.ACTIVE, None, future) == ACTIVE


def test_trial_with_past_trial_until_becomes_expired() -> None:
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    assert compute_access_state(SubscriptionStatus.TRIAL, past, None) == EXPIRED


def test_active_with_past_paid_until_becomes_expired() -> None:
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    assert compute_access_state(SubscriptionStatus.ACTIVE, None, past) == EXPIRED


def test_stored_expired_stays_expired_regardless_of_dates() -> None:
    future = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    assert compute_access_state(SubscriptionStatus.EXPIRED, future, future) == EXPIRED
    assert compute_access_state(SubscriptionStatus.EXPIRED, None, None) == EXPIRED


def test_missing_paid_until_stays_unlimited_active() -> None:
    """None (дата явно отсутствует) - легитимная семантика "бессрочно" для
    active/beta, не ошибка."""
    assert compute_access_state(SubscriptionStatus.ACTIVE, None, None) == ACTIVE
    assert compute_access_state(SubscriptionStatus.BETA, None, None) == ACTIVE


def test_missing_trial_until_stays_trial_active() -> None:
    assert compute_access_state(SubscriptionStatus.TRIAL, None, None) == TRIAL_ACTIVE


def test_malformed_paid_until_fails_closed_as_expired() -> None:
    """billing/access: непарсибельная (но НЕ отсутствующая) дата - это
    порча данных, а не "бессрочно". Fail-closed, не fail-open."""
    assert compute_access_state(SubscriptionStatus.ACTIVE, None, "not-a-date") == EXPIRED
    assert compute_access_state(SubscriptionStatus.BETA, None, "not-a-date") == EXPIRED


def test_malformed_trial_until_fails_closed_as_expired() -> None:
    assert compute_access_state(SubscriptionStatus.TRIAL, "not-a-date", None) == EXPIRED


def test_malformed_but_irrelevant_date_does_not_matter() -> None:
    """paid_until мусорный, но status=trial - решает trial_until, а не
    paid_until, так что мусор в неиспользуемом поле не должен ничего
    ломать."""
    assert compute_access_state(
        SubscriptionStatus.TRIAL, None, "not-a-date",
    ) == TRIAL_ACTIVE


def test_now_parameter_is_respected_for_deterministic_tests() -> None:
    fixed_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    expires = "2026-01-01T00:00:00+00:00"
    assert compute_access_state(
        SubscriptionStatus.ACTIVE, None, expires, now=fixed_now,
    ) == EXPIRED
    assert compute_access_state(
        SubscriptionStatus.ACTIVE, None, expires, now=fixed_now - timedelta(seconds=1),
    ) == ACTIVE


def test_is_access_granted() -> None:
    assert is_access_granted(ACTIVE) is True
    assert is_access_granted(TRIAL_ACTIVE) is True
    assert is_access_granted(EXPIRED) is False
    assert is_access_granted(SUSPENDED) is False
    assert is_access_granted(PAST_DUE) is False
    assert is_access_granted(NO_WORKSPACE) is False


# --- AccessStateMiddleware: источник - SubscriptionRepository ---------------

def _ctx(workspace_id: int = 1) -> WorkspaceContext:
    return WorkspaceContext(100, workspace_id, "owner", "active")


def test_middleware_sets_no_workspace_when_context_is_missing() -> None:
    subscription_repository = AsyncMock()
    mw = AccessStateMiddleware(subscription_repository)
    handler = AsyncMock(return_value="ok")

    result = _run(mw(handler, object(), {"workspace_context": None}))

    assert result == "ok"
    handler.assert_awaited_once()
    assert handler.await_args.args[1]["access_state"] == NO_WORKSPACE
    subscription_repository.resolve_access_state.assert_not_called()


def test_middleware_uses_subscription_repository_resolve_access_state() -> None:
    subscription_repository = AsyncMock(
        resolve_access_state=AsyncMock(return_value=TRIAL_ACTIVE),
    )
    mw = AccessStateMiddleware(subscription_repository)
    handler = AsyncMock(return_value="ok")

    _run(mw(handler, object(), {"workspace_context": _ctx(1)}))

    subscription_repository.resolve_access_state.assert_awaited_once_with(1)
    assert handler.await_args.args[1]["access_state"] == TRIAL_ACTIVE


def test_middleware_passes_through_expired_and_suspended_states() -> None:
    for state in (EXPIRED, SUSPENDED, PAST_DUE):
        subscription_repository = AsyncMock(
            resolve_access_state=AsyncMock(return_value=state),
        )
        mw = AccessStateMiddleware(subscription_repository)
        handler = AsyncMock(return_value="ok")

        _run(mw(handler, object(), {"workspace_context": _ctx(1)}))

        assert handler.await_args.args[1]["access_state"] == state
