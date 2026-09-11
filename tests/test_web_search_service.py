"""Unit tests for app.services.web_search.service:
- decide_web_search(): the deterministic (no-LLM) search/no-search rules;
- WebSearchService.maybe_search(): wiring decision -> provider, fail-soft
  when disabled/unconfigured;
- format_search_context(): the LLM-facing text block.

No network anywhere - WebSearchProvider is a hand-written fake here, never
the real Yandex adapter.
"""

from __future__ import annotations

import pytest

from app.services.web_search.base import SearchResponse, SearchResult, WebSearchProvider
from app.services.web_search.service import (
    WebSearchService,
    decide_web_search,
    format_search_context,
)


# ── B. Decision rules — SEARCH cases from the ORCHESTRAVEL task ─────────────

@pytest.mark.parametrize("query", [
    "Какие сейчас изменения правил въезда в Индонезию для россиян?",
    "Что нового у Travel Advantage?",
    "Какие свежие новости российского туристического рынка?",
    "Проверь в интернете, что взять туристу в Китай",
    "Найди актуальную информацию на example.com",
])
def test_decide_web_search_true_for_task_examples(query):
    decision = decide_web_search(query)
    assert decision.should_search is True
    assert decision.matched_category is not None


@pytest.mark.parametrize("query", [
    "Напиши пост про Индонезию",
    "Перепиши этот текст",
    "Придумай заголовок",
    "Сделай Telegram-пост из этого материала",
])
def test_decide_web_search_false_for_task_examples(query):
    decision = decide_web_search(query)
    assert decision.should_search is False
    assert decision.matched_category is None


def test_decide_web_search_false_for_empty_query():
    assert decide_web_search("").should_search is False
    assert decide_web_search("   ").should_search is False


def test_bare_company_mention_without_actuality_qualifier_does_not_search():
    """Category C is scoped to "в контексте что нового/что происходит/
    актуально" - a bare mention must not turn every content-generation
    request about a named company into a search."""
    decision = decide_web_search("Напиши пост про Travel Advantage для соцсетей")
    assert decision.should_search is False


def test_company_mention_with_actuality_qualifier_does_search():
    decision = decide_web_search("Расскажи, что происходит у MWR Life в этом квартале")
    assert decision.should_search is True
    assert decision.matched_category == "market"


def test_bare_competitor_word_searches_without_needing_a_qualifier():
    decision = decide_web_search("Какой у нас главный конкурент на рынке?")
    assert decision.should_search is True
    assert decision.matched_category == "market"


def test_site_extraction_from_bare_domain():
    decision = decide_web_search("Найди актуальную информацию на example.com")
    assert decision.site == "example.com"


def test_site_extraction_from_full_url():
    decision = decide_web_search("Поищи что нового на https://www.example.com/news")
    assert decision.site == "www.example.com"


def test_url_present_triggers_search_even_without_other_keywords():
    decision = decide_web_search("Посмотри vk.com/example и скажи, о чём там")
    assert decision.should_search is True
    assert decision.matched_category == "site"


# ── WebSearchService wiring / fail-soft ─────────────────────────────────────

class _FakeProvider(WebSearchProvider):
    name = "fake"

    def __init__(self, response: SearchResponse | None = None, *, calls: list | None = None):
        self._response = response
        self._calls = calls if calls is not None else []

    def search(self, query, *, site=None, limit=5):
        self._calls.append((query, site, limit))
        return self._response


def _sample_response(query: str = "q") -> SearchResponse:
    return SearchResponse(
        query=query,
        results=[
            SearchResult(
                title="Заголовок", url="https://example.com/a", snippet="Текст.",
                domain="example.com", published_at=None, provider="fake", rank=1,
            ),
        ],
        provider="fake",
        elapsed_ms=12,
    )


def test_maybe_search_disabled_returns_none_and_never_calls_provider():
    calls: list = []
    service = WebSearchService(_FakeProvider(_sample_response(), calls=calls), enabled=False)
    assert service.maybe_search("Что нового у Travel Advantage?") is None
    assert calls == []


def test_maybe_search_no_provider_returns_none():
    service = WebSearchService(None, enabled=True)
    assert service.maybe_search("Что нового у Travel Advantage?") is None


def test_maybe_search_enabled_but_decision_false_never_calls_provider():
    calls: list = []
    service = WebSearchService(_FakeProvider(_sample_response(), calls=calls), enabled=True)
    assert service.maybe_search("Напиши пост про Индонезию") is None
    assert calls == []


def test_maybe_search_enabled_and_should_search_calls_provider():
    calls: list = []
    response = _sample_response("Что нового у Travel Advantage?")
    service = WebSearchService(_FakeProvider(response, calls=calls), enabled=True)
    result = service.maybe_search("Что нового у Travel Advantage?")
    assert result is response
    assert len(calls) == 1


def test_maybe_search_passes_detected_site_to_provider():
    calls: list = []
    service = WebSearchService(_FakeProvider(_sample_response(), calls=calls), enabled=True)
    service.maybe_search("Найди актуальную информацию на example.com")
    assert calls[0][1] == "example.com"


def test_maybe_search_explicit_site_overrides_detected_site():
    calls: list = []
    service = WebSearchService(_FakeProvider(_sample_response(), calls=calls), enabled=True)
    service.maybe_search("Найди актуальную информацию на example.com", site="other.com")
    assert calls[0][1] == "other.com"


# ── format_search_context ───────────────────────────────────────────────────

def test_format_search_context_empty_for_none_and_no_results():
    assert format_search_context(None) == ""
    assert format_search_context(SearchResponse(query="q", results=[], provider="fake")) == ""


def test_format_search_context_includes_header_query_and_sources():
    response = _sample_response("Какие правила въезда?")
    text = format_search_context(response)
    assert "=== АКТУАЛЬНЫЙ ПОИСК В ИНТЕРНЕТЕ ===" in text
    assert "Какие правила въезда?" in text
    assert "[1] Заголовок" in text
    assert "Текст." in text
    assert "https://example.com/a" in text
    assert "не придумывай" in text.lower()


def test_format_search_context_instructs_model_not_to_add_final_sources_section():
    """H: the model must be told not to print its own closing "Источники"
    section - the Web UI is the single source of truth for that block (see
    app/templates/chat.html's collectAnswerSources/appendAnswerSources)."""
    text = format_search_context(_sample_response("Какие правила въезда?"))
    lowered = text.lower()
    assert "не добавляй" in lowered
    assert "источники" in lowered
    assert "отдельным блоком интерфейса" in lowered
