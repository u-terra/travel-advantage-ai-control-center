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


# ── B extended: new high-risk markers (tourist fees, customs, passport,
# medical entry requirements) added for the official-source-priority /
# geo-scope-guard task ──────────────────────────────────────────────────────

@pytest.mark.parametrize("query", [
    "Какой сейчас туристический сбор на Бали?",
    "Расскажи про курортный сбор для иностранцев",
    "Какой налог для туристов в Индонезии?",
    "Что нужно задекларировать на таможне?",
    "Какие сейчас паспортные требования для въезда в Индонезию?",
    "Какие требования к паспорту для визы?",
    "Какие медицинские требования для въезда в страну?",
])
def test_decide_web_search_true_for_new_high_risk_markers(query):
    # matched_category is diagnostic-only (see decide_web_search's docstring
    # - order matters only for the label, not for should_search); some of
    # these also contain a freshness/other marker that wins the label first
    # (e.g. "сейчас"), so only should_search is asserted here. The
    # authority-reranking gate itself is checked separately below, since it
    # deliberately does NOT rely on this label (see maybe_search).
    decision = decide_web_search(query)
    assert decision.should_search is True


@pytest.mark.parametrize("query", [
    "Какой сейчас туристический сбор на Бали?",
    "Расскажи про курортный сбор для иностранцев",
    "Какой налог для туристов в Индонезии?",
    "Что нужно задекларировать на таможне?",
    "Какие сейчас паспортные требования для въезда в Индонезию?",
    "Какие требования к паспорту для визы?",
    "Какие медицинские требования для въезда в страну?",
])
def test_new_high_risk_markers_trigger_authority_reranking(query):
    """End-to-end through WebSearchService.maybe_search - the gate that
    actually matters, since it is what decides whether official-source
    priority runs for these queries in production."""
    service = WebSearchService(_FakeProvider(_multi_source_response(query)), enabled=True)
    result = service.maybe_search(query)
    assert result.results[0].domain == "imigrasi.go.id"


@pytest.mark.parametrize("query", [
    "Собери данные по нашим клиентам за сбор информации",
    "Напиши пост про подготовку к сбору документов для визы завтра",
    "Какой у нас план по налогам в этом квартале",
    "Найди мой паспортный стол в этом районе",
])
def test_new_markers_do_not_over_trigger_on_unrelated_text(query):
    """Guardrail for the task's explicit "не добавляй чрезмерно широких
    маркеров" instruction - a bare "сбор"/"налог"/"паспорт" must not have
    been added, only phrase-level or narrow stems. Note: some of these
    phrasings still legitimately match OTHER, pre-existing categories (e.g.
    "виза" inside "документов для визы", or the freshness marker "сейчас"/
    "завтра"-adjacent wording) - this test only asserts none of the NEW
    markers themselves fire, not that should_search is False overall."""
    decision = decide_web_search(query)
    if decision.matched_category == "changeable_rules":
        lowered = query.lower()
        new_markers = (
            "туристический сбор", "туристического сбора", "туристическим сбором",
            "туристическом сборе", "курортный сбор", "налог для туристов",
            "туристический налог", "таможн", "таможен", "декларац",
            "паспортные требования", "требования к паспорту",
            "медицинские требования",
        )
        assert not any(marker in lowered for marker in new_markers)


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


# ── Official-source priority: authority reranking ───────────────────────────

def _multi_source_response(query: str = "Какие правила въезда?") -> SearchResponse:
    """Three results, official domain deliberately last (as Yandex might
    rank it, since SEO-heavy secondary sources often outrank a government
    site) - the real prod scenario this task fixes."""
    return SearchResponse(
        query=query,
        results=[
            SearchResult(
                title="Блог про Индонезию", url="https://travelblog.example/a",
                snippet="...", domain="travelblog.example",
                published_at=None, provider="fake", rank=1,
            ),
            SearchResult(
                title="Турагентство", url="https://agency.example/b",
                snippet="...", domain="agency.example",
                published_at=None, provider="fake", rank=2,
            ),
            SearchResult(
                title="Imigrasi RI", url="https://imigrasi.go.id/c",
                snippet="...", domain="imigrasi.go.id",
                published_at=None, provider="fake", rank=3,
            ),
        ],
        provider="fake",
        elapsed_ms=10,
    )


def test_official_domain_ranked_first_for_changeable_rules_query():
    calls: list = []
    service = WebSearchService(
        _FakeProvider(_multi_source_response(), calls=calls), enabled=True,
    )
    result = service.maybe_search("Какие сейчас правила въезда в Индонезию?")
    assert result.results[0].domain == "imigrasi.go.id"


def test_reranking_keeps_all_secondary_results():
    service = WebSearchService(_FakeProvider(_multi_source_response()), enabled=True)
    result = service.maybe_search("Какие сейчас правила въезда в Индонезию?")
    domains = {item.domain for item in result.results}
    assert domains == {"travelblog.example", "agency.example", "imigrasi.go.id"}
    assert len(result.results) == 3


def test_reranking_is_stable_within_the_same_authority_tier():
    """Two official domains and two secondary domains each keep their
    original relative (provider-given) order among themselves."""
    response = SearchResponse(
        query="q",
        results=[
            SearchResult(
                title="Secondary A", url="https://a.example/1", snippet="",
                domain="a.example", published_at=None, provider="fake", rank=1,
            ),
            SearchResult(
                title="Official A", url="https://a.gov/1", snippet="",
                domain="a.gov", published_at=None, provider="fake", rank=2,
            ),
            SearchResult(
                title="Secondary B", url="https://b.example/1", snippet="",
                domain="b.example", published_at=None, provider="fake", rank=3,
            ),
            SearchResult(
                title="Official B", url="https://embassy.b.example/1", snippet="",
                domain="embassy.b.example", published_at=None, provider="fake", rank=4,
            ),
        ],
        provider="fake",
        elapsed_ms=1,
    )
    service = WebSearchService(_FakeProvider(response), enabled=True)
    result = service.maybe_search("Какие сейчас правила въезда?")
    assert [item.domain for item in result.results] == [
        "a.gov", "embassy.b.example", "a.example", "b.example",
    ]


def test_no_official_domain_present_leaves_order_unchanged():
    """Stage-one limitation: reranking can only reorder what the provider
    already returned - it cannot invent/fetch an official source that never
    appeared in this search call."""
    service = WebSearchService(_FakeProvider(_sample_response()), enabled=True)
    result = service.maybe_search("Какие сейчас правила въезда в Индонезию?")
    assert result.results[0].domain == "example.com"


def test_reranking_not_applied_outside_changeable_rules_category():
    """Market/company/freshness queries have no "official" domain concept -
    reordering them would be arbitrary, so reranking must not run there."""
    response = _multi_source_response("Что нового у Travel Advantage?")
    service = WebSearchService(_FakeProvider(response), enabled=True)
    result = service.maybe_search("Что нового у Travel Advantage?")
    # Provider order preserved exactly - imigrasi.go.id stays last.
    assert [item.domain for item in result.results] == [
        "travelblog.example", "agency.example", "imigrasi.go.id",
    ]


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


def test_format_search_context_includes_geo_scope_guard():
    """Real prod bug: a Bali-only tourist-fee source got generalized into an
    Indonesia-wide rule. The rules block must tell the model not to do that,
    to always state the geographic level, to keep fees in the source's own
    currency, to say so when no official source was found, and to prefer
    the official source on conflict."""
    text = format_search_context(_sample_response("Какие правила въезда?"))
    lowered = text.lower()
    assert "регион" in lowered and "город" in lowered and "пункт въезда" in lowered
    assert "бали" in lowered and "индонез" in lowered
    assert "usd" in lowered or "eur" in lowered
    assert "официальное подтверждение не найдено" in lowered
    assert "приоритет всегда у официального" in lowered
