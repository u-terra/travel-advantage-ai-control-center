"""The built-in "/help" page (app.web_api): must stay reachable in every
subscription state (no subscription / trial / beta / active / expired /
suspended) - unlike "/" and "/onboarding" it is never redirected to
/subscription-inactive - and it must never widen access to the
subscription-gated product API. See task notes: "Help не должен проходить
через subscription gate."

Requires the web-only dependencies (requirements-web.txt: fastapi,
uvicorn, markdown, argon2-cffi). Skips cleanly when they're not installed.
"""

from __future__ import annotations

import asyncio

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("markdown")
pytest.importorskip("argon2")

from fastapi.testclient import TestClient  # noqa: E402

from tests._web_auth_test_helpers import login_as  # noqa: E402

OWNER_ID = 586249067


def _run(coro):
    return asyncio.run(coro)


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


# ── /help is reachable at every subscription state ──────────────────────

def test_help_page_reachable_for_default_beta_workspace(api) -> None:
    """Default post-login_as() state - see the "beta" acceptance criterion
    used identically in test_web_api_subscription.py."""
    client, _, _ = api
    response = client.get("/help")
    assert response.status_code == 200
    assert "Помощь" in response.text


def test_help_page_reachable_when_subscription_expired(api) -> None:
    client, web_api, workspace_id = api
    _run(web_api.subscription_repository.mark_expired(workspace_id))

    response = client.get("/help", follow_redirects=False)

    assert response.status_code == 200
    assert "subscription-inactive" not in response.headers.get("location", "")


def test_help_page_reachable_when_subscription_suspended(api) -> None:
    client, web_api, workspace_id = api
    _run(web_api.subscription_repository.mark_suspended(workspace_id))

    response = client.get("/help", follow_redirects=False)

    assert response.status_code == 200
    assert "subscription-inactive" not in response.headers.get("location", "")


def test_help_page_reachable_when_subscription_past_due(api) -> None:
    client, web_api, workspace_id = api
    _run(web_api.subscription_repository.mark_past_due(workspace_id))

    response = client.get("/help", follow_redirects=False)

    assert response.status_code == 200


def test_help_page_reachable_with_no_workspace_dates_at_all(api) -> None:
    """Trial/paid dates both NULL - the "no subscription yet" shape - must
    not be treated as an error state that blocks Help."""
    client, _, _ = api
    response = client.get("/help", follow_redirects=False)
    assert response.status_code == 200


# ── auth is still required (a real page, not a public one) ─────────────

def test_help_page_redirects_anonymous_visitor_to_login(api) -> None:
    client, _, _ = api
    client.cookies.clear()

    response = client.get("/help", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_help_page_redirects_to_login_with_no_session_at_all() -> None:
    import sys
    sys.modules.pop("app.web_api", None)
    import app.web_api as web_api

    with TestClient(web_api.app, base_url="https://testserver") as client:
        response = client.get("/help", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    sys.modules.pop("app.web_api", None)


# ── Help never widens access to the gated product API ───────────────────

def test_visiting_help_does_not_unlock_gated_endpoint_when_expired(api) -> None:
    client, web_api, workspace_id = api
    _run(web_api.subscription_repository.mark_expired(workspace_id))

    help_response = client.get("/help")
    assert help_response.status_code == 200

    gated_response = client.get("/api/materials")
    assert gated_response.status_code == 402
    assert gated_response.json()["detail"]["access_state"] == "expired"


def test_visiting_help_does_not_unlock_gated_write_endpoint_when_suspended(api) -> None:
    client, web_api, workspace_id = api
    _run(web_api.subscription_repository.mark_suspended(workspace_id))

    help_response = client.get("/help")
    assert help_response.status_code == 200

    gated_response = client.post("/api/competitors", json={"url": "https://example.com"})
    assert gated_response.status_code == 402


def test_help_page_itself_has_no_paid_api_calls_baked_in(api) -> None:
    """The page is static content - it must not embed calls to any
    subscription-gated product endpoint (chat, competitors, materials,
    etc.), only navigation links, so it can never be used to reach paid
    functionality while access is denied."""
    client, _, _ = api
    response = client.get("/help")
    gated_paths = [
        "/api/chat", "/api/materials", "/api/competitors",
        "/api/conversations", "/api/knowledge",
    ]
    for path in gated_paths:
        assert path not in response.text


# ── navigation surfaces the "Помощь" link ────────────────────────────────

def test_cabinet_home_page_contains_help_link(api) -> None:
    client, web_api, _workspace_id = api
    me = client.get("/api/auth/me").json()
    binding = _run(web_api.web_auth_repository.get_default_binding(
        _run(web_api.web_auth_repository.get_user_by_email(me["email"])).id
    ))
    _run(web_api.web_auth_repository.mark_onboarding_completed(binding.id))

    response = client.get("/")

    assert response.status_code == 200
    assert 'href="/help"' in response.text


def test_subscription_inactive_page_contains_help_link(api) -> None:
    client, web_api, workspace_id = api
    _run(web_api.subscription_repository.mark_expired(workspace_id))

    response = client.get("/subscription-inactive")

    assert response.status_code == 200
    assert 'href="/help"' in response.text


def test_billing_page_contains_help_link(api) -> None:
    client, _, _ = api
    response = client.get("/billing")
    assert response.status_code == 200
    assert 'href="/help"' in response.text
