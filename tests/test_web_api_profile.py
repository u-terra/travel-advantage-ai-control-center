"""GET /api/profile - read-only BusinessProfile + personal style
(WorkspaceUserPreferences) for the web shell «Профиль». Same repository
calls the existing chat endpoint already uses for personalization context
(see partner_repository.get_user_preferences() in /api/chat), plus
get_business_profile() - the same data Telegram's «⚙️ Профиль» already
shows read-only via on_profile_view().

workspace_memory is deliberately NOT part of this response: it's internal
Assistant context (see WorkspaceMemoryRepository / /api/chat), not a
user-facing profile field - see test_workspace_memory_is_never_exposed().

Requires the web-only dependencies (requirements-web.txt: fastapi,
uvicorn, markdown). Skips cleanly when they're not installed.
"""

from __future__ import annotations

import asyncio

import aiosqlite
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
        _run(web_api.partner_repository.ensure_owner_workspace(web_api.WEB_TELEGRAM_USER_ID))
        yield client, web_api, db_path

    sys.modules.pop("app.web_api", None)


def test_default_owner_workspace_has_a_real_business_profile(api) -> None:
    """ensure_owner_workspace() provisions a real, usable default profile -
    the endpoint must reflect it, not fabricate its own."""
    client, web_api, _ = api

    response = client.get("/api/profile")

    assert response.status_code == 200
    body = response.json()
    profile = _run(web_api.partner_repository.get_business_profile(web_api.WEB_WORKSPACE_ID))
    assert profile is not None
    assert body["business_profile"]["business_name"] == profile.business_name
    assert body["business_profile"]["business_type"] == profile.business_type
    assert body["business_profile"]["ta_affiliated"] == profile.ta_affiliated


def test_personal_style_is_null_until_actually_set(api) -> None:
    client, _, _ = api

    response = client.get("/api/profile")

    assert response.json()["personal_style"] is None


def test_personal_style_reflects_real_saved_preferences(api) -> None:
    client, web_api, _ = api
    # workspace_user_preferences has a composite FK to workspace_memberships
    # (workspace_id, telegram_user_id) - ensure_owner_workspace() (in the
    # fixture) only creates partner_workspaces/partner_profiles rows, not a
    # membership, so writing preferences needs this too (mirrors what
    # app/main.py's real startup does for the owner).
    _run(web_api.partner_repository.bootstrap_owner_membership(web_api.WEB_TELEGRAM_USER_ID))
    _run(web_api.partner_repository.set_user_style_description(
        web_api.WEB_WORKSPACE_ID, web_api.WEB_TELEGRAM_USER_ID, "Пишу просто и по делу.",
    ))
    _run(web_api.partner_repository.add_user_example_post(
        web_api.WEB_WORKSPACE_ID, web_api.WEB_TELEGRAM_USER_ID, "Пример поста.",
    ))
    _run(web_api.partner_repository.set_user_avoid_phrases(
        web_api.WEB_WORKSPACE_ID, web_api.WEB_TELEGRAM_USER_ID, ["уникальное предложение"],
    ))

    response = client.get("/api/profile")

    style = response.json()["personal_style"]
    assert style["style_description"] == "Пишу просто и по делу."
    assert style["example_posts"] == ["Пример поста."]
    assert style["avoid_phrases"] == ["уникальное предложение"]


def test_workspace_memory_is_never_exposed(api) -> None:
    """workspace_memory is internal Assistant context (see /api/chat), not
    a user-facing profile field - even when a real summary is saved, it
    must not appear anywhere in the /api/profile response."""
    client, web_api, _ = api
    saved_summary = "Внутренний рабочий конспект для Ассистента, не для показа пользователю."

    async def _save_memory() -> None:
        await web_api.workspace_memory_repository.init()
        db_path = web_api.workspace_memory_repository.db_path
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute(
                "INSERT INTO workspace_memory (workspace_id, summary, created_at, updated_at) "
                "VALUES (?, ?, 'now', 'now')",
                (web_api.WEB_WORKSPACE_ID, saved_summary),
            )
            await db.commit()

    _run(_save_memory())

    record = _run(web_api.workspace_memory_repository.get(web_api.WEB_WORKSPACE_ID))
    assert record is not None and record.summary == saved_summary  # sanity: really saved

    response = client.get("/api/profile")

    assert "workspace_memory" not in response.json()
    assert saved_summary not in response.text


def test_response_contains_no_secret_looking_keys(api) -> None:
    client, _, _ = api

    response = client.get("/api/profile")
    raw = response.text.lower()

    for forbidden in ("api_key", "token", "secret", "password"):
        assert forbidden not in raw


def test_profile_isolated_by_workspace(api) -> None:
    client, web_api, _ = api
    other = _run(web_api.partner_repository.provision_partner(
        222334000, "Other Agency", "other-agency-profile",
        business_name="Чужой бизнес", business_type="independent_agent",
        short_description="Другое рабочее пространство.",
        context={},
    ))
    _run(web_api.partner_repository.set_user_style_description(
        other.workspace.id, 222334000, "Чужой стиль общения.",
    ))

    response = client.get("/api/profile")

    body = response.json()
    assert body["business_profile"]["business_name"] != "Чужой бизнес"
    assert body["personal_style"] is None


def test_endpoint_never_returns_500_on_backend_error(api, monkeypatch) -> None:
    client, web_api, _ = api

    async def broken_get(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(web_api.partner_repository, "get_business_profile", broken_get)

    response = client.get("/api/profile")

    assert response.status_code == 200
    body = response.json()
    assert "error" in body
    assert body["business_profile"] is None
