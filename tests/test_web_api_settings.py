"""GET /api/settings - read-only workspace parameters (name/slug/status/
access) for the web shell «Настройки». No env vars, no API keys, no system
config - only real PartnerWorkspace fields already used elsewhere
(app.repositories.partner_repository.get_workspace()).

Requires the web-only dependencies (requirements-web.txt: fastapi,
uvicorn, markdown). Skips cleanly when they're not installed.
"""

from __future__ import annotations

import asyncio

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("markdown")

from fastapi.testclient import TestClient  # noqa: E402


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

    with TestClient(web_api.app) as client:
        yield client, web_api, db_path

    sys.modules.pop("app.web_api", None)


def test_workspace_not_yet_provisioned_has_no_500(api) -> None:
    client, _, _ = api

    response = client.get("/api/settings")

    assert response.status_code == 200
    body = response.json()
    assert body["workspace"] is None
    assert "error" in body


def test_returns_real_workspace_fields(api) -> None:
    client, web_api, _ = api
    _run(web_api.partner_repository.ensure_owner_workspace(web_api.WEB_TELEGRAM_USER_ID))

    response = client.get("/api/settings")

    assert response.status_code == 200
    body = response.json()
    workspace = body["workspace"]
    real = _run(web_api.partner_repository.get_workspace(web_api.WEB_WORKSPACE_ID))
    assert workspace["name"] == real.name
    assert workspace["slug"] == real.slug
    assert workspace["status"] == real.status
    assert workspace["access_status"] == real.access_status
    assert workspace["access_expires_at"] == real.access_expires_at


def test_response_contains_no_secret_looking_keys(api) -> None:
    client, web_api, _ = api
    _run(web_api.partner_repository.ensure_owner_workspace(web_api.WEB_TELEGRAM_USER_ID))

    response = client.get("/api/settings")
    raw = response.text.lower()

    for forbidden in ("api_key", "token", "secret", "password", "bot_token"):
        assert forbidden not in raw


def test_response_is_a_minimal_explicit_projection(api) -> None:
    """Guards against silently growing this endpoint to leak internal
    PartnerWorkspace fields that aren't meant to be public settings."""
    client, web_api, _ = api
    _run(web_api.partner_repository.ensure_owner_workspace(web_api.WEB_TELEGRAM_USER_ID))

    response = client.get("/api/settings")

    assert set(response.json()["workspace"].keys()) == {
        "name", "slug", "status", "access_status", "access_expires_at",
    }


def test_endpoint_never_returns_500_on_backend_error(api, monkeypatch) -> None:
    client, web_api, _ = api
    _run(web_api.partner_repository.ensure_owner_workspace(web_api.WEB_TELEGRAM_USER_ID))

    async def broken_get(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(web_api.partner_repository, "get_workspace", broken_get)

    response = client.get("/api/settings")

    assert response.status_code == 200
    body = response.json()
    assert "error" in body
    assert body["workspace"] is None
