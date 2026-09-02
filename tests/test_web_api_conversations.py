"""POST/GET /api/conversations, GET /api/conversations/{id}/messages, and the
reworked /api/chat - server-side persistent history for the web Ассистент.
Backed by WebConversationRepository (see
tests/test_web_conversation_repository.py for repository-level isolation/
ordering coverage); this file exercises the same guarantees through the HTTP
surface, plus the /api/chat orchestration itself (history pulled from the
server, title derivation, and the no-fake-assistant-message-on-failure rule).

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


def _fail_generate(**kwargs):
    raise RuntimeError("upstream LLM error")


# ── POST /api/conversations ──────────────────────────────────────────────

def test_create_conversation_returns_default_title(api) -> None:
    client, _, _, workspace_id = api

    response = client.post("/api/conversations")

    assert response.status_code == 200
    conversation = response.json()["conversation"]
    assert conversation["title"] == "Новый диалог"
    assert conversation["id"] > 0
    assert conversation["created_at"] == conversation["updated_at"]


# ── GET /api/conversations ───────────────────────────────────────────────

def test_list_conversations_empty_by_default(api) -> None:
    client, _, _, workspace_id = api

    response = client.get("/api/conversations")

    assert response.status_code == 200
    assert response.json() == {"conversations": []}


def test_list_conversations_freshest_first(api, monkeypatch) -> None:
    client, web_api, _, workspace_id = api
    monkeypatch.setattr(web_api.chat_provider, "generate", _fake_generate())

    first = client.post("/api/conversations").json()["conversation"]
    second = client.post("/api/conversations").json()["conversation"]

    # touch `first` via a real message - it must jump back to the top.
    client.post("/api/chat", json={"message": "Привет", "conversation_id": first["id"]})

    listed = client.get("/api/conversations").json()["conversations"]
    assert [item["id"] for item in listed] == [first["id"], second["id"]]


# ── GET /api/conversations/{id}/messages ─────────────────────────────────

def test_messages_for_unknown_conversation_returns_error(api) -> None:
    client, _, _, workspace_id = api

    response = client.get("/api/conversations/999999/messages")

    assert response.status_code == 200
    body = response.json()
    assert body["conversation"] is None
    assert body["messages"] == []
    assert "error" in body


def test_messages_round_trip_after_chat(api, monkeypatch) -> None:
    client, web_api, _, workspace_id = api
    monkeypatch.setattr(
        web_api.chat_provider, "generate", _fake_generate(text="Держите ответ."),
    )

    conversation_id = client.post("/api/conversations").json()["conversation"]["id"]
    client.post(
        "/api/chat", json={"message": "Куда поехать в марте?", "conversation_id": conversation_id},
    )

    response = client.get(f"/api/conversations/{conversation_id}/messages")

    assert response.status_code == 200
    body = response.json()
    assert body["conversation"]["id"] == conversation_id
    roles = [item["role"] for item in body["messages"]]
    contents = [item["content"] for item in body["messages"]]
    assert roles == ["user", "assistant"]
    assert contents == ["Куда поехать в марте?", "Держите ответ."]


# ── /api/chat orchestration ──────────────────────────────────────────────

def test_chat_requires_conversation_id(api) -> None:
    client, _, _, workspace_id = api

    response = client.post("/api/chat", json={"message": "Привет"})

    assert response.status_code == 422


def test_chat_rejects_unknown_conversation_id(api) -> None:
    client, _, _, workspace_id = api

    response = client.post(
        "/api/chat", json={"message": "Привет", "conversation_id": 999999},
    )

    assert response.status_code == 200
    body = response.json()
    assert "error" in body
    assert "answer" not in body


def test_chat_saves_user_and_assistant_messages(api, monkeypatch) -> None:
    client, web_api, _, workspace_id = api
    monkeypatch.setattr(
        web_api.chat_provider, "generate", _fake_generate(text="Отвечаю."),
    )

    conversation_id = client.post("/api/conversations").json()["conversation"]["id"]
    response = client.post(
        "/api/chat", json={"message": "Привет!", "conversation_id": conversation_id},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "Отвечаю."
    assert body["conversation"]["id"] == conversation_id

    messages = _run(web_api.web_conversation_repository.list_messages(
        workspace_id, OWNER_ID, conversation_id,
    ))
    assert [(item.role, item.content) for item in messages] == [
        ("user", "Привет!"), ("assistant", "Отвечаю."),
    ]


def test_chat_derives_title_from_first_message(api, monkeypatch) -> None:
    client, web_api, _, workspace_id = api
    monkeypatch.setattr(web_api.chat_provider, "generate", _fake_generate())

    conversation_id = client.post("/api/conversations").json()["conversation"]["id"]
    response = client.post(
        "/api/chat",
        json={
            "message": "Подскажи направления для пляжного отдыха в ноябре",
            "conversation_id": conversation_id,
        },
    )

    assert response.status_code == 200
    assert response.json()["conversation"]["title"] == (
        "Подскажи направления для пляжного отдыха в ноябре"
    )

    # a second message must NOT re-derive the title.
    client.post(
        "/api/chat", json={"message": "А ещё что?", "conversation_id": conversation_id},
    )
    listed = client.get("/api/conversations").json()["conversations"]
    assert listed[0]["title"] == "Подскажи направления для пляжного отдыха в ноябре"


def test_chat_passes_prior_history_to_provider(api, monkeypatch) -> None:
    client, web_api, _, workspace_id = api
    captured = {}
    monkeypatch.setattr(
        web_api.chat_provider, "generate", _fake_generate(text="Первый ответ", captured=captured),
    )

    conversation_id = client.post("/api/conversations").json()["conversation"]["id"]
    client.post(
        "/api/chat", json={"message": "Первое сообщение", "conversation_id": conversation_id},
    )

    monkeypatch.setattr(
        web_api.chat_provider, "generate", _fake_generate(text="Второй ответ", captured=captured),
    )
    client.post(
        "/api/chat", json={"message": "Второе сообщение", "conversation_id": conversation_id},
    )

    assert captured["history"] == [
        {"role": "user", "content": "Первое сообщение"},
        {"role": "assistant", "content": "Первый ответ"},
    ]


def test_chat_generation_failure_keeps_user_message_without_fake_assistant_reply(
    api, monkeypatch,
) -> None:
    client, web_api, _, workspace_id = api
    monkeypatch.setattr(web_api.chat_provider, "generate", _fail_generate)

    conversation_id = client.post("/api/conversations").json()["conversation"]["id"]
    response = client.post(
        "/api/chat", json={"message": "Сломай генерацию", "conversation_id": conversation_id},
    )

    assert response.status_code == 200
    assert "error" in response.json()
    assert "answer" not in response.json()

    messages = _run(web_api.web_conversation_repository.list_messages(
        workspace_id, OWNER_ID, conversation_id,
    ))
    assert [(item.role, item.content) for item in messages] == [
        ("user", "Сломай генерацию"),
    ]

    from app.domain.usage import UsageStatus

    usage_events = _run(web_api.usage_ledger_repository.list_for_workspace(
        workspace_id, limit=10,
    ))
    assert any(event.status is UsageStatus.FAILURE for event in usage_events)


def test_messages_render_markdown_for_assistant_only(api, monkeypatch) -> None:
    client, web_api, _, workspace_id = api
    monkeypatch.setattr(
        web_api.chat_provider, "generate", _fake_generate(text="**жирный** текст"),
    )

    conversation_id = client.post("/api/conversations").json()["conversation"]["id"]
    client.post(
        "/api/chat", json={"message": "<script>alert(1)</script>", "conversation_id": conversation_id},
    )

    body = client.get(f"/api/conversations/{conversation_id}/messages").json()
    user_message, assistant_message = body["messages"]

    assert "content_html" not in user_message
    assert "<strong>жирный</strong>" in assistant_message["content_html"]
    # source-of-truth content stays plain text, never rendered HTML.
    assert assistant_message["content"] == "**жирный** текст"


def test_chat_response_never_leaks_context_fields(api, monkeypatch) -> None:
    client, web_api, _, workspace_id = api
    monkeypatch.setattr(web_api.chat_provider, "generate", _fake_generate())

    conversation_id = client.post("/api/conversations").json()["conversation"]["id"]
    response = client.post(
        "/api/chat", json={"message": "Привет", "conversation_id": conversation_id},
    )

    body = response.json()
    forbidden_keys = {
        "knowledge_context", "workspace_memory", "personal_style",
        "system_prompt", "history",
    }
    assert forbidden_keys.isdisjoint(body.keys())


# ── workspace / user isolation ───────────────────────────────────────────

def test_foreign_workspace_cannot_read_or_append_to_conversation(api, monkeypatch) -> None:
    """Identity now comes from a real second session (a different web
    account bound to a different workspace) instead of mutating a global -
    a stronger proof, since it goes through the exact same auth path a
    real attacker would."""
    client, web_api, db_path, workspace_id = api
    monkeypatch.setattr(web_api.chat_provider, "generate", _fake_generate())

    conversation_id = client.post("/api/conversations").json()["conversation"]["id"]

    other_workspace_id = _run(_insert_other_workspace(web_api, db_path))
    with TestClient(web_api.app, base_url="https://testserver") as other_client:
        login_as(other_client, web_api, other_workspace_id, OWNER_ID + 100, email="intruder@example.com")

        response = other_client.get(f"/api/conversations/{conversation_id}/messages")
        assert response.json()["conversation"] is None

        chat_response = other_client.post(
            "/api/chat", json={"message": "Чужой доступ", "conversation_id": conversation_id},
        )
        assert "error" in chat_response.json()

    messages = _run(web_api.web_conversation_repository.list_messages(
        workspace_id, OWNER_ID, conversation_id,
    ))
    assert messages == []


async def _insert_other_workspace(web_api, db_path) -> int:
    import aiosqlite

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "INSERT INTO partner_workspaces (name, slug, status, created_at, updated_at) "
            "VALUES (?, ?, 'active', datetime('now'), datetime('now'))",
            ("Другое пространство", "other-workspace"),
        )
        await db.commit()
        return cursor.lastrowid or 0


def test_foreign_user_cannot_read_or_append_to_conversation(api, monkeypatch) -> None:
    """Same workspace, different telegram_user_id (a teammate, not an
    outside attacker) - a real second session, same reasoning as the
    foreign-workspace test above."""
    client, web_api, _, workspace_id = api
    monkeypatch.setattr(web_api.chat_provider, "generate", _fake_generate())

    conversation_id = client.post("/api/conversations").json()["conversation"]["id"]

    with TestClient(web_api.app, base_url="https://testserver") as other_client:
        login_as(
            other_client, web_api, workspace_id, OWNER_ID + 1,
            email="teammate@example.com",
        )

        response = other_client.get(f"/api/conversations/{conversation_id}/messages")
        assert response.json()["conversation"] is None

        chat_response = other_client.post(
            "/api/chat", json={"message": "Чужой пользователь", "conversation_id": conversation_id},
        )
        assert "error" in chat_response.json()

    messages = _run(web_api.web_conversation_repository.list_messages(
        workspace_id, OWNER_ID, conversation_id,
    ))
    assert messages == []
