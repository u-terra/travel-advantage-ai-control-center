"""GET/PUT /api/profile* - real, editable BusinessProfile + personal style
(WorkspaceUserPreferences) for the web shell «Профиль». Same repository
calls the existing chat endpoint already uses for personalization context
(see partner_repository.get_user_preferences() in /api/chat), plus
get_business_profile()/BusinessProfileService - the same data and the same
access-control/optimistic-concurrency Telegram's «⚙️ Профиль» already uses
(app/handlers/profile.py). No parallel profile model is created.

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

from app.domain.business_profiles import BusinessProfileValidationError  # noqa: E402

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
        yield client, web_api, db_path, ws.id

    sys.modules.pop("app.web_api", None)


def test_default_owner_workspace_has_a_real_business_profile(api) -> None:
    """ensure_owner_workspace() provisions a real, usable default profile -
    the endpoint must reflect it, not fabricate its own."""
    client, web_api, _, workspace_id = api

    response = client.get("/api/profile")

    assert response.status_code == 200
    body = response.json()
    profile = _run(web_api.partner_repository.get_business_profile(workspace_id))
    assert profile is not None
    assert body["business_profile"]["business_name"] == profile.business_name
    assert body["business_profile"]["business_type"] == profile.business_type
    assert body["business_profile"]["ta_affiliated"] == profile.ta_affiliated


def test_personal_style_is_null_until_actually_set(api) -> None:
    client, _, _, workspace_id = api

    response = client.get("/api/profile")

    assert response.json()["personal_style"] is None


def test_personal_style_reflects_real_saved_preferences(api) -> None:
    client, web_api, _, workspace_id = api
    # workspace_user_preferences has a composite FK to workspace_memberships
    # (workspace_id, telegram_user_id) - ensure_owner_workspace() (in the
    # fixture) only creates partner_workspaces/partner_profiles rows, not a
    # membership, so writing preferences needs this too (mirrors what
    # app/main.py's real startup does for the owner).
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
    _run(web_api.partner_repository.set_user_style_description(
        workspace_id, OWNER_ID, "Пишу просто и по делу.",
    ))
    _run(web_api.partner_repository.add_user_example_post(
        workspace_id, OWNER_ID, "Пример поста.",
    ))
    _run(web_api.partner_repository.set_user_avoid_phrases(
        workspace_id, OWNER_ID, ["уникальное предложение"],
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
    client, web_api, _, workspace_id = api
    saved_summary = "Внутренний рабочий конспект для Ассистента, не для показа пользователю."

    async def _save_memory() -> None:
        await web_api.workspace_memory_repository.init()
        db_path = web_api.workspace_memory_repository.db_path
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute(
                "INSERT INTO workspace_memory (workspace_id, summary, created_at, updated_at) "
                "VALUES (?, ?, 'now', 'now')",
                (workspace_id, saved_summary),
            )
            await db.commit()

    _run(_save_memory())

    record = _run(web_api.workspace_memory_repository.get(workspace_id))
    assert record is not None and record.summary == saved_summary  # sanity: really saved

    response = client.get("/api/profile")

    assert "workspace_memory" not in response.json()
    assert saved_summary not in response.text


def test_response_contains_no_secret_looking_keys(api) -> None:
    client, _, _, workspace_id = api

    response = client.get("/api/profile")
    raw = response.text.lower()

    for forbidden in ("api_key", "token", "secret", "password"):
        assert forbidden not in raw


def test_profile_isolated_by_workspace(api) -> None:
    client, web_api, _, workspace_id = api
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
    client, web_api, _, workspace_id = api

    async def broken_get(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(web_api.partner_repository, "get_business_profile", broken_get)

    response = client.get("/api/profile")

    assert response.status_code == 200
    body = response.json()
    assert "error" in body
    assert body["business_profile"] is None


def _business_payload(**overrides) -> dict:
    payload = {
        "business_name": "Обновлённое имя",
        "business_type": "agency",
        "short_description": "Новое описание.",
        "specializations": ["Азия", "Европа"],
        "destinations": ["Таиланд"],
        "region": "Москва",
        "audiences": ["Семьи"],
        "tone": "Дружелюбный",
    }
    payload.update(overrides)
    return payload


# ── PUT /api/profile/business - reuses BusinessProfileService, no new model ──

def test_update_business_profile_saves_real_fields(api) -> None:
    client, web_api, _, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))

    response = client.put("/api/profile/business", json=_business_payload())

    assert response.status_code == 200
    body = response.json()["business_profile"]
    assert body["business_name"] == "Обновлённое имя"
    assert body["specializations"] == ["Азия", "Европа"]
    assert body["region"] == "Москва"
    assert body["tone"] == "Дружелюбный"

    stored = _run(web_api.partner_repository.get_business_profile(workspace_id))
    assert stored.business_name == "Обновлённое имя"


def test_update_business_profile_is_used_by_assistant_on_next_chat_call(api, monkeypatch) -> None:
    """После сохранения новые значения должны сразу использоваться
    Ассистентом - проверяем это напрямую через то, что реально передаётся
    в chat_provider.generate(), без обращения к настоящему LLM."""
    client, web_api, _, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))

    client.put("/api/profile/business", json=_business_payload(
        business_name="Компания Феникс", short_description="Экспертиза по Азии.",
    ))

    captured = {}

    def fake_generate(**kwargs):
        captured.update(kwargs)
        from app.chat_provider import ChatResult
        return ChatResult(text="ok", usage=None)

    monkeypatch.setattr(web_api.chat_provider, "generate", fake_generate)

    conversation_id = client.post("/api/conversations").json()["conversation"]["id"]
    response = client.post(
        "/api/chat", json={"message": "Привет", "conversation_id": conversation_id},
    )

    assert response.status_code == 200
    assert "Компания Феникс" in captured["knowledge_context"]
    assert "Экспертиза по Азии" in captured["knowledge_context"]


def test_update_business_profile_without_membership_is_rejected(api) -> None:
    """Fail closed: get_current_principal() re-checks PartnerRepository's
    own access model on every request - a session alone (from login_as() in
    the fixture, which auto-granted an active membership) is not enough
    once that membership is gone. Deactivating it here simulates an admin
    revoking access after the session already exists; the request must be
    rejected at the auth layer (403), before it ever reaches the
    endpoint's own body."""
    client, web_api, _, workspace_id = api
    _run(web_api.partner_repository.set_partner_membership_status(OWNER_ID, "inactive"))

    response = client.put("/api/profile/business", json=_business_payload())

    assert response.status_code == 403
    stored = _run(web_api.partner_repository.get_business_profile(workspace_id))
    assert stored.business_name != "Обновлённое имя"


def test_ta_affiliated_workspace_cannot_change_business_type(api) -> None:
    """Same rule as Telegram's on_profile_field_selected(): ta_affiliated
    profiles keep their business_type regardless of what the client sends."""
    client, web_api, _, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
    profile = _run(web_api.partner_repository.get_business_profile(workspace_id))
    assert profile.ta_affiliated is True  # the default owner profile is TA-affiliated

    response = client.put(
        "/api/profile/business",
        json=_business_payload(business_type="travel_company"),
    )

    assert response.status_code == 200
    assert response.json()["business_profile"]["business_type"] == profile.business_type


def test_update_business_profile_rejects_invalid_business_type(api) -> None:
    """The endpoint's error handling covers BusinessProfileValidationError -
    exercised here directly against the same repository method the endpoint
    calls, since the web fixture's own workspace happens to be
    ta_affiliated (business_type is silently ignored there by design)."""
    client, web_api, _, workspace_id = api
    other = _run(web_api.partner_repository.provision_partner(
        222334333, "Independent Agency", "independent-agency-validation",
        business_name="Independent", business_type="independent_agent",
        short_description="x", context={},
    ))

    with pytest.raises(BusinessProfileValidationError):
        _run(web_api.partner_repository.update_business_profile(
            other.workspace.id, 1,
            business_name="X", business_type="not-a-real-type",
            short_description="x", context={},
        ))


def test_update_business_profile_endpoint_never_returns_500(api, monkeypatch) -> None:
    """A broken access check must fail closed (403), never 500 and never
    silently let the request through - see get_current_principal()."""
    client, web_api, _, workspace_id = api

    async def broken_resolve(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(web_api.partner_repository, "resolve_workspace_context", broken_resolve)

    response = client.put("/api/profile/business", json=_business_payload())

    assert response.status_code == 403
    stored = _run(web_api.partner_repository.get_business_profile(workspace_id))
    assert stored.business_name != "Обновлённое имя"


def test_update_business_profile_isolated_by_workspace(api) -> None:
    """resolve_workspace_context() is keyed off WEB_TELEGRAM_USER_ID, which
    only ever resolves to WEB_WORKSPACE_ID in this fixture - a foreign
    workspace's profile must stay untouched."""
    client, web_api, _, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
    other = _run(web_api.partner_repository.provision_partner(
        222334444, "Other Agency 5", "other-agency-profile-5",
        business_name="Чужой бизнес 5", business_type="independent_agent",
        short_description="x", context={},
    ))

    client.put("/api/profile/business", json=_business_payload(business_name="Захват"))

    other_profile = _run(web_api.partner_repository.get_business_profile(other.workspace.id))
    assert other_profile.business_name == "Чужой бизнес 5"


# ── PUT /api/profile/style + examples - existing preference methods only ────

def test_update_personal_style_saves_description_and_avoid_phrases(api) -> None:
    client, web_api, _, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))

    response = client.put("/api/profile/style", json={
        "style_description": "Пишу с юмором.",
        "avoid_phrases": ["уникальное предложение", "успейте купить"],
    })

    assert response.status_code == 200
    style = response.json()["personal_style"]
    assert style["style_description"] == "Пишу с юмором."
    assert style["avoid_phrases"] == ["уникальное предложение", "успейте купить"]


def test_update_personal_style_is_used_by_assistant_on_next_chat_call(api, monkeypatch) -> None:
    client, web_api, _, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))

    client.put("/api/profile/style", json={
        "style_description": "Пишу тепло и просто, всегда на «вы».",
        "avoid_phrases": [],
    })

    captured = {}

    def fake_generate(**kwargs):
        captured.update(kwargs)
        from app.chat_provider import ChatResult
        return ChatResult(text="ok", usage=None)

    monkeypatch.setattr(web_api.chat_provider, "generate", fake_generate)

    conversation_id = client.post("/api/conversations").json()["conversation"]["id"]
    response = client.post(
        "/api/chat", json={"message": "Привет", "conversation_id": conversation_id},
    )

    assert response.status_code == 200
    assert captured["personal_style"] == "Пишу тепло и просто, всегда на «вы»."


def test_update_personal_style_does_not_touch_example_posts(api) -> None:
    client, web_api, _, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
    _run(web_api.partner_repository.add_user_example_post(
        workspace_id, OWNER_ID, "Уже сохранённый пример.",
    ))

    response = client.put("/api/profile/style", json={
        "style_description": "Новый стиль.", "avoid_phrases": [],
    })

    assert response.json()["personal_style"]["example_posts"] == ["Уже сохранённый пример."]


def test_add_example_post_appends_a_real_example(api) -> None:
    client, web_api, _, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))

    response = client.post("/api/profile/style/examples", json={"text": "Мой пример поста."})

    assert response.status_code == 200
    assert response.json()["personal_style"]["example_posts"] == ["Мой пример поста."]


def test_add_example_post_rejects_blank_text(api) -> None:
    client, web_api, _, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))

    response = client.post("/api/profile/style/examples", json={"text": "   "})

    assert response.status_code == 200
    body = response.json()
    assert "error" in body
    preferences = _run(web_api.partner_repository.get_user_preferences(
        workspace_id, OWNER_ID,
    ))
    assert preferences is None or preferences.example_posts == ()


def test_add_example_post_enforces_max_five(api) -> None:
    client, web_api, _, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
    for index in range(5):
        _run(web_api.partner_repository.add_user_example_post(
            workspace_id, OWNER_ID, f"Пример {index}.",
        ))

    response = client.post("/api/profile/style/examples", json={"text": "Шестой пример."})

    assert response.status_code == 200
    body = response.json()
    assert "error" in body
    preferences = _run(web_api.partner_repository.get_user_preferences(
        workspace_id, OWNER_ID,
    ))
    assert len(preferences.example_posts) == 5


def test_clear_example_posts_removes_all_of_them(api) -> None:
    client, web_api, _, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
    _run(web_api.partner_repository.add_user_example_post(
        workspace_id, OWNER_ID, "Пример.",
    ))

    response = client.delete("/api/profile/style/examples")

    assert response.status_code == 200
    assert response.json()["personal_style"]["example_posts"] == []


def test_style_endpoints_are_isolated_by_workspace(api) -> None:
    """Style/avoid-phrase writes always target WEB_WORKSPACE_ID +
    WEB_TELEGRAM_USER_ID explicitly - a foreign workspace's preferences
    must stay untouched."""
    client, web_api, _, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
    other = _run(web_api.partner_repository.provision_partner(
        222334555, "Other Agency 6", "other-agency-style-6",
        business_name="Other", business_type="independent_agent",
        short_description="x", context={},
    ))
    _run(web_api.partner_repository.set_user_style_description(
        other.workspace.id, 222334555, "Чужой стиль общения.",
    ))

    client.put("/api/profile/style", json={
        "style_description": "Мой стиль.", "avoid_phrases": [],
    })

    other_preferences = _run(web_api.partner_repository.get_user_preferences(
        other.workspace.id, 222334555,
    ))
    assert other_preferences.style_description == "Чужой стиль общения."


def test_style_endpoint_never_returns_500(api, monkeypatch) -> None:
    client, web_api, _, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))

    async def broken_set(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(web_api.partner_repository, "set_user_style_description", broken_set)

    response = client.put("/api/profile/style", json={
        "style_description": "x", "avoid_phrases": [],
    })

    assert response.status_code == 200
    assert "error" in response.json()
