"""New-web-user onboarding: GET /onboarding, POST /api/onboarding/complete,
and the "/" redirect that gates the cabinet behind it.

No parallel onboarding-data model - business fields land in the same
BusinessProfile/workspace_user_preferences the "Профиль" tab already
edits (see app.web_api.complete_onboarding). Only the completion flag
itself is new state, scoped to the web_auth_bindings row for THIS
session (see app.repositories.web_auth_repository), not the web-user or
workspace as a whole.

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

from app.chat_provider import ChatResult  # noqa: E402

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

    with TestClient(
        web_api.app, base_url="https://testserver", follow_redirects=False,
    ) as client:
        # provision_partner(), not ensure_owner_workspace(): the latter is
        # the special TA-owner fixture (ta_affiliated=True), which would
        # lock business_type and defeat these tests' whole point of
        # picking a business_type in onboarding step 1. It also already
        # creates the 'owner' membership login_as() needs, so no separate
        # bootstrap_owner_membership() call is required below.
        provisioned = _run(web_api.partner_repository.provision_partner(
            OWNER_ID, "Onboarding Test Partner", "onboarding-test-partner",
            business_name="Исходное имя", business_type="other",
            short_description="", context={},
        ))
        yield client, web_api, provisioned.workspace.id
        sys.modules.pop("app.web_api", None)


def _complete_payload(**overrides):
    payload = {
        "who": "independent_agent",
        "business_name": "Мария Онбординг",
        "short_description": "Подбираю туры под запрос клиента.",
        "specializations": ["Пляжный отдых", "Турция"],
        "audiences": ["Семьи с детьми"],
        "region": "Москва и область",
        "tone": "friendly",
        "address_form": "ty",
    }
    payload.update(overrides)
    return payload


# ── "/" redirects a not-yet-onboarded binding to /onboarding ────────────

def test_new_binding_is_redirected_to_onboarding_from_home(api) -> None:
    client, web_api, workspace_id = api
    login_as(client, web_api, workspace_id, OWNER_ID)

    response = client.get("/")

    assert response.status_code == 303
    assert response.headers["location"] == "/onboarding"


def test_login_with_incomplete_onboarding_is_redirected_from_home(api) -> None:
    """Not just the immediate post-register session - any later ordinary
    login on the same (still incomplete) binding must land on /onboarding
    too, not just once at registration time."""
    client, web_api, workspace_id = api
    login_as(client, web_api, workspace_id, OWNER_ID)
    client.cookies.clear()

    login_response = client.post("/api/auth/login", json={
        "email": "owner@example.com", "password": "correcthorsebattery-test-suite",
    })
    assert login_response.status_code == 200
    assert "error" not in login_response.json()

    response = client.get("/")

    assert response.status_code == 303
    assert response.headers["location"] == "/onboarding"


def test_onboarding_page_is_reachable_while_incomplete(api) -> None:
    client, web_api, workspace_id = api
    login_as(client, web_api, workspace_id, OWNER_ID)

    response = client.get("/onboarding")

    assert response.status_code == 200
    assert "ORCHESTRAVEL" in response.text


def test_existing_binding_after_migration_goes_straight_to_cabinet(api) -> None:
    """A binding grandfathered in by the onboarding_completed_at backfill
    (see tests/test_web_auth_repository.py for the migration itself) must
    not be redirected to /onboarding - "/" has to serve the cabinet
    directly, exactly like it did before this feature existed."""
    client, web_api, workspace_id = api
    login_as(client, web_api, workspace_id, OWNER_ID)

    me = client.get("/api/auth/me").json()
    binding = _run(web_api.web_auth_repository.get_default_binding(
        _run(web_api.web_auth_repository.get_user_by_email(me["email"])).id
    ))
    _run(web_api.web_auth_repository.mark_onboarding_completed(binding.id))

    response = client.get("/")

    assert response.status_code == 200
    assert "chat" in response.text.lower()


# ── CSRF ──────────────────────────────────────────────────────────────────

def test_complete_onboarding_requires_csrf(api) -> None:
    client, web_api, workspace_id = api
    login_as(client, web_api, workspace_id, OWNER_ID)

    token = client.headers.pop("X-CSRF-Token")
    try:
        response = client.post(
            "/api/onboarding/complete", json=_complete_payload(),
        )
    finally:
        client.headers["X-CSRF-Token"] = token

    assert response.status_code == 403


# ── completion ────────────────────────────────────────────────────────────

def test_complete_onboarding_saves_business_profile_and_style(api) -> None:
    client, web_api, workspace_id = api
    login_as(client, web_api, workspace_id, OWNER_ID)

    response = client.post("/api/onboarding/complete", json=_complete_payload())

    assert response.status_code == 200
    body = response.json()
    assert "error" not in body
    assert body["onboarding_completed"] is True
    assert body["business_profile_saved"] is True
    assert body["business_profile"]["business_name"] == "Мария Онбординг"
    assert body["business_profile"]["business_type"] == "independent_agent"
    assert "Турция" in body["business_profile"]["specializations"]
    assert body["personal_style"]["style_description"] == "Дружелюбно. Обращайся на «ты»."

    profile = _run(web_api.partner_repository.get_business_profile(workspace_id))
    assert profile.business_name == "Мария Онбординг"
    assert list(profile.context.audiences) == ["Семьи с детьми"]
    assert profile.context.region == "Москва и область"


def test_complete_onboarding_marks_the_binding_completed(api) -> None:
    client, web_api, workspace_id = api
    login_as(client, web_api, workspace_id, OWNER_ID)

    me = client.get("/api/auth/me").json()
    binding_before = _run(web_api.web_auth_repository.get_default_binding(
        _run(web_api.web_auth_repository.get_user_by_email(me["email"])).id
    ))
    assert binding_before.onboarding_completed_at is None

    client.post("/api/onboarding/complete", json=_complete_payload())

    binding_after = _run(web_api.web_auth_repository.get_binding_by_id(binding_before.id))
    assert binding_after.onboarding_completed_at is not None


def test_home_serves_cabinet_directly_after_completing_onboarding(api) -> None:
    client, web_api, workspace_id = api
    login_as(client, web_api, workspace_id, OWNER_ID)

    complete = client.post("/api/onboarding/complete", json=_complete_payload())
    assert complete.status_code == 200

    response = client.get("/")

    assert response.status_code == 200
    assert "ORCHESTRAVEL" in response.text
    assert "onboarding" not in response.headers.get("location", "")


def test_reopening_onboarding_after_completion_still_works(api) -> None:
    """Task: a completed user opening /onboarding manually should see the
    form again (prefilled client-side from /api/profile) and be allowed to
    save again - not be blocked or redirected away."""
    client, web_api, workspace_id = api
    login_as(client, web_api, workspace_id, OWNER_ID)
    client.post("/api/onboarding/complete", json=_complete_payload())

    page = client.get("/onboarding")
    assert page.status_code == 200

    second_save = client.post(
        "/api/onboarding/complete",
        json=_complete_payload(business_name="Мария Онбординг 2"),
    )
    assert second_save.status_code == 200
    assert second_save.json()["business_profile"]["business_name"] == "Мария Онбординг 2"


# ── workspace isolation ──────────────────────────────────────────────────

def test_complete_onboarding_only_writes_to_the_caller_own_workspace(api) -> None:
    client, web_api, workspace_id = api
    login_as(client, web_api, workspace_id, OWNER_ID)

    other = _run(web_api.partner_repository.provision_partner(
        222335111, "Other Agency", "other-agency-onboarding-test",
        business_name="Other Agency Original Name", business_type="independent_agent",
        short_description="Не должно измениться.", context={},
    ))

    response = client.post("/api/onboarding/complete", json=_complete_payload(
        business_name="Захват чужого workspace",
    ))
    assert response.status_code == 200
    assert response.json()["business_profile"]["business_name"] == "Захват чужого workspace"

    own_profile = _run(web_api.partner_repository.get_business_profile(workspace_id))
    assert own_profile.business_name == "Захват чужого workspace"

    other_profile = _run(web_api.partner_repository.get_business_profile(other.workspace.id))
    assert other_profile.business_name == "Other Agency Original Name"


# ── the completed data actually reaches the Assistant's prompt context ──

def _fake_generate(captured: dict):
    def _generate(**kwargs):
        captured.update(kwargs)
        return ChatResult(text="Ответ ассистента", usage=None)
    return _generate


def test_onboarding_data_reaches_the_assistant_profile_context(api) -> None:
    client, web_api, workspace_id = api
    login_as(client, web_api, workspace_id, OWNER_ID)

    client.post("/api/onboarding/complete", json=_complete_payload(
        business_name="Компания Онбординг",
        specializations=["Горнолыжные туры"],
    ))

    captured: dict = {}
    web_api.chat_provider.generate = _fake_generate(captured)

    conversation_id = client.post("/api/conversations").json()["conversation"]["id"]
    response = client.post("/api/chat", json={
        "message": "Что порекомендовать клиенту?", "conversation_id": conversation_id,
    })
    assert response.status_code == 200

    assert "Компания Онбординг" in captured["knowledge_context"]
    assert "Горнолыжные туры" in captured["knowledge_context"]
    assert captured["personal_style"] == "Дружелюбно. Обращайся на «ты»."


# ── member: role-aware onboarding (no BusinessProfile write access) ──────
#
# A 'member' binding has no write access to BusinessProfile - same rule as
# PUT /api/profile/business (see BusinessProfileService._require_write).
# onboarding.html's MEMBER_STEPS never collects/sends business fields for
# this role; these tests hit /api/onboarding/complete directly (as a
# member session) to prove the SERVER itself never saves them either and
# never claims it did, regardless of what a client sends.

MEMBER_ID = OWNER_ID + 1


def _login_as_member(client, web_api, workspace_id):
    return login_as(
        client, web_api, workspace_id, MEMBER_ID,
        email="member@example.com", role="member",
    )


def test_member_new_binding_is_redirected_to_onboarding(api) -> None:
    client, web_api, workspace_id = api
    _login_as_member(client, web_api, workspace_id)

    response = client.get("/")

    assert response.status_code == 303
    assert response.headers["location"] == "/onboarding"


def test_member_cannot_change_business_profile_via_onboarding(api) -> None:
    client, web_api, workspace_id = api
    original = _run(web_api.partner_repository.get_business_profile(workspace_id))
    _login_as_member(client, web_api, workspace_id)

    # Even a hand-crafted request with business fields attached (a stale
    # or tampered client) must not move the needle - the role check in
    # complete_onboarding() is server-side, not just onboarding.html
    # choosing not to send these fields.
    response = client.post("/api/onboarding/complete", json={
        "who": "agency", "business_name": "Захват через member",
        "short_description": "x", "specializations": ["x"],
        "tone": "expert", "address_form": "vy",
    })

    assert response.status_code == 200
    body = response.json()
    assert "error" not in body
    assert body["business_profile_saved"] is False
    assert body["business_profile"]["business_name"] == original.business_name
    assert body["business_profile"]["business_name"] != "Захват через member"

    unchanged = _run(web_api.partner_repository.get_business_profile(workspace_id))
    assert unchanged.business_name == original.business_name
    assert unchanged.revision == original.revision


def test_member_personal_style_is_saved(api) -> None:
    client, web_api, workspace_id = api
    _login_as_member(client, web_api, workspace_id)

    response = client.post("/api/onboarding/complete", json={
        "tone": "expert", "address_form": "vy",
    })

    assert response.status_code == 200
    expected_style = "Экспертно. Обращайся на «вы»."
    assert response.json()["personal_style"]["style_description"] == expected_style

    preferences = _run(web_api.partner_repository.get_user_preferences(
        workspace_id, MEMBER_ID,
    ))
    assert preferences.style_description == expected_style


def test_member_onboarding_completes_and_reaches_the_cabinet(api) -> None:
    client, web_api, workspace_id = api
    _login_as_member(client, web_api, workspace_id)

    me = client.get("/api/auth/me").json()
    assert me["role"] == "member"
    binding_before = _run(web_api.web_auth_repository.get_default_binding(
        _run(web_api.web_auth_repository.get_user_by_email(me["email"])).id
    ))
    assert binding_before.onboarding_completed_at is None

    complete = client.post("/api/onboarding/complete", json={
        "tone": "expert", "address_form": "vy",
    })
    assert complete.status_code == 200
    assert complete.json()["onboarding_completed"] is True

    binding_after = _run(web_api.web_auth_repository.get_binding_by_id(binding_before.id))
    assert binding_after.onboarding_completed_at is not None

    response = client.get("/")
    assert response.status_code == 200
    assert "ORCHESTRAVEL" in response.text


def test_member_response_never_implies_business_fields_were_saved(api) -> None:
    """UI/API surface check for the fixed UX defect: even when a member's
    request carries business fields, the response must not create the
    impression they were saved - business_profile_saved must be False and
    the returned business_profile must reflect the real (unchanged) one,
    not an echo of what was submitted."""
    client, web_api, workspace_id = api
    _login_as_member(client, web_api, workspace_id)

    response = client.post("/api/onboarding/complete", json={
        "who": "other", "business_name": "Псевдо-сохранение",
        "tone": "friendly", "address_form": "ty",
    })

    body = response.json()
    assert body["business_profile_saved"] is False
    assert body["business_profile"] is not None
    assert body["business_profile"]["business_name"] != "Псевдо-сохранение"
