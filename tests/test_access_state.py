from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

from app.access_state_gate import AccessStateMiddleware
from app.domain.partners import PartnerWorkspace, WorkspaceContext
from app.services.access_state import (
    ACTIVE,
    EXPIRED,
    NO_WORKSPACE,
    SUSPENDED,
    TRIAL_ACTIVE,
    compute_access_state,
    is_access_granted,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# --- compute_access_state: чистая логика ------------------------------------

def test_active_without_expiry_stays_active() -> None:
    assert compute_access_state("active", None) == ACTIVE


def test_trial_active_without_expiry_stays_trial_active() -> None:
    assert compute_access_state("trial_active", None) == TRIAL_ACTIVE


def test_suspended_always_wins_even_with_future_expiry() -> None:
    future = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    assert compute_access_state("suspended", future) == SUSPENDED


def test_active_with_future_expiry_stays_active() -> None:
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert compute_access_state("active", future) == ACTIVE


def test_trial_active_with_past_expiry_becomes_expired() -> None:
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    assert compute_access_state("trial_active", past) == EXPIRED


def test_active_with_past_expiry_becomes_expired() -> None:
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    assert compute_access_state("active", past) == EXPIRED


def test_stored_expired_stays_expired_regardless_of_date() -> None:
    future = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    assert compute_access_state("expired", future) == EXPIRED
    assert compute_access_state("expired", None) == EXPIRED


def test_unparseable_expiry_is_ignored_not_locked_out() -> None:
    """Повреждённая дата не должна сама по себе заблокировать уже
    оплаченный доступ — fail-safe в сторону не потерять платящего клиента
    из-за проблем с данными."""
    assert compute_access_state("active", "not-a-date") == ACTIVE


def test_unknown_access_status_fails_safe_as_expired() -> None:
    assert compute_access_state("something_else", None) == EXPIRED


def test_now_parameter_is_respected_for_deterministic_tests() -> None:
    fixed_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    expires = "2026-01-01T00:00:00+00:00"
    assert compute_access_state("active", expires, now=fixed_now) == EXPIRED
    assert compute_access_state(
        "active", expires, now=fixed_now - timedelta(seconds=1)
    ) == ACTIVE


def test_is_access_granted() -> None:
    assert is_access_granted(ACTIVE) is True
    assert is_access_granted(TRIAL_ACTIVE) is True
    assert is_access_granted(EXPIRED) is False
    assert is_access_granted(SUSPENDED) is False
    assert is_access_granted(NO_WORKSPACE) is False


# --- AccessStateMiddleware ----------------------------------------------------

def _ctx(workspace_id: int = 1) -> WorkspaceContext:
    return WorkspaceContext(100, workspace_id, "owner", "active")


def test_middleware_sets_no_workspace_when_context_is_missing() -> None:
    repository = AsyncMock()
    mw = AccessStateMiddleware(repository)
    handler = AsyncMock(return_value="ok")

    result = _run(mw(handler, object(), {"workspace_context": None}))

    assert result == "ok"
    handler.assert_awaited_once()
    assert handler.await_args.args[1]["access_state"] == NO_WORKSPACE
    repository.get_workspace.assert_not_called()


def test_middleware_computes_state_from_workspace_row() -> None:
    repository = AsyncMock(get_workspace=AsyncMock(return_value=PartnerWorkspace(
        1, "W", "w", "active", "now", "now",
        access_status="trial_active", access_expires_at=None,
    )))
    mw = AccessStateMiddleware(repository)
    handler = AsyncMock(return_value="ok")

    _run(mw(handler, object(), {"workspace_context": _ctx(1)}))

    repository.get_workspace.assert_awaited_once_with(1)
    assert handler.await_args.args[1]["access_state"] == TRIAL_ACTIVE


def test_middleware_falls_back_to_no_workspace_when_row_vanished() -> None:
    """workspace_context есть, но get_workspace вернул None (рассинхрон
    данных) — fail-safe, не рабочий доступ."""
    repository = AsyncMock(get_workspace=AsyncMock(return_value=None))
    mw = AccessStateMiddleware(repository)
    handler = AsyncMock(return_value="ok")

    _run(mw(handler, object(), {"workspace_context": _ctx(1)}))

    assert handler.await_args.args[1]["access_state"] == NO_WORKSPACE
