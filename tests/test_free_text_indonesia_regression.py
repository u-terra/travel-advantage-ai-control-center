"""Regression test for a real prod bug, end-to-end through on_free_text.

"Какие сейчас изменения правил въезда в Индонезию для россиян?" used to fall
through app.routing.router.route_text() as is_uncertain (no keyword matched
a bare visa/entry-rule topic without an explicit "клиент спрашивает"-style
intent - see the "Live prod bug" comment in app/routing/keywords.py). Even
once routed correctly, the request must actually reach WebSearchService
(wired into app.handlers.tasks._maybe_send_draft in the "Wire Telegram
free-text flow to the existing Web Search MVP" commit): a fresh/changeable-
rules query is worthless without a real, current source behind the answer.

This file exercises the full path with the EXACT wording from the bug
report, not a paraphrase - test_tasks_web_search.py already covers the
general wiring with generic strings; this one guards the specific query
class (bare visa/entry-rule topic, no explicit client-intent phrase) end to
end: routing -> WebSearchService -> generation context -> rendered sources,
plus the two neighboring cases that must NOT regress the other way -
a content request that happens to mention a similar destination must not
trigger a search, and a genuinely topic-less follow-up must stay uncertain.
"""

from __future__ import annotations

import asyncio

from app.handlers.tasks import on_free_text
from app.routing.modules import Module
from app.routing.router import route_text
from app.services.llm.models import ContentDraft
from app.services.web_search.base import SearchResponse, SearchResult, WebSearchProvider
from app.services.web_search.service import WebSearchService
from tests.llm_fakes import FakeLLMProvider
from tests.test_journal_handlers import Message, business_profile, context, journal, profile_repository

_QUERY = "Какие сейчас изменения правил въезда в Индонезию для россиян?"
_UNRELATED_POST_REQUEST = "Напиши пост о путешествии на Бали"
_VAGUE_FOLLOWUP = "Что с этим делать?"

_SOURCE_URL = "https://example.com/indonesia-entry-rules"


def run(coro):
    return asyncio.run(coro)


class _FakeProvider(WebSearchProvider):
    name = "fake"

    def __init__(self, response: SearchResponse | None = None):
        self._response = response
        self.calls: list[str] = []

    def search(self, query, *, site=None, limit=5, search_type=None):
        self.calls.append(query)
        return self._response


def _sample_response() -> SearchResponse:
    return SearchResponse(
        query=_QUERY,
        results=[
            SearchResult(
                title="Правила въезда в Индонезию для россиян",
                url=_SOURCE_URL,
                snippet="С 2026 года визовые правила при въезде изменились.",
                domain="example.com", published_at=None, provider="fake", rank=1,
            ),
        ],
        provider="fake",
        elapsed_ms=5,
    )


def run_free_text(text: str, *, web_search_service: WebSearchService | None):
    message = Message(text)
    provider = FakeLLMProvider(draft=ContentDraft("Черновик", ()))
    profiles = profile_repository(business_profile())
    run(on_free_text(
        message, journal(), provider, context(), profiles,
        web_search_service=web_search_service,
    ))
    return message, provider


# 1. Routing alone: the exact query is no longer uncertain.
def test_exact_indonesia_query_route_is_not_uncertain():
    decision = route_text(_QUERY)
    assert decision.is_uncertain is False
    assert decision.primary_module is Module.TRAVEL_ASSISTANT


# 2. End-to-end: WebSearchService is actually called for the exact query.
def test_exact_indonesia_query_triggers_web_search_call():
    # _sample_response() has no official domain, and _QUERY matches the
    # changeable-rules gate ("въезд") - so this also triggers exactly one
    # official-source fallback search (stage two of official-source
    # priority; see test_web_search_service.py's dedicated fallback tests).
    # This test only cares that the ORIGINAL query was searched for real.
    fake_provider = _FakeProvider(_sample_response())
    service = WebSearchService(fake_provider, enabled=True)
    run_free_text(_QUERY, web_search_service=service)
    assert fake_provider.calls[0] == _QUERY
    assert len(fake_provider.calls) == 2


# 3. The search result's formatted context reaches LLM generation.
def test_exact_indonesia_query_context_reaches_generation():
    fake_provider = _FakeProvider(_sample_response())
    service = WebSearchService(fake_provider, enabled=True)
    _, provider = run_free_text(_QUERY, web_search_service=service)
    source_text = provider.generate_draft.call_args.kwargs["source_text"]
    assert "АКТУАЛЬНЫЙ ПОИСК В ИНТЕРНЕТЕ" in source_text
    assert _SOURCE_URL in source_text


# 4. The user-visible Telegram reply shows the sources.
def test_exact_indonesia_query_shows_sources_to_user():
    fake_provider = _FakeProvider(_sample_response())
    service = WebSearchService(fake_provider, enabled=True)
    message, _ = run_free_text(_QUERY, web_search_service=service)
    text = message.answers[-1][0]
    assert "Черновик" in text
    assert "Источники:" in text
    assert _SOURCE_URL in text


# ── Official-source priority + geo-scope guard regression ───────────────────
#
# The real prod bug: the answer generalized Bali's regional tourist fee
# (150,000 IDR) into a rule for all of Indonesia, and leaned on secondary
# sources (aggregators/media/insurers/agencies) instead of the government
# immigration site. These tests guard both fixes end to end, through the
# same on_free_text() path as the tests above - not just unit tests of
# app.services.web_search.service in isolation.

_SECONDARY_BALI_URL = "https://travelblog.example/bali-tourist-fee"
_OFFICIAL_URL = "https://imigrasi.go.id/entry-rules"


def _bali_and_official_response() -> SearchResponse:
    """Secondary/Bali-only source ranked first by the provider (as Yandex's
    own SEO-driven ranking can genuinely do) - official government source
    ranked last. This is the exact ordering that produced the prod bug."""
    return SearchResponse(
        query=_QUERY,
        results=[
            SearchResult(
                title="Туристический сбор на Бали для иностранцев",
                url=_SECONDARY_BALI_URL,
                snippet="На Бали введён туристический сбор 150 000 IDR.",
                domain="travelblog.example", published_at=None, provider="fake", rank=1,
            ),
            SearchResult(
                title="Immigration of Republic of Indonesia - Entry Rules",
                url=_OFFICIAL_URL,
                snippet="Официальные правила въезда в Индонезию.",
                domain="imigrasi.go.id", published_at=None, provider="fake", rank=2,
            ),
        ],
        provider="fake",
        elapsed_ms=5,
    )


# 5. Official source is promoted ahead of the Bali-only secondary source in
# the text actually sent to the LLM.
def test_indonesia_query_promotes_official_source_in_generation_context():
    fake_provider = _FakeProvider(_bali_and_official_response())
    service = WebSearchService(fake_provider, enabled=True)
    _, provider = run_free_text(_QUERY, web_search_service=service)
    source_text = provider.generate_draft.call_args.kwargs["source_text"]
    assert source_text.index(_OFFICIAL_URL) < source_text.index(_SECONDARY_BALI_URL)


# 6. The geo-scope guard text (Bali != Indonesia, currency, no-official-
# confirmation, official-wins-on-conflict) reaches the generation context.
def test_indonesia_query_context_includes_geo_scope_guard():
    fake_provider = _FakeProvider(_bali_and_official_response())
    service = WebSearchService(fake_provider, enabled=True)
    _, provider = run_free_text(_QUERY, web_search_service=service)
    source_text = provider.generate_draft.call_args.kwargs["source_text"]
    lowered = source_text.lower()
    assert "бали" in lowered and "индонез" in lowered
    assert "пункт въезда" in lowered
    assert "приоритет всегда у официального" in lowered


# 7. The user-visible Telegram sources list also shows the official source
# first - the same reordering the LLM context got, not a second mechanism.
def test_indonesia_query_shows_official_source_first_to_user():
    fake_provider = _FakeProvider(_bali_and_official_response())
    service = WebSearchService(fake_provider, enabled=True)
    message, _ = run_free_text(_QUERY, web_search_service=service)
    text = message.answers[-1][0]
    assert text.index(_OFFICIAL_URL) < text.index(_SECONDARY_BALI_URL)


# 5. A content-creation request naming a nearby destination must not search -
# no freshness/rules/market/explicit-intent marker means no search, even
# though it is routed confidently (Content Factory, not uncertain).
def test_bali_post_request_does_not_trigger_search():
    fake_provider = _FakeProvider(_sample_response())
    service = WebSearchService(fake_provider, enabled=True)
    decision = route_text(_UNRELATED_POST_REQUEST)
    assert decision.is_uncertain is False
    assert decision.primary_module is Module.CONTENT_FACTORY
    message, _ = run_free_text(_UNRELATED_POST_REQUEST, web_search_service=service)
    assert fake_provider.calls == []
    assert "Источники:" not in message.answers[-1][0]


# 6. A genuinely topic-less follow-up still gets no confident route.
def test_vague_followup_remains_uncertain():
    decision = route_text(_VAGUE_FOLLOWUP)
    assert decision.is_uncertain is True
    assert decision.primary_module is Module.ORCHESTRATOR
