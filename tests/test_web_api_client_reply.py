"""POST /api/client-reply - Web/Telegram parity launch-fix for the
«Ответить клиенту» scenario.

Telegram's dedicated "💬 Ответить клиенту" button flow (app.handlers.tasks:
AwaitReplySubject -> AwaitTask -> _route_and_dispatch(forced_module=
Module.TRAVEL_ASSISTANT) -> _maybe_send_draft's is_client_reply branch)
generates through MaterialOrchestrationService.build_client_reply_generation_
spec + build_provider_generation_request + provider.generate_draft +
strip_assistant_tail, and does NOT unconditionally persist an Artifact/
WorkItem (see app.services.reply_sync.ReplyWorkSyncService - only the
button+new-subject case creates one). Web's POST /api/chat never had an
equivalent mode at all - a client's message typed there just went through
the generic Assistant chat.

This is the new, explicitly separate endpoint: same transport-independent
pieces Telegram already uses, no new prompt, no new LLM provider, no
Artifact/WorkItem side effects. POST /api/chat itself is untouched (see
tests/test_web_api_conversations.py and tests/test_web_api_chat_material_
parity.py for its own coverage).

Requires the web-only dependencies (requirements-web.txt: fastapi, uvicorn,
markdown). Skips cleanly when they're not installed.
"""

from __future__ import annotations

import asyncio

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("markdown")

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
        yield client, web_api, db_path, ws.id

    sys.modules.pop("app.web_api", None)


def _fake_draft(text: str = "Добрый день! Уточню детали и напишу точнее."):
    from app.services.llm.models import ContentDraft
    return ContentDraft(text=text, warnings=())


def _fail_if_called(**kwargs):
    raise AssertionError("this provider must not be called for this request")


# ── explicit Web client-reply request calls build_client_reply_generation_spec ──

def test_client_reply_uses_build_client_reply_generation_spec(api, monkeypatch) -> None:
    client, web_api, _, _ = api
    monkeypatch.setattr(web_api.chat_provider, "generate", _fail_if_called)

    captured = {}
    original_builder = web_api.material_orchestration_service.build_client_reply_generation_spec

    def spy_builder(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return original_builder(*args, **kwargs)

    monkeypatch.setattr(
        web_api.material_orchestration_service,
        "build_client_reply_generation_spec",
        spy_builder,
    )
    monkeypatch.setattr(web_api.competitor_llm_provider, "generate_draft", lambda **kw: _fake_draft())

    response = client.post(
        "/api/client-reply",
        json={"client_message": "Можно перенести тур на другие даты?", "client_name": "Иван"},
    )

    assert response.status_code == 200
    assert captured, "build_client_reply_generation_spec was never called"


# ── client message text flows into GenerationSpec as untrusted_source_content ──

def test_client_message_becomes_untrusted_source_content(api, monkeypatch) -> None:
    client, web_api, _, _ = api

    captured = {}

    def fake_generate_draft(**kwargs):
        captured.update(kwargs)
        return _fake_draft()

    monkeypatch.setattr(web_api.competitor_llm_provider, "generate_draft", fake_generate_draft)

    response = client.post(
        "/api/client-reply",
        json={"client_message": "А что с возвратом денег при отмене брони?"},
    )

    assert response.status_code == 200
    assert "А что с возвратом денег при отмене брони?" in captured["source_text"]


# ── result comes back as a client reply (not generic chat) ──────────────

def test_result_returned_as_client_reply(api, monkeypatch) -> None:
    client, web_api, _, _ = api
    monkeypatch.setattr(web_api.chat_provider, "generate", _fail_if_called)
    monkeypatch.setattr(
        web_api.competitor_llm_provider, "generate_draft",
        lambda **kw: _fake_draft("Добрый день! Да, перенос возможен, уточню даты."),
    )

    response = client.post(
        "/api/client-reply",
        json={"client_message": "Можно перенести тур?", "client_name": "Ольга"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "Добрый день! Да, перенос возможен, уточню даты."
    assert body["client_name"] == "Ольга"
    assert "error" not in body


def test_client_name_is_optional(api, monkeypatch) -> None:
    client, web_api, _, _ = api
    monkeypatch.setattr(web_api.competitor_llm_provider, "generate_draft", lambda **kw: _fake_draft())

    response = client.post(
        "/api/client-reply", json={"client_message": "Какие документы нужны для визы?"},
    )

    assert response.status_code == 200
    assert response.json()["client_name"] is None


def test_empty_client_message_is_rejected_without_calling_llm(api, monkeypatch) -> None:
    client, web_api, _, _ = api
    monkeypatch.setattr(web_api.competitor_llm_provider, "generate_draft", _fail_if_called)

    response = client.post("/api/client-reply", json={"client_message": "   "})

    assert response.status_code == 200
    assert "error" in response.json()


# ── POST /api/chat is not changed by this endpoint's existence ──────────

def test_regular_chat_endpoint_is_unaffected(api, monkeypatch) -> None:
    from app.chat_provider import ChatResult

    client, web_api, _, _ = api
    monkeypatch.setattr(
        web_api.chat_provider, "generate",
        lambda **kw: ChatResult(text="Обычный ответ ассистента", usage=None),
    )
    monkeypatch.setattr(web_api.competitor_llm_provider, "generate_draft", _fail_if_called)

    conversation_id = client.post("/api/conversations").json()["conversation"]["id"]
    response = client.post(
        "/api/chat",
        json={"message": "Куда лучше поехать в мае?", "conversation_id": conversation_id},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "Обычный ответ ассистента"
    assert "reply" not in body


# ── workspace isolation ──────────────────────────────────────────────────

def test_client_reply_is_workspace_isolated(api, monkeypatch) -> None:
    client, web_api, db_path, workspace_id = api
    monkeypatch.setattr(web_api.competitor_llm_provider, "generate_draft", lambda **kw: _fake_draft())

    captured_workspace_ids = []
    original_builder = web_api.material_orchestration_service.build_client_reply_generation_spec

    def spy_builder(workspace_id_arg, *args, **kwargs):
        captured_workspace_ids.append(workspace_id_arg)
        return original_builder(workspace_id_arg, *args, **kwargs)

    monkeypatch.setattr(
        web_api.material_orchestration_service,
        "build_client_reply_generation_spec",
        spy_builder,
    )

    other_workspace_id = _run(_insert_other_workspace(web_api, db_path))
    with TestClient(web_api.app, base_url="https://testserver") as other_client:
        login_as(
            other_client, web_api, other_workspace_id, OWNER_ID + 100,
            email="intruder@example.com",
        )
        response = other_client.post(
            "/api/client-reply", json={"client_message": "Есть скидки на группу?"},
        )
        assert response.status_code == 200

    client.post("/api/client-reply", json={"client_message": "А что по срокам?"})

    assert other_workspace_id in captured_workspace_ids
    assert workspace_id in captured_workspace_ids
    assert captured_workspace_ids[0] != captured_workspace_ids[1]


async def _insert_other_workspace(web_api, db_path) -> int:
    import aiosqlite

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "INSERT INTO partner_workspaces (name, slug, status, created_at, updated_at) "
            "VALUES (?, ?, 'active', datetime('now'), datetime('now'))",
            ("Другое пространство", "other-workspace-client-reply"),
        )
        await db.commit()
        return cursor.lastrowid or 0


# ── LLM errors surface correctly ─────────────────────────────────────────

def test_generate_draft_returning_none_is_a_clean_error(api, monkeypatch) -> None:
    client, web_api, _, _ = api
    monkeypatch.setattr(web_api.competitor_llm_provider, "generate_draft", lambda **kw: None)

    response = client.post(
        "/api/client-reply", json={"client_message": "Что с багажом на этом рейсе?"},
    )

    assert response.status_code == 200
    assert "error" in response.json()


def test_generate_draft_raising_is_a_clean_error(api, monkeypatch) -> None:
    def boom(**kwargs):
        raise RuntimeError("upstream LLM error")

    client, web_api, _, _ = api
    monkeypatch.setattr(web_api.competitor_llm_provider, "generate_draft", boom)

    response = client.post(
        "/api/client-reply", json={"client_message": "Работает ли страховка за рубежом?"},
    )

    assert response.status_code == 200
    assert "error" in response.json()


# ── no Artifact / WorkItem is created automatically ──────────────────────

def test_client_reply_does_not_create_artifact_or_work_item(api, monkeypatch) -> None:
    client, web_api, _, _ = api
    monkeypatch.setattr(web_api.competitor_llm_provider, "generate_draft", lambda **kw: _fake_draft())

    response = client.post(
        "/api/client-reply",
        json={"client_message": "Расскажите про условия отмены брони", "client_name": "Пётр"},
    )

    assert response.status_code == 200
    body = response.json()
    assert "material_id" not in body
    assert "work_item" not in body
    assert "artifact" not in body

    materials = client.get("/api/materials").json()["materials"]
    assert materials == []
