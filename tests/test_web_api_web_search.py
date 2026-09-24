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
from app.domain.knowledge import KnowledgeItem  # noqa: E402
from app.services.knowledge_service import KnowledgeBundle, SourceReference  # noqa: E402
from app.services.web_search.base import SearchResponse, SearchResult, WebSearchProvider  # noqa: E402
from app.services.web_search.service import (  # noqa: E402
    OFFICIAL_SOURCE_MISSING_USER_NOTICE,
    WebSearchService,
)

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

    def search(self, query, *, site=None, limit=5, search_type=None, allow_exceeding_configured_max=False):
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
    # _search_response()'s only domain ("example.org") is not official, and
    # this message matches the changeable-rules gate ("въезд") - so stage
    # two (official-source fallback) also fires exactly one extra search
    # call here. _FakeSearchProvider returns the same fixed response
    # regardless of query text, so the fallback finds no official domain
    # either and the merged results/sources are unchanged - only the call
    # count reflects the new fallback attempt.
    assert len(fake_provider.calls) == 2
    # No official domain anywhere (first search or fallback) -> the
    # deterministic, non-LLM caveat must be appended to the persisted
    # answer text itself, not just implied by search_sources.
    assert OFFICIAL_SOURCE_MISSING_USER_NOTICE in body["answer"]


def test_fallback_official_source_promoted_and_no_missing_notice(api, monkeypatch) -> None:
    """When the official-source fallback DOES find a government domain
    (even ranked below position 5, made possible by the wider limit=15
    fallback search), it must be promoted first AND the deterministic
    no-official-source notice must NOT appear - it would be misleading."""
    client, web_api, _, _ = api
    monkeypatch.setattr(web_api.chat_provider, "generate", _fake_generate())

    official_result = SearchResult(
        title="Imigrasi RI", url="https://imigrasi.go.id/entry-rules", snippet="...",
        domain="imigrasi.go.id", provider="fake_yandex", published_at=None, rank=8,
    )
    fallback_response = SearchResponse(
        query="fallback", results=[official_result], provider="fake_yandex", elapsed_ms=5,
    )
    fake_provider = _FakeSearchProvider(_search_response("правила въезда"))
    # First call returns the fixture below (no official); make the SECOND
    # (fallback) call return the official-bearing response instead.
    original_search = fake_provider.search
    call_count = {"n": 0}

    def sequenced_search(query, *, site=None, limit=5, search_type=None,
                          allow_exceeding_configured_max=False):
        call_count["n"] += 1
        result = original_search(
            query, site=site, limit=limit, search_type=search_type,
            allow_exceeding_configured_max=allow_exceeding_configured_max,
        )
        return fallback_response if call_count["n"] == 2 else result

    monkeypatch.setattr(fake_provider, "search", sequenced_search)
    monkeypatch.setattr(web_api, "web_search_service", WebSearchService(fake_provider, enabled=True))

    conversation_id = _new_conversation(client)
    response = client.post("/api/chat", json={
        "message": "Какие сейчас изменения правил въезда в Индонезию для россиян?",
        "conversation_id": conversation_id,
    })

    assert response.status_code == 200
    body = response.json()
    assert body["search_sources"][0]["domain"] == "imigrasi.go.id"
    assert OFFICIAL_SOURCE_MISSING_USER_NOTICE not in body["answer"]


def test_search_not_triggered_for_ordinary_content_request(api, monkeypatch) -> None:
    """decide_web_search() still gates the call even when the provider/
    service is fully enabled and configured.

    "Напиши пост про Индонезию" is itself a Content Factory free-text
    request (see test_web_api_chat_material_parity.py) - generate_draft is
    stubbed the same way that file stubs it, chat_provider.generate is left
    stubbed too in case routing ever changes, but only one of the two is
    actually expected to be called here."""
    client, web_api, _, _ = api
    monkeypatch.setattr(web_api.chat_provider, "generate", _fake_generate())
    from app.services.llm.models import ContentDraft
    monkeypatch.setattr(
        web_api.competitor_llm_provider, "generate_draft",
        lambda **kw: ContentDraft(text="Пост про Индонезию.", warnings=()),
    )

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


# ── _knowledge_context() must also tell the model not to duplicate the ─────
# ── UI's own "Источники" block (H) ──────────────────────────────────────────


def _bundle_with_one_source() -> KnowledgeBundle:
    # _knowledge_context() only emits anything when at least one of
    # primary_items/facts/compliance_facts is present (see its early-return
    # guard) - bundle.sources alone is, in this codebase, always populated
    # alongside real content by KnowledgeService.retrieve(), so a primary
    # item is included here to match that real shape.
    return KnowledgeBundle(
        question="q",
        primary_items=(
            KnowledgeItem(
                id=1, stable_key="k1", category="general", title="MWR Life Compensation Plan",
                content="Комиссионный план...", source_id=1, source_ref="doc-1", status="active",
                sort_order=0, tags=(), created_at="2026-01-01", updated_at="2026-01-01",
            ),
        ),
        related_items=(),
        facts=(),
        compliance_facts=(),
        examples=(),
        sources=(
            SourceReference(
                source_id=1, stable_key="k1", title="MWR Life Compensation Plan",
                source_reference="https://example.org/plan.pdf", verification_status="verified",
            ),
        ),
        potentially_ambiguous=False,
        ambiguity_reasons=(),
        missing_definitions=(),
    )


def _empty_bundle() -> KnowledgeBundle:
    return KnowledgeBundle(
        question="q", primary_items=(), related_items=(), facts=(), compliance_facts=(),
        examples=(), sources=(), potentially_ambiguous=False, ambiguity_reasons=(),
        missing_definitions=(),
    )


def test_knowledge_context_instructs_model_not_to_add_final_sources_section(api) -> None:
    _, web_api, _, _ = api
    text = web_api._knowledge_context(_bundle_with_one_source())
    lowered = text.lower()
    assert "источники" in lowered
    assert "не добавляй" in lowered
    assert "отдельным блоком интерфейса" in lowered


def test_knowledge_context_without_sources_has_no_sources_instruction(api) -> None:
    _, web_api, _, _ = api
    text = web_api._knowledge_context(_empty_bundle())
    assert text == ""
