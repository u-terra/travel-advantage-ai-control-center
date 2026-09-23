"""POST /api/telegram/bind-token (see app.web_api) - the web side of the
one-time Telegram-connect deep link. The Telegram-side consumption lives
in app.handlers.start (see tests/test_start_telegram_bind.py).

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

    web_api.signup_rate_limiter._events.clear()

    with TestClient(web_api.app, base_url="https://testserver") as client:
        yield client, web_api

    sys.modules.pop("app.web_api", None)


def _signup(client) -> int:
    response = client.post("/api/auth/signup", json={
        "name": "Test", "email": "bind@example.com",
        "password": "correcthorsebattery-bind", "business_name": "Bind Co",
    })
    assert "error" not in response.json(), response.json()
    csrf = client.cookies.get("ta_csrf")
    assert csrf, "expected a ta_csrf cookie after signup"
    client.headers["X-CSRF-Token"] = csrf
    return response.json()["workspace_id"]


def test_bind_token_requires_a_session(api):
    client, _ = api
    client.cookies.clear()

    response = client.post("/api/telegram/bind-token")

    assert response.status_code == 401


def test_bind_token_requires_csrf(api):
    client, _ = api
    _signup(client)
    client.headers.pop("X-CSRF-Token", None)

    response = client.post("/api/telegram/bind-token")

    assert response.status_code == 403


def test_bind_token_issues_a_deep_link_for_the_own_workspace(api):
    client, web_api = api
    workspace_id = _signup(client)

    response = client.post("/api/telegram/bind-token")

    assert response.status_code == 200
    body = response.json()
    assert "error" not in body
    assert body["deep_link"].startswith(
        f"https://t.me/{web_api.settings.orchestravel_bot_username}?start="
    )

    raw_token = body["deep_link"].rsplit("start=", 1)[1]
    from app.services.web_auth_tokens import hash_token
    stored = _run(web_api.telegram_bind_token_repository.get_by_token_hash(
        hash_token(raw_token)
    ))
    assert stored is not None
    assert stored.workspace_id == workspace_id


def test_bind_token_response_never_contains_a_reusable_raw_token_twice(api):
    """Each call mints a brand-new token - the previous one is not
    reissued or extended."""
    client, _ = api
    _signup(client)

    first = client.post("/api/telegram/bind-token").json()["deep_link"]
    second = client.post("/api/telegram/bind-token").json()["deep_link"]

    assert first != second


def test_bind_token_refuses_once_telegram_already_linked(api):
    """A real, invite/CLI-provisioned workspace (positive telegram_user_id
    already) must not be able to mint ANOTHER bind token for itself - this
    endpoint is for connecting Telegram the first time, not reconnecting."""
    from tests._web_auth_test_helpers import login_as

    client, web_api = api
    ws, _ = _run(web_api.partner_repository.ensure_owner_workspace(OWNER_ID))
    login_as(client, web_api, ws.id, OWNER_ID)

    response = client.post("/api/telegram/bind-token")

    assert response.status_code == 200
    assert "error" in response.json()
