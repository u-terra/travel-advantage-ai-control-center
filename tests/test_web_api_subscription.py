"""Unified Subscription: the Web gate (get_active_principal/
require_csrf_and_subscription in app/web_api.py) must grant/deny access
identically to Telegram's AccessStateMiddleware, because both resolve
through the same SubscriptionRepository.resolve_access_state() call - see
app/services/access_state.py and app/repositories/subscription_repository.py.

Requires the web-only dependencies (requirements-web.txt: fastapi,
uvicorn, markdown, argon2-cffi). Skips cleanly when they're not installed.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("markdown")
pytest.importorskip("argon2")

from fastapi.testclient import TestClient  # noqa: E402

from tests._web_auth_test_helpers import login_as  # noqa: E402

OWNER_ID = 586249067


def _run(coro):
    return asyncio.run(coro)


def _future(days: float = 1) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _past(days: float = 1) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


@pytest.fixture
def api(tmp_path, monkeypatch):
    db_path = tmp_path / "journal.sqlite3"
    monkeypatch.setenv("JOURNAL_DB_PATH", str(db_path))
    monkeypatch.setenv("PLANNER_OPENAI_API_KEY", "test-key")

    import sys
    sys.modules.pop("app.web_api", None)
    import app.web_api as web_api

    with TestClient(web_api.app, base_url="https://testserver") as client:
        ws, _ = _run(web_api.partner_repository.ensure_owner_workspace(OWNER_ID))
        login_as(client, web_api, ws.id, OWNER_ID)
        yield client, web_api, ws.id

    sys.modules.pop("app.web_api", None)


# ── granted states reach the product API ────────────────────────────────

def test_default_beta_workspace_reaches_gated_read_endpoint(api) -> None:
    """No mutation - the state every workspace ends up in right after
    login_as()/registration, i.e. the "existing production workspace after
    migration keeps access" case (backfilled to 'beta' by
    SubscriptionRepository.init(), which the TestClient startup event
    already ran)."""
    client, _, _ = api
    response = client.get("/api/materials")
    assert response.status_code == 200
    assert "error" not in response.json()


def test_trial_active_workspace_reaches_gated_read_endpoint(api) -> None:
    client, web_api, workspace_id = api
    _run(web_api.subscription_repository.start_trial(workspace_id, _future()))

    response = client.get("/api/materials")
    assert response.status_code == 200


def test_active_workspace_reaches_gated_write_endpoint(api) -> None:
    client, web_api, workspace_id = api
    _run(web_api.subscription_repository.mark_paid(
        workspace_id, external_payment_id="rk-1", payment_provider="robokassa",
        paid_until=_future(30),
    ))

    response = client.post("/api/competitors", json={"url": "https://example.com"})
    assert response.status_code == 200


# ── blocked states get 402 on the product API, both read and write ─────

def test_expired_trial_blocks_read_endpoint(api) -> None:
    client, web_api, workspace_id = api
    _run(web_api.subscription_repository.start_trial(workspace_id, _past()))

    response = client.get("/api/materials")
    assert response.status_code == 402
    assert response.json()["detail"]["access_state"] == "expired"


def test_expired_subscription_blocks_read_endpoint(api) -> None:
    client, web_api, workspace_id = api
    _run(web_api.subscription_repository.mark_expired(workspace_id))

    response = client.get("/api/materials")
    assert response.status_code == 402
    assert response.json()["detail"]["error"] == "subscription_inactive"
    assert response.json()["detail"]["access_state"] == "expired"


def test_past_due_blocks_same_way_as_expired(api) -> None:
    client, web_api, workspace_id = api
    _run(web_api.subscription_repository.mark_past_due(workspace_id))

    response = client.get("/api/materials")
    assert response.status_code == 402
    assert response.json()["detail"]["access_state"] == "past_due"


def test_suspended_blocks_read_endpoint(api) -> None:
    client, web_api, workspace_id = api
    _run(web_api.subscription_repository.mark_suspended(workspace_id))

    response = client.get("/api/materials")
    assert response.status_code == 402
    assert response.json()["detail"]["access_state"] == "suspended"


def test_expired_subscription_blocks_write_endpoint_even_with_valid_csrf(api) -> None:
    client, web_api, workspace_id = api
    _run(web_api.subscription_repository.mark_expired(workspace_id))

    response = client.post("/api/competitors", json={"url": "https://example.com"})
    assert response.status_code == 402


# ── billing/access fail-closed on malformed (not missing) dates ────────

def test_malformed_trial_until_blocks_read_endpoint(api) -> None:
    client, web_api, workspace_id = api
    _run(web_api.subscription_repository.start_trial(workspace_id, "not-a-date"))

    response = client.get("/api/materials")
    assert response.status_code == 402
    assert response.json()["detail"]["access_state"] == "expired"


def test_malformed_paid_until_blocks_read_endpoint(api) -> None:
    client, web_api, workspace_id = api
    _run(web_api.subscription_repository.mark_paid(
        workspace_id, external_payment_id="rk-1", payment_provider="robokassa",
        paid_until="not-a-date",
    ))

    response = client.get("/api/materials")
    assert response.status_code == 402
    assert response.json()["detail"]["access_state"] == "expired"


def test_malformed_paid_until_blocks_write_endpoint(api) -> None:
    client, web_api, workspace_id = api
    _run(web_api.subscription_repository.mark_paid(
        workspace_id, external_payment_id="rk-1", payment_provider="robokassa",
        paid_until="not-a-date",
    ))

    response = client.post("/api/competitors", json={"url": "https://example.com"})
    assert response.status_code == 402


def test_grandfathered_workspace_with_no_dates_is_unaffected(api) -> None:
    """No mutation at all - the default post-login_as() state has both
    trial_until and paid_until = NULL, never "malformed", so the
    fail-closed-on-malformed-date rule must not touch it."""
    client, _, _ = api
    response = client.get("/api/materials")
    assert response.status_code == 200


# ── auth/account endpoints stay reachable regardless of subscription ───

def test_me_stays_accessible_and_reports_state_when_expired(api) -> None:
    client, web_api, workspace_id = api
    _run(web_api.subscription_repository.mark_expired(workspace_id))

    response = client.get("/api/auth/me")
    assert response.status_code == 200
    body = response.json()
    assert body["access_state"] == "expired"
    assert body["access_granted"] is False


def test_me_reports_granted_true_for_active_workspace(api) -> None:
    client, _, _ = api
    response = client.get("/api/auth/me")
    assert response.status_code == 200
    assert response.json()["access_granted"] is True


def test_logout_stays_accessible_when_subscription_expired(api) -> None:
    client, web_api, workspace_id = api
    _run(web_api.subscription_repository.mark_expired(workspace_id))

    response = client.post("/api/auth/logout")
    assert response.status_code == 200
    assert response.json() == {"logged_out": True}


def test_register_does_not_require_an_active_subscription(api) -> None:
    """Registration itself never checks workspace_subscriptions - only the
    invite's own validity and PartnerRepository's membership model (see
    app.web_api.register). A workspace with an expired subscription can
    still onboard a second web account; only the PRODUCT endpoints are
    gated."""
    client, web_api, workspace_id = api
    _run(web_api.subscription_repository.mark_expired(workspace_id))

    from app.services.web_auth_tokens import generate_token, hash_token

    raw_invite = generate_token()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    _run(web_api.web_auth_repository.create_invite(
        workspace_id, OWNER_ID, hash_token(raw_invite), expires_at,
    ))

    fresh_client = TestClient(web_api.app, base_url="https://testserver")
    response = fresh_client.post("/api/auth/register", json={
        "invite_token": raw_invite, "email": "second@example.com",
        "password": "correcthorsebattery-test-suite",
    })
    assert response.status_code == 200
    assert "error" not in response.json()


# ── layering: membership gate still wins, independent of subscription ──

def test_inactive_membership_blocks_before_subscription_is_even_checked(api) -> None:
    """get_current_principal's own membership/lifecycle check (not
    subscription-related at all) must still deny access on its own terms
    (403) even when the workspace's subscription is fully active - the two
    gates are independent layers, not a single merged check."""
    client, web_api, _workspace_id = api

    _run(web_api.partner_repository.set_partner_membership_status(OWNER_ID, "inactive"))

    response = client.get("/api/materials")
    assert response.status_code == 403
