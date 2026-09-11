"""Telegram Web Search MVP: on_free_text wired to the same WebSearchService/
decide_web_search/format_search_context the Web path (app.web_api) already
uses - see app.services.web_search.service and app.handlers.tasks._maybe_send_draft.

No second search provider, no second decision policy, no LLM-authored
"Источники" section - Telegram renders a compact sources block itself
(_format_telegram_sources), same contract as chat.html's
collectAnswerSources/appendAnswerSources on the Web side.
"""

from __future__ import annotations

import asyncio

from app.handlers.tasks import on_free_text
from app.services.llm.models import ContentDraft
from app.services.web_search.base import SearchResponse, SearchResult, WebSearchProvider
from app.services.web_search.service import WebSearchService
from tests.llm_fakes import FakeLLMProvider
from tests.test_journal_handlers import Message, business_profile, context, journal, profile_repository


def run(coro):
    return asyncio.run(coro)


class _FakeProvider(WebSearchProvider):
    name = "fake"

    def __init__(self, response: SearchResponse | None = None, *, error: bool = False):
        self._response = response
        self.error = error
        self.calls: list[tuple[str, str | None, int]] = []

    def search(self, query, *, site=None, limit=5):
        self.calls.append((query, site, limit))
        if self.error:
            raise RuntimeError("boom")
        return self._response


def _sample_response(query: str, urls: tuple[str, ...] = ("https://example.com/a",)) -> SearchResponse:
    return SearchResponse(
        query=query,
        results=[
            SearchResult(
                title=f"Заголовок {i}", url=url, snippet="Свежие сведения.",
                domain="example.com", published_at=None, provider="fake", rank=i,
            )
            for i, url in enumerate(urls, start=1)
        ],
        provider="fake",
        elapsed_ms=5,
    )


def run_free_text(
    text: str, *, web_search_service: WebSearchService | None = None,
    draft: str | None = "Черновик",
):
    message = Message(text)
    provider = FakeLLMProvider(draft=None if draft is None else ContentDraft(draft, ()))
    profiles = profile_repository(business_profile())
    run(on_free_text(
        message, journal(), provider, context(), profiles,
        web_search_service=web_search_service,
    ))
    return message, provider


# A: an actual/fresh query -> search is called.
def test_actual_query_triggers_search_call():
    fake_provider = _FakeProvider(_sample_response("q"))
    service = WebSearchService(fake_provider, enabled=True)
    run_free_text("Нужен пост о свежих новостях туризма", web_search_service=service)
    assert len(fake_provider.calls) == 1


# B: "напиши пост..." (no freshness/rules/market/site markers) -> search is not called.
def test_regular_post_request_does_not_trigger_search():
    fake_provider = _FakeProvider(_sample_response("q"))
    service = WebSearchService(fake_provider, enabled=True)
    run_free_text("Нужен пост о путешествиях", web_search_service=service)
    assert fake_provider.calls == []


# C: search error -> the normal Telegram draft reply still goes out.
def test_search_error_still_returns_normal_draft():
    fake_provider = _FakeProvider(error=True)
    service = WebSearchService(fake_provider, enabled=True)
    message, provider = run_free_text(
        "Нужен пост о свежих новостях туризма", web_search_service=service, draft="Черновик C",
    )
    assert len(fake_provider.calls) == 1
    provider.generate_draft.assert_called_once()
    assert "Черновик C" in message.answers[-1][0]
    assert "Источники:" not in message.answers[-1][0]


# D: search results reach the generation context (source_text fed to the LLM).
def test_search_results_reach_generation_context():
    response = _sample_response("q", urls=("https://example.com/visa-rules",))
    fake_provider = _FakeProvider(response)
    service = WebSearchService(fake_provider, enabled=True)
    _, provider = run_free_text("Нужен пост о свежих новостях туризма", web_search_service=service)
    request = provider.generate_draft.call_args.kwargs["source_text"]
    assert "АКТУАЛЬНЫЙ ПОИСК В ИНТЕРНЕТЕ" in request
    assert "https://example.com/visa-rules" in request


# E: Telegram shows a compact sources block after the answer.
def test_telegram_shows_sources_block_after_answer():
    response = _sample_response("q", urls=("https://example.com/a", "https://example.com/b"))
    fake_provider = _FakeProvider(response)
    service = WebSearchService(fake_provider, enabled=True)
    message, _ = run_free_text("Нужен пост о свежих новостях туризма", web_search_service=service)
    text = message.answers[-1][0]
    assert "Источники:" in text
    assert "https://example.com/a" in text
    assert "https://example.com/b" in text
    # Internal-only fields must never leak to the Telegram user.
    assert "fake" not in text
    assert "rank" not in text.lower()


# F: duplicate URLs across results collapse to one in the sources block.
def test_duplicate_urls_collapse_to_one():
    response = _sample_response(
        "q", urls=("https://example.com/a", "https://example.com/a", "https://example.com/b"),
    )
    fake_provider = _FakeProvider(response)
    service = WebSearchService(fake_provider, enabled=True)
    message, _ = run_free_text("Нужен пост о свежих новостях туризма", web_search_service=service)
    text = message.answers[-1][0]
    assert text.count("https://example.com/a") == 1
    assert text.count("https://example.com/b") == 1


# G: web search disabled -> old behavior (no search call, no sources block).
def test_disabled_web_search_behaves_like_before():
    fake_provider = _FakeProvider(_sample_response("q"))
    service = WebSearchService(fake_provider, enabled=False)
    message, provider = run_free_text(
        "Нужен пост о свежих новостях туризма", web_search_service=service, draft="Черновик G",
    )
    assert fake_provider.calls == []
    assert "Черновик G" in message.answers[-1][0]
    assert "Источники:" not in message.answers[-1][0]


# G2: web_search_service not passed at all (default None, existing callers) ->
# identical to every pre-existing on_free_text test in tests/test_journal_handlers.py.
def test_missing_web_search_service_is_fully_inert():
    message, provider = run_free_text("Нужен пост о свежих новостях туризма", draft="Черновик H")
    provider.generate_draft.assert_called_once()
    assert "Черновик H" in message.answers[-1][0]
    assert "Источники:" not in message.answers[-1][0]
