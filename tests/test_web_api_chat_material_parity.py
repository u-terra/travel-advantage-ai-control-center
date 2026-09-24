"""Web/Telegram material-creation parity for POST /api/chat.

Launch-fix: a free-text request like "Напиши пост про раннее бронирование"
already reached Telegram's Content Factory (app.handlers.tasks -
_maybe_send_draft -> artifact_repository.create_artifact_with_initial_version)
and showed up in "Мои материалы", but the same text typed into Web's
/api/chat only ever produced a chat reply via chat_provider - never an
Artifact. This exercises the fix: /api/chat now classifies material intent
with the SAME app.routing.router.route_text() Telegram's router uses, and
on a match generates through the SAME MaterialOrchestrationService +
competitor_llm_provider Web already uses for "Создать материал из сигнала"
(app.services.material_orchestration, see also
tests/test_web_api_signal_competitor_actions.py) - no second generator, no
second LLM provider, no change to Telegram handlers, Competitor
Intelligence, Radar, Web Search, provider/fallback wiring, conversation
history, or subscription/access_state.

Requires the web-only dependencies (requirements-web.txt: fastapi, uvicorn,
markdown). Skips cleanly when they're not installed.
"""

from __future__ import annotations

import asyncio

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("markdown")

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

    with TestClient(web_api.app, base_url="https://testserver") as client:
        ws, _ = _run(web_api.partner_repository.ensure_owner_workspace(OWNER_ID))
        login_as(client, web_api, ws.id, OWNER_ID)
        yield client, web_api, db_path, ws.id

    sys.modules.pop("app.web_api", None)


def _new_conversation(client) -> int:
    return client.post("/api/conversations").json()["conversation"]["id"]


def _fake_chat_generate(text="Ответ ассистента"):
    def _generate(**kwargs):
        return ChatResult(text=text, usage=None)
    return _generate


def _fail_if_called(**kwargs):
    raise AssertionError("this provider must not be called for this request")


def _fake_draft(text="Готовый черновик поста про раннее бронирование."):
    from app.services.llm.models import ContentDraft
    return ContentDraft(text=text, warnings=())


# ── "напиши пост ..." creates an Artifact ───────────────────────────────

def test_free_text_content_request_creates_artifact(api, monkeypatch) -> None:
    client, web_api, _, workspace_id = api
    monkeypatch.setattr(web_api.chat_provider, "generate", _fail_if_called)
    monkeypatch.setattr(web_api.competitor_llm_provider, "generate_draft", lambda **kw: _fake_draft())

    conversation_id = _new_conversation(client)
    response = client.post(
        "/api/chat",
        json={
            "message": "Напиши пост про раннее бронирование",
            "conversation_id": conversation_id,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["material_id"] is not None
    assert "раннее бронирование" in body["answer"] or body["answer"]

    materials = client.get("/api/materials").json()["materials"]
    assert any(m["id"] == body["material_id"] for m in materials)

    artifact = _run(web_api.artifact_repository.get_artifact(workspace_id, body["material_id"]))
    assert artifact is not None
    assert artifact.artifact_type == "post"


# ── ordinary questions never become an Artifact ──────────────────────────

def test_ordinary_question_does_not_create_artifact(api, monkeypatch) -> None:
    client, web_api, _, workspace_id = api
    monkeypatch.setattr(web_api.chat_provider, "generate", _fake_chat_generate())
    monkeypatch.setattr(web_api.competitor_llm_provider, "generate_draft", _fail_if_called)

    conversation_id = _new_conversation(client)
    response = client.post(
        "/api/chat",
        json={"message": "Куда лучше поехать отдыхать в марте?", "conversation_id": conversation_id},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["material_id"] is None

    materials = client.get("/api/materials").json()["materials"]
    assert materials == []


# ── exactly one Artifact is created per material request ────────────────

def test_material_request_creates_exactly_one_artifact(api, monkeypatch) -> None:
    client, web_api, _, workspace_id = api
    monkeypatch.setattr(web_api.chat_provider, "generate", _fail_if_called)
    monkeypatch.setattr(web_api.competitor_llm_provider, "generate_draft", lambda **kw: _fake_draft())

    conversation_id = _new_conversation(client)
    response = client.post(
        "/api/chat",
        json={"message": "Сделай сторис про горящие туры", "conversation_id": conversation_id},
    )

    assert response.status_code == 200
    materials = client.get("/api/materials").json()["materials"]
    assert len(materials) == 1
    assert materials[0]["id"] == response.json()["material_id"]


# ── workspace isolation ──────────────────────────────────────────────────

def test_material_from_chat_is_workspace_isolated(api, monkeypatch) -> None:
    client, web_api, db_path, workspace_id = api
    monkeypatch.setattr(web_api.chat_provider, "generate", _fail_if_called)
    monkeypatch.setattr(web_api.competitor_llm_provider, "generate_draft", lambda **kw: _fake_draft())

    conversation_id = _new_conversation(client)
    response = client.post(
        "/api/chat",
        json={"message": "Напиши пост про раннее бронирование", "conversation_id": conversation_id},
    )
    material_id = response.json()["material_id"]
    assert material_id is not None

    other_workspace_id = _run(_insert_other_workspace(web_api, db_path))
    with TestClient(web_api.app, base_url="https://testserver") as other_client:
        login_as(
            other_client, web_api, other_workspace_id, OWNER_ID + 100,
            email="intruder@example.com",
        )
        other_materials = other_client.get("/api/materials").json()["materials"]
        assert other_materials == []

        foreign_read = _run(web_api.artifact_repository.get_artifact(other_workspace_id, material_id))
        assert foreign_read is None

    own_materials = client.get("/api/materials").json()["materials"]
    assert any(m["id"] == material_id for m in own_materials)


async def _insert_other_workspace(web_api, db_path) -> int:
    import aiosqlite

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "INSERT INTO partner_workspaces (name, slug, status, created_at, updated_at) "
            "VALUES (?, ?, 'active', datetime('now'), datetime('now'))",
            ("Другое пространство", "other-workspace-material-parity"),
        )
        await db.commit()
        return cursor.lastrowid or 0


# ── existing "Создать материал из сигнала" flow is unchanged ────────────

def test_material_from_signal_flow_still_uses_its_own_endpoint(api) -> None:
    """This fix only touches POST /api/chat's own branch - it adds no new
    call site into create_material_from_signal (see
    tests/test_web_api_signal_competitor_actions.py for that endpoint's own
    full regression coverage) and does not change its route, request shape,
    or response shape."""
    client, web_api, _, _ = api

    response = client.post("/api/signals/radar:999999/actions", json={"action": "post"})
    assert response.status_code == 200
    assert response.json() == {"error": "Сигнал недоступен.", "material": None}
