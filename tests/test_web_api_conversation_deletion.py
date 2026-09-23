"""ORCHESTRAVEL user history management: DELETE /api/conversations/{id},
POST /api/conversations/bulk-delete, POST /api/conversations/clear.

Same conventions as tests/test_web_api_materials.py's delete coverage:
workspace(+user)-isolated via WebConversationRepository's existing
(workspace_id, telegram_user_id, id) WHERE clauses, explicit confirm=true
required for bulk/clear, and - the requirement specific to conversations -
a deleted conversation's messages must never reach the Assistant's
history/context for a future /api/chat call.

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


def _fake_generate(text="Ответ ассистента", captured=None):
    def _generate(**kwargs):
        if captured is not None:
            captured.update(kwargs)
        return ChatResult(text=text, usage=None)
    return _generate


# ── DELETE /api/conversations/{id}: single delete ───────────────────────

def test_delete_one_conversation_removes_it_and_its_messages(api) -> None:
    client, web_api, _, workspace_id = api
    conversation = _run(web_api.web_conversation_repository.create_conversation(
        workspace_id, OWNER_ID,
    ))
    _run(web_api.web_conversation_repository.add_message(
        workspace_id, OWNER_ID, conversation.id, "user", "Привет",
    ))

    response = client.delete(f"/api/conversations/{conversation.id}")

    assert response.status_code == 200
    assert response.json() == {"deleted": True}
    assert _run(web_api.web_conversation_repository.get_conversation(
        workspace_id, OWNER_ID, conversation.id,
    )) is None
    assert _run(web_api.web_conversation_repository.list_messages(
        workspace_id, OWNER_ID, conversation.id,
    )) == []


def test_delete_unknown_conversation_has_no_500(api) -> None:
    client, _, _, workspace_id = api

    response = client.delete("/api/conversations/999999")

    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] is False
    assert "error" in body


def test_delete_conversation_is_isolated_by_workspace(api) -> None:
    client, web_api, _, workspace_id = api
    other = _run(web_api.partner_repository.provision_partner(
        222335111, "Other Agency C1", "other-agency-conv-1",
        business_name="Other Agency C1", business_type="independent_agent",
        short_description="Другое рабочее пространство.", context={},
    ))
    foreign_conversation = _run(web_api.web_conversation_repository.create_conversation(
        other.workspace.id, 900000001,
    ))

    response = client.delete(f"/api/conversations/{foreign_conversation.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] is False
    assert "error" in body
    assert _run(web_api.web_conversation_repository.get_conversation(
        other.workspace.id, 900000001, foreign_conversation.id,
    )) is not None


# ── POST /api/conversations/bulk-delete ─────────────────────────────────

def test_bulk_delete_conversations_requires_confirm(api) -> None:
    client, web_api, _, workspace_id = api
    c1 = _run(web_api.web_conversation_repository.create_conversation(workspace_id, OWNER_ID))

    response = client.post(
        "/api/conversations/bulk-delete", json={"ids": [c1.id], "confirm": False},
    )

    assert response.status_code == 200
    body = response.json()
    assert "error" in body
    assert body["deleted_count"] == 0
    assert _run(web_api.web_conversation_repository.get_conversation(
        workspace_id, OWNER_ID, c1.id,
    )) is not None


def test_bulk_delete_conversations_removes_selected_only(api) -> None:
    client, web_api, _, workspace_id = api
    c1 = _run(web_api.web_conversation_repository.create_conversation(workspace_id, OWNER_ID))
    c2 = _run(web_api.web_conversation_repository.create_conversation(workspace_id, OWNER_ID))
    c3 = _run(web_api.web_conversation_repository.create_conversation(workspace_id, OWNER_ID))

    response = client.post(
        "/api/conversations/bulk-delete", json={"ids": [c1.id, c2.id], "confirm": True},
    )

    assert response.status_code == 200
    assert response.json() == {"deleted_count": 2}
    assert _run(web_api.web_conversation_repository.get_conversation(
        workspace_id, OWNER_ID, c1.id,
    )) is None
    assert _run(web_api.web_conversation_repository.get_conversation(
        workspace_id, OWNER_ID, c2.id,
    )) is None
    assert _run(web_api.web_conversation_repository.get_conversation(
        workspace_id, OWNER_ID, c3.id,
    )) is not None


def test_bulk_delete_conversations_cannot_touch_another_workspace(api) -> None:
    client, web_api, _, workspace_id = api
    other = _run(web_api.partner_repository.provision_partner(
        222335222, "Other Agency C2", "other-agency-conv-2",
        business_name="Other Agency C2", business_type="independent_agent",
        short_description="Другое рабочее пространство.", context={},
    ))
    foreign_conversation = _run(web_api.web_conversation_repository.create_conversation(
        other.workspace.id, 900000002,
    ))

    response = client.post(
        "/api/conversations/bulk-delete", json={"ids": [foreign_conversation.id], "confirm": True},
    )

    assert response.status_code == 200
    assert response.json() == {"deleted_count": 0}
    assert _run(web_api.web_conversation_repository.get_conversation(
        other.workspace.id, 900000002, foreign_conversation.id,
    )) is not None


# ── POST /api/conversations/clear ───────────────────────────────────────

def test_clear_conversations_requires_confirm(api) -> None:
    client, web_api, _, workspace_id = api
    _run(web_api.web_conversation_repository.create_conversation(workspace_id, OWNER_ID))

    response = client.post("/api/conversations/clear", json={"confirm": False})

    assert response.status_code == 200
    body = response.json()
    assert "error" in body
    assert body["deleted_count"] == 0
    assert len(_run(web_api.web_conversation_repository.list_conversations(
        workspace_id, OWNER_ID,
    ))) == 1


def test_clear_conversations_removes_all_for_this_user_only(api) -> None:
    client, web_api, _, workspace_id = api
    _run(web_api.web_conversation_repository.create_conversation(workspace_id, OWNER_ID))
    _run(web_api.web_conversation_repository.create_conversation(workspace_id, OWNER_ID))
    other = _run(web_api.partner_repository.provision_partner(
        222335333, "Other Agency C3", "other-agency-conv-3",
        business_name="Other Agency C3", business_type="independent_agent",
        short_description="Другое рабочее пространство.", context={},
    ))
    foreign_conversation = _run(web_api.web_conversation_repository.create_conversation(
        other.workspace.id, 900000003,
    ))

    response = client.post("/api/conversations/clear", json={"confirm": True})

    assert response.status_code == 200
    assert response.json() == {"deleted_count": 2}
    assert _run(web_api.web_conversation_repository.list_conversations(
        workspace_id, OWNER_ID,
    )) == []
    assert _run(web_api.web_conversation_repository.get_conversation(
        other.workspace.id, 900000003, foreign_conversation.id,
    )) is not None


# ── deleted conversation must never reach Assistant context/retrieval ───

def test_deleted_conversation_history_is_empty_for_future_reads(api) -> None:
    client, web_api, _, workspace_id = api
    conversation = _run(web_api.web_conversation_repository.create_conversation(
        workspace_id, OWNER_ID,
    ))
    _run(web_api.web_conversation_repository.add_message(
        workspace_id, OWNER_ID, conversation.id, "user", "Секретный вопрос про Бали",
    ))
    _run(web_api.web_conversation_repository.add_message(
        workspace_id, OWNER_ID, conversation.id, "assistant", "Секретный ответ про Бали",
    ))

    delete_response = client.delete(f"/api/conversations/{conversation.id}")
    assert delete_response.status_code == 200 and delete_response.json()["deleted"] is True

    # list_messages() is the exact read /api/chat uses to build `history`
    # for chat_provider.generate() - once deleted, it must come back empty,
    # not raise and not silently keep serving the old turns.
    assert _run(web_api.web_conversation_repository.list_messages(
        workspace_id, OWNER_ID, conversation.id,
    )) == []


def test_chat_on_deleted_conversation_id_fails_closed_without_generating(api, monkeypatch) -> None:
    """A client that still holds a since-deleted conversation_id (e.g. a
    stale browser tab) must never get a real Assistant reply built from -
    or persisted into - a conversation that no longer exists."""
    client, web_api, _, workspace_id = api
    conversation = _run(web_api.web_conversation_repository.create_conversation(
        workspace_id, OWNER_ID,
    ))
    _run(web_api.web_conversation_repository.add_message(
        workspace_id, OWNER_ID, conversation.id, "user", "Секретный вопрос про Бали",
    ))
    _run(web_api.web_conversation_repository.delete_conversation(
        workspace_id, OWNER_ID, conversation.id,
    ))

    captured: dict = {}
    monkeypatch.setattr(web_api.chat_provider, "generate", _fake_generate(captured=captured))

    response = client.post("/api/chat", json={
        "conversation_id": conversation.id, "message": "Ещё один вопрос",
    })

    assert response.status_code == 200
    body = response.json()
    assert "error" in body
    # The LLM must never even be called for a deleted conversation - no
    # leftover context, no new message persisted anywhere.
    assert captured == {}
