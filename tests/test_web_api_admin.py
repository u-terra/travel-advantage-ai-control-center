"""Beta Control Center HTTP surface (app.web_api + app.admin_api):
platform-admin gate, dashboard/workspaces/billing/errors/activity/
feedback/health/audit-log endpoints, and the security properties the task
explicitly calls out (workspace owner != platform admin, no secrets
leaked, CSRF + confirm + audit log on every mutation).

Requires the web-only dependencies (requirements-web.txt). Skips cleanly
when they're not installed.
"""

from __future__ import annotations

import asyncio

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("markdown")
pytest.importorskip("argon2")

from fastapi.testclient import TestClient  # noqa: E402

from tests._web_auth_test_helpers import login_as  # noqa: E402

ADMIN_ID = 111000111
ADMIN_EMAIL = "admin@example.com"
OWNER_ID = 586249067
OWNER_EMAIL = "owner@example.com"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def api(tmp_path, monkeypatch):
    db_path = tmp_path / "journal.sqlite3"
    monkeypatch.setenv("JOURNAL_DB_PATH", str(db_path))
    monkeypatch.setenv("PLANNER_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("ORCHESTRAVEL_ADMIN_EMAILS", ADMIN_EMAIL)

    import sys
    sys.modules.pop("app.web_api", None)
    import app.web_api as web_api

    with TestClient(web_api.app, base_url="https://testserver") as client:
        # Two separate workspaces: one belongs to the platform admin, one
        # to an ordinary workspace owner - being an owner must never imply
        # platform-admin access.
        admin_ws, _ = _run(web_api.partner_repository.ensure_owner_workspace(ADMIN_ID))
        owner = _run(web_api.partner_repository.provision_partner(
            OWNER_ID, "Owner Agency", "owner-agency",
            business_name="Owner Agency", business_type="independent_agent",
            short_description="Независимое пространство.", context={},
        ))
        yield web_api, admin_ws.id, owner.workspace.id

    sys.modules.pop("app.web_api", None)


def _admin_client(web_api, admin_workspace_id) -> TestClient:
    client = TestClient(web_api.app, base_url="https://testserver")
    login_as(client, web_api, admin_workspace_id, ADMIN_ID, email=ADMIN_EMAIL)
    return client


def _owner_client(web_api, owner_workspace_id) -> TestClient:
    client = TestClient(web_api.app, base_url="https://testserver")
    login_as(client, web_api, owner_workspace_id, OWNER_ID, email=OWNER_EMAIL, role="owner")
    return client


# ── gate: auth required, then platform-admin allowlist, fail-closed ────

def test_unauthenticated_request_gets_401_not_404(api) -> None:
    web_api, admin_ws, owner_ws = api
    client = TestClient(web_api.app, base_url="https://testserver")
    response = client.get("/api/admin/dashboard")
    assert response.status_code == 401


def test_workspace_owner_is_not_a_platform_admin(api) -> None:
    """The central claim of the task: a normal, authenticated, even
    owner-role workspace member gets the SAME 404 as a route that doesn't
    exist - never a distinct "forbidden" response."""
    web_api, admin_ws, owner_ws = api
    client = _owner_client(web_api, owner_ws)

    assert client.get("/api/admin/dashboard").status_code == 404
    assert client.get("/admin").status_code == 404
    assert client.get("/admin/workspaces").status_code == 404


def test_platform_admin_can_access(api) -> None:
    web_api, admin_ws, owner_ws = api
    client = _admin_client(web_api, admin_ws)

    assert client.get("/api/admin/dashboard").status_code == 200
    assert client.get("/admin").status_code == 200


def test_empty_admin_allowlist_locks_out_everyone(tmp_path, monkeypatch) -> None:
    """Fail-closed: no ORCHESTRAVEL_ADMIN_EMAILS at all means nobody is a
    platform admin, not even a user who happens to be the sole
    workspace owner."""
    db_path = tmp_path / "journal.sqlite3"
    monkeypatch.setenv("JOURNAL_DB_PATH", str(db_path))
    monkeypatch.setenv("PLANNER_OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("ORCHESTRAVEL_ADMIN_EMAILS", raising=False)

    import sys
    sys.modules.pop("app.web_api", None)
    import app.web_api as web_api

    with TestClient(web_api.app, base_url="https://testserver") as client:
        ws, _ = _run(web_api.partner_repository.ensure_owner_workspace(OWNER_ID))
        login_as(client, web_api, ws.id, OWNER_ID, email="onlyuser@example.com")
        response = client.get("/api/admin/dashboard")
        assert response.status_code == 404
    sys.modules.pop("app.web_api", None)


def test_admin_mutation_requires_csrf(api) -> None:
    web_api, admin_ws, owner_ws = api
    client = _admin_client(web_api, admin_ws)
    client.headers.pop("X-CSRF-Token", None)

    response = client.post(f"/api/admin/workspaces/{admin_ws}/suspend", json={"confirm": True})
    assert response.status_code == 403


# ── dashboard / no fabricated metrics ───────────────────────────────────

def test_dashboard_returns_real_zero_counts_not_fabricated(api) -> None:
    web_api, admin_ws, owner_ws = api
    client = _admin_client(web_api, admin_ws)

    response = client.get("/api/admin/dashboard")
    assert response.status_code == 200
    body = response.json()
    assert body["today"]["conversations"] == 0
    assert body["today"]["errors"] == 0
    assert "subscriptions_by_status" in body
    assert "robokassa_is_test" in body


# ── workspaces: search + isolation + detail ─────────────────────────────

def test_search_finds_owner_workspace_by_email(api) -> None:
    web_api, admin_ws, owner_ws = api
    _owner_client(web_api, owner_ws)  # registers the web-auth binding
    client = _admin_client(web_api, admin_ws)

    response = client.get("/api/admin/workspaces", params={"query": OWNER_EMAIL})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["workspaces"][0]["workspace_id"] == owner_ws


def test_search_does_not_return_unrelated_workspace(api) -> None:
    web_api, admin_ws, owner_ws = api
    _owner_client(web_api, owner_ws)
    client = _admin_client(web_api, admin_ws)

    response = client.get("/api/admin/workspaces", params={"query": "no-such-user-anywhere"})
    assert response.json()["total"] == 0


def test_workspace_detail_returns_members_and_subscription(api) -> None:
    web_api, admin_ws, owner_ws = api
    _owner_client(web_api, owner_ws)
    client = _admin_client(web_api, admin_ws)

    response = client.get(f"/api/admin/workspaces/{owner_ws}")
    assert response.status_code == 200
    body = response.json()
    assert body["workspace"]["workspace_id"] == owner_ws
    assert len(body["members"]) == 1
    assert body["subscription"]["status"] in {"beta", "active", "trial"}
    assert "recent_events" in body


def test_workspace_detail_never_exposes_secrets_or_paths(api) -> None:
    web_api, admin_ws, owner_ws = api
    _owner_client(web_api, owner_ws)
    client = _admin_client(web_api, admin_ws)

    body_text = client.get(f"/api/admin/workspaces/{owner_ws}").text.lower()
    for forbidden in ("password", "argon2", "csrf", "session_token", "robokassa_password"):
        assert forbidden not in body_text


# ── billing admin actions: repository + confirm + CSRF + audit log ─────

def test_suspend_requires_confirm_true(api) -> None:
    web_api, admin_ws, owner_ws = api
    client = _admin_client(web_api, admin_ws)

    response = client.post(f"/api/admin/workspaces/{admin_ws}/suspend", json={"confirm": False})
    assert "error" in response.json()
    subscription = _run(web_api.subscription_repository.get_for_workspace(admin_ws))
    assert subscription is None or subscription.status.value != "suspended"


def test_suspend_then_restore_uses_subscription_repository(api) -> None:
    web_api, admin_ws, owner_ws = api
    client = _admin_client(web_api, admin_ws)

    suspend_response = client.post(f"/api/admin/workspaces/{admin_ws}/suspend", json={"confirm": True})
    assert suspend_response.json()["subscription"]["status"] == "suspended"
    subscription = _run(web_api.subscription_repository.get_for_workspace(admin_ws))
    assert subscription.status.value == "suspended"

    restore_response = client.post(f"/api/admin/workspaces/{admin_ws}/restore", json={"confirm": True})
    assert restore_response.json()["subscription"]["status"] == "active"


def test_suspend_writes_an_audit_log_entry(api) -> None:
    web_api, admin_ws, owner_ws = api
    client = _admin_client(web_api, admin_ws)

    client.post(f"/api/admin/workspaces/{admin_ws}/suspend", json={"confirm": True})

    entries = _run(web_api.admin_audit_log_repository.list_recent(target_workspace_id=admin_ws))
    assert len(entries) == 1
    assert entries[0].action == "suspend"
    assert entries[0].admin_email == ADMIN_EMAIL
    assert '"status": "suspended"' in entries[0].after_json


def test_activate_extends_paid_until_and_sets_standard_plan(api) -> None:
    web_api, admin_ws, owner_ws = api
    client = _admin_client(web_api, admin_ws)

    response = client.post(f"/api/admin/workspaces/{admin_ws}/activate", json={"confirm": True, "days": 10})
    assert response.status_code == 200
    body = response.json()["subscription"]
    assert body["plan"] == "standard"
    assert body["paid_until"] is not None

    subscription = _run(web_api.subscription_repository.get_for_workspace(admin_ws))
    assert subscription.payment_provider == "admin"
    assert subscription.external_payment_id == f"admin:{ADMIN_EMAIL}"


def test_extend_trial_uses_subscription_repository_start_trial(api) -> None:
    web_api, admin_ws, owner_ws = api
    client = _admin_client(web_api, admin_ws)

    response = client.post(f"/api/admin/workspaces/{admin_ws}/extend-trial", json={"confirm": True, "days": 14})
    assert response.status_code == 200
    body = response.json()["subscription"]
    assert body["status"] == "trial"
    assert body["trial_until"] is not None


def test_activate_rejects_out_of_range_days(api) -> None:
    web_api, admin_ws, owner_ws = api
    client = _admin_client(web_api, admin_ws)

    response = client.post(f"/api/admin/workspaces/{admin_ws}/activate", json={"confirm": True, "days": 10000})
    assert "error" in response.json()


# ── feedback admin view ─────────────────────────────────────────────────

def test_feedback_status_change_requires_confirm_and_writes_audit_log(api) -> None:
    web_api, admin_ws, owner_ws = api
    from app.domain.feedback import FeedbackRating
    feedback = _run(web_api.feedback_repository.submit(
        workspace_id=admin_ws, web_user_id=1, conversation_id=1, message_id=1,
        rating=FeedbackRating.DOWN, reason="other", comment="test",
    ))
    client = _admin_client(web_api, admin_ws)

    rejected = client.post(f"/api/admin/feedback/{feedback.id}/status", json={"confirm": False, "status": "resolved"})
    assert "error" in rejected.json()

    accepted = client.post(f"/api/admin/feedback/{feedback.id}/status", json={"confirm": True, "status": "resolved"})
    assert accepted.json()["feedback"]["status"] == "resolved"

    entries = _run(web_api.admin_audit_log_repository.list_recent(target_workspace_id=admin_ws))
    assert any(e.action == "feedback_status_change" for e in entries)


# ── health: no secrets, honest about Telegram ───────────────────────────

def test_health_never_exposes_secrets(api) -> None:
    web_api, admin_ws, owner_ws = api
    client = _admin_client(web_api, admin_ws)

    body_text = client.get("/api/admin/health").text.lower()
    for forbidden in ("password", "argon2", "secret", "signaturevalue"):
        assert forbidden not in body_text


def test_health_does_not_fabricate_telegram_status(api) -> None:
    web_api, admin_ws, owner_ws = api
    client = _admin_client(web_api, admin_ws)

    body = client.get("/api/admin/health").json()
    assert "telegram_online" not in body
    assert "telegram_status" not in body
    assert "telegram_note" in body


# ── normal product flows are unaffected by the admin router mount ──────

def test_normal_user_billing_status_endpoint_still_works(api) -> None:
    web_api, admin_ws, owner_ws = api
    client = _owner_client(web_api, owner_ws)

    response = client.get("/api/billing/status")
    assert response.status_code == 200
