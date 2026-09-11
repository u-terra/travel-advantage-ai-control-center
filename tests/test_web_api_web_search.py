"""POST /api/chat + web search (ORCHESTRAVEL web-search MVP): verifies the
YandexSearchProvider/WebSearchService plumbing through the real endpoint,
without ever calling the real Yandex API - app.web_api.web_search_service is
replaced with a WebSearchService wrapping a hand-written fake provider.

OpenAI itself is also never called for real: app.web_api.chat_provider.generate
is monkeypatched, same convention as tests/test_web_api_conversations.py.

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
from app.services.web_search.base import SearchResponse, SearchResult, WebSearchProvider  # noqa: E402
from app.services.web_search.service import WebSearchService  # noqa: E402

from tests._web_auth_test_helpers import login_as  # noqa: E402

OWNER_ID = 586249067


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def api(tmp_path, monkeypatch):
    db_path = tmp_path / "journal.sqlite3"
    monkeypatch.setenv("JOURNAL_DB_PATH", str(db_path))
    monkeypatch.setenv("PLANNER_OPENAI_API_KEY", "test-key")
    # Never read for real here (web_search_service is replaced below in each
    # test), but keeps load_settings() honest about what "disabled" means.
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "false")

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


class _FakeSearchProvider(WebSearchProvider):
    name = "fake_yandex"

    def __init__(self, response: SearchResponse | None):
        self._response = response
        self.calls: list[tuple[str, str | None, int]] = []

    def search(self, query, *, site=None, limit=5):
        self.calls.append((query, site, limit))
        return self._response


def _search_response(query: str) -> SearchResponse:
    return SearchResponse(
        query=query,
        results=[
            SearchResult(
                title="Правила въезда в Индонезию",
                url="https://example.org/indonesia-entry",
                snippet="С 2026 года действует безвизовый режим для поездок до 30 дней.",
                domain="example.org",
                published_at=None,
                provider="fake_yandex",
                rank=1,
            ),
        ],
        provider="fake_yandex",
        elapsed_ms=42,
    )


def _new_conversation(client) -> int:
    return client.post("/api/conversations").json()["conversation"]["id"]


# ── search disabled -> old behavior fully preserved ─────────────────────────

def test_search_disabled_leaves_chat_behavior_unchanged(api) -> None:
    client, web_api, _, _ = api
    captured: dict = {}
    monkeypatch_target = web_api.chat_provider
    monkeypatch_target.generate = _fake_generate(captured=captured)
    # disabled=False (default fixture provider is a WebSearchService(None, enabled=False))
    assert web_api.web_search_service.maybe_search("Что нового у Travel Advantage?") is None

    conversation_id = _new_conversation(client)
    response = client.post("/api/chat", json={
        "message": "Что нового у Travel Advantage?", "conversation_id": conversation_id,
    })

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "Ответ ассистента"
    assert body["search_sources"] == []
    assert "АКТУАЛЬНЫЙ ПОИСК" not in (captured.get("knowledge_context") or "")


# ── search enabled + results -> context reaches chat_provider.generate ─────

def test_search_enabled_with_results_adds_context_and_sources(api, monkeypatch) -> None:
    client, web_api, _, _ = api
    captured: dict = {}
    monkeypatch.setattr(web_api.chat_provider, "generate", _fake_generate(captured=captured))

    fake_provider = _FakeSearchProvider(_search_response("правила въезда"))
    monkeypatch.setattr(web_api, "web_search_service", WebSearchService(fake_provider, enabled=True))

    conversation_id = _new_conversation(client)
    response = client.post("/api/chat", json={
        "message": "Какие сейчас изменения правил въезда в Индонезию для россиян?",
        "conversation_id": conversation_id,
    })

    assert response.status_code == 200
    body = response.json()

    knowledge_context = captured.get("knowledge_context") or ""
    assert "=== АКТУАЛЬНЫЙ ПОИСК В ИНТЕРНЕТЕ ===" in knowledge_context
    assert "https://example.org/indonesia-entry" in knowledge_context

    assert body["search_sources"] == [{
        "title": "Правила въезда в Индонезию",
        "url": "https://example.org/indonesia-entry",
        "domain": "example.org",
        "provider": "fake_yandex",
    }]
    assert len(fake_provider.calls) == 1


def test_search_not_triggered_for_ordinary_content_request(api, monkeypatch) -> None:
    """decide_web_search() still gates the call even when the provider/
    service is fully enabled and configured."""
    client, web_api, _, _ = api
    monkeypatch.setattr(web_api.chat_provider, "generate", _fake_generate())

    fake_provider = _FakeSearchProvider(_search_response("q"))
    monkeypatch.setattr(web_api, "web_search_service", WebSearchService(fake_provider, enabled=True))

    conversation_id = _new_conversation(client)
    response = client.post("/api/chat", json={
        "message": "Напиши пост про Индонезию", "conversation_id": conversation_id,
    })

    assert response.status_code == 200
    assert response.json()["search_sources"] == []
    assert fake_provider.calls == []


# ── Yandex error -> OpenAI answer still returned (fail-soft) ───────────────

def test_search_failure_does_not_break_the_chat_response(api, monkeypatch) -> None:
    client, web_api, _, _ = api
    monkeypatch.setattr(web_api.chat_provider, "generate", _fake_generate("Ответ несмотря на сбой поиска"))

    fake_provider = _FakeSearchProvider(None)  # simulates any Yandex failure -> None
    monkeypatch.setattr(web_api, "web_search_service", WebSearchService(fake_provider, enabled=True))

    conversation_id = _new_conversation(client)
    response = client.post("/api/chat", json={
        "message": "Какие сейчас изменения правил въезда в Индонезию для россиян?",
        "conversation_id": conversation_id,
    })

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "Ответ несмотря на сбой поиска"
    assert body["search_sources"] == []
    assert "error" not in body


# ── existing knowledge retrieval order is not disturbed ────────────────────

def test_web_search_context_appends_after_existing_knowledge_context(api, monkeypatch) -> None:
    client, web_api, _, _ = api
    captured: dict = {}
    monkeypatch.setattr(web_api.chat_provider, "generate", _fake_generate(captured=captured))
    # Stand-in for whatever the existing knowledge/competitor pipeline
    # already produced this turn - proves web search APPENDS rather than
    # replacing or reordering what was already there.
    monkeypatch.setattr(web_api, "_knowledge_context", lambda bundle: "BASE KNOWLEDGE CONTEXT")

    fake_provider = _FakeSearchProvider(_search_response("q"))
    monkeypatch.setattr(web_api, "web_search_service", WebSearchService(fake_provider, enabled=True))

    conversation_id = _new_conversation(client)
    client.post("/api/chat", json={
        "message": "Какие сейчас изменения правил въезда в Индонезию для россиян?",
        "conversation_id": conversation_id,
    })

    knowledge_context = captured.get("knowledge_context") or ""
    base_index = knowledge_context.index("BASE KNOWLEDGE CONTEXT")
    search_index = knowledge_context.index("АКТУАЛЬНЫЙ ПОИСК")
    assert base_index < search_index
