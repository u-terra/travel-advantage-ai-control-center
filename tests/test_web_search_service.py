"""Unit tests for app.services.web_search.service:
- decide_web_search(): the deterministic (no-LLM) search/no-search rules;
- WebSearchService.maybe_search(): wiring decision -> provider, fail-soft
  when disabled/unconfigured;
- format_search_context(): the LLM-facing text block.

No network anywhere - WebSearchProvider is a hand-written fake here, never
the real Yandex adapter.
"""

from __future__ import annotations

import logging

import pytest

from app.services.web_search.base import SearchResponse, SearchResult, WebSearchProvider
from app.services.web_search.service import (
    OFFICIAL_SOURCE_MISSING_USER_NOTICE,
    WebSearchService,
    _merge_official_fallback,
    _official_fallback_query,
    decide_web_search,
    format_search_context,
    official_source_missing,
)
from app.services.web_search.yandex_provider import SEARCH_TYPE_INTERNATIONAL


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

    def __init__(
        self,
        response: SearchResponse | None = None,
        *,
        calls: list | None = None,
        responses: list | None = None,
    ):
        """``response`` is returned for every call (existing behavior).
        ``responses``, if given, is a queue popped one-per-call - lets a test
        give a different answer to the original search vs. the official-
        source fallback search (see the fallback tests below); once
        exhausted, further calls return None, same as any real fail-soft
        provider error."""
        self._response = response
        self._responses = list(responses) if responses is not None else None
        self._calls = calls if calls is not None else []

    def search(
        self, query, *, site=None, limit=5, search_type=None,
        allow_exceeding_configured_max=False,
    ):
        self._calls.append((query, site, limit, search_type, allow_exceeding_configured_max))
        if self._responses is not None:
            return self._responses.pop(0) if self._responses else None
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


# ── Official-source fallback: stage two of official-source priority ────────
#
# Stage one (_rank_by_authority, tested above) can only reorder what a
# single search call already returned. Live testing showed Yandex can
# return 5/5 secondary sources with no official domain at all within
# max_results - these tests cover the targeted one-shot fallback search
# added to close that gap (WebSearchService.maybe_search ->
# _ensure_official_source / _merge_official_fallback).

def _official_result(url: str = "https://imigrasi.go.id/fallback", rank: int = 1) -> SearchResult:
    return SearchResult(
        title="Imigrasi RI", url=url, snippet="Официальные правила.",
        domain="imigrasi.go.id", published_at=None, provider="fake", rank=rank,
    )


def _fallback_response(*results: SearchResult, query: str = "q") -> SearchResponse:
    return SearchResponse(query=query, results=list(results), provider="fake", elapsed_ms=5)


def test_official_already_present_does_not_trigger_fallback_search():
    """Requirement 3: if the first search already has an official domain,
    the reranked result is used as-is and no second search call is made."""
    calls: list = []
    service = WebSearchService(
        _FakeProvider(_multi_source_response(), calls=calls), enabled=True,
    )
    service.maybe_search("Какие сейчас правила въезда в Индонезию?")
    assert len(calls) == 1


def test_official_absent_triggers_exactly_one_fallback_search():
    """Requirement 4: no official domain in the first search's results ->
    exactly one targeted fallback search, never more than one."""
    calls: list = []
    responses = [_sample_response("q"), None]  # 1st: no official; 2nd (fallback): failed/empty
    service = WebSearchService(
        _FakeProvider(calls=calls, responses=responses), enabled=True,
    )
    service.maybe_search("Какие сейчас правила въезда в Индонезию?")
    assert len(calls) == 2


def test_fallback_official_source_is_promoted_first():
    """Requirement 5/7: an official source found only by the fallback search
    ends up first in the merged results."""
    original = _sample_response("Какие правила въезда?")  # example.com, secondary only
    fallback = _fallback_response(_official_result())
    service = WebSearchService(
        _FakeProvider(responses=[original, fallback]), enabled=True,
    )
    result = service.maybe_search("Какие правила въезда?")
    assert result.results[0].domain == "imigrasi.go.id"


def test_fallback_merge_keeps_original_secondary_results():
    """Requirement 5: secondary results from the original search are not
    dropped when the fallback adds an official source."""
    original = _sample_response("Какие правила въезда?")
    fallback = _fallback_response(_official_result())
    service = WebSearchService(
        _FakeProvider(responses=[original, fallback]), enabled=True,
    )
    result = service.maybe_search("Какие правила въезда?")
    domains = {item.domain for item in result.results}
    assert domains == {"imigrasi.go.id", "example.com"}


def test_merge_official_fallback_dedupes_by_url():
    """Requirement 5: a fallback result whose URL already appears among the
    original results must not be duplicated."""
    shared_url = "https://travelblog.example/a"
    original = SearchResponse(
        query="q",
        results=[
            SearchResult(
                title="Blog", url=shared_url, snippet="", domain="travelblog.example",
                published_at=None, provider="fake", rank=1,
            ),
        ],
    )
    fallback = _fallback_response(
        # Same URL as an original result (dedup must drop this one)...
        SearchResult(
            title="Dup", url=shared_url, snippet="", domain="imigrasi.go.id",
            published_at=None, provider="fake", rank=1,
        ),
        # ...but a genuinely new official URL must still be kept.
        _official_result(url="https://imigrasi.go.id/new", rank=2),
    )
    merged = _merge_official_fallback(original, fallback)
    urls = [item.url for item in merged.results]
    assert urls.count(shared_url) == 1
    assert "https://imigrasi.go.id/new" in urls


def test_fallback_also_not_found_context_has_explicit_not_found_status():
    """Requirement 6: when the fallback ALSO fails to find an official
    source, the LLM-facing context must carry an explicit, literal
    OFFICIAL_SOURCE_STATUS: NOT_FOUND marker - not just the general prose
    rule, which live testing showed was not reliably followed on its own."""
    original = _sample_response("Какие правила въезда?")
    fallback = _fallback_response(
        SearchResult(
            title="Ещё один блог", url="https://otherblog.example/a", snippet="",
            domain="otherblog.example", published_at=None, provider="fake", rank=1,
        ),
    )
    service = WebSearchService(
        _FakeProvider(responses=[original, fallback]), enabled=True,
    )
    result = service.maybe_search("Какие правила въезда?")
    text = format_search_context(result)
    assert "OFFICIAL_SOURCE_STATUS: NOT_FOUND" in text


def test_official_found_context_has_explicit_found_status():
    """Requirement 7: once an official source is present (here, straight
    from the first search - no fallback needed), the context must carry the
    explicit FOUND marker and the official source must be first."""
    result = WebSearchService(
        _FakeProvider(_multi_source_response()), enabled=True,
    ).maybe_search("Какие сейчас правила въезда в Индонезию?")
    text = format_search_context(result)
    assert "OFFICIAL_SOURCE_STATUS: FOUND" in text
    assert result.results[0].domain == "imigrasi.go.id"


def test_market_query_does_not_get_a_second_fallback_search_call():
    """Requirement: market/company queries have no "official source"
    concept - they must never get the extra fallback call."""
    calls: list = []
    response = _multi_source_response("Что нового у Travel Advantage?")
    service = WebSearchService(_FakeProvider(response, calls=calls), enabled=True)
    service.maybe_search("Что нового у Travel Advantage?")
    assert len(calls) == 1


def test_format_search_context_omits_status_line_for_non_changeable_rules_query():
    """Requirement 1: the OFFICIAL_SOURCE_STATUS marker only applies to the
    same high-risk/changeable-rules category the fallback itself is gated
    on - a market query must not get a (meaningless) status line."""
    text = format_search_context(_sample_response("Что нового у Travel Advantage?"))
    assert "OFFICIAL_SOURCE_STATUS" not in text


# ── Fallback search scope / query wording fix ───────────────────────────────
#
# Live prod re-test of the fallback above showed it still returned zero
# official domains - diagnosed to two things: the appended Russian
# bureaucratic phrase matched the very RU SEO content it was trying to
# outrank, and the default SEARCH_TYPE_RU scope is itself a Russia-market
# relevance profile, structurally unfavorable to a foreign (non-Russian)
# government domain. These tests cover the fix: the DEFAULT/normal search
# call keeps using the provider's default scope (search_type=None, i.e.
# SEARCH_TYPE_RU inside YandexSearchProvider) unchanged, while ONLY the
# fallback call requests SEARCH_TYPE_INTERNATIONAL (SEARCH_TYPE_COM,
# confirmed against the current official Yandex/AI Studio docs) and a short
# English official-intent phrase instead of the Russian one.

def test_default_search_uses_no_explicit_search_type():
    """The normal/default search call must keep behaving exactly as before -
    no search_type override, so YandexSearchProvider falls back to its own
    default (SEARCH_TYPE_RU)."""
    calls: list = []
    service = WebSearchService(
        _FakeProvider(_multi_source_response(), calls=calls), enabled=True,
    )
    service.maybe_search("Какие сейчас правила въезда в Индонезию?")
    assert calls[0][3] is None  # (query, site, limit, search_type)


def test_fallback_search_uses_international_search_type():
    """The fallback call - and only the fallback call - must request the
    international/worldwide scope, not the default RU-only one."""
    calls: list = []
    responses = [_sample_response("q"), None]  # no official in either call
    service = WebSearchService(
        _FakeProvider(calls=calls, responses=responses), enabled=True,
    )
    service.maybe_search("Какие сейчас правила въезда в Индонезию?")
    assert len(calls) == 2
    assert calls[0][3] is None
    assert calls[1][3] == SEARCH_TYPE_INTERNATIONAL == "SEARCH_TYPE_COM"


def test_fallback_query_uses_english_official_intent_not_russian_phrase():
    """The fallback query text must carry a short English official-intent
    phrase, not the old long Russian bureaucratic phrase that matched the
    very secondary/SEO sites it was meant to outrank."""
    query = "Какие сейчас правила въезда в Индонезию?"
    fallback_query = _official_fallback_query(query)
    assert query in fallback_query
    lowered = fallback_query.lower()
    assert "official" in lowered and "government" in lowered
    assert "консульство" not in lowered and "миграционная служба" not in lowered


def test_fallback_call_receives_the_english_official_intent_query():
    """End to end: the actual second provider.search() call gets the
    English-intent query, not a Russian one."""
    calls: list = []
    responses = [_sample_response("q"), None]
    service = WebSearchService(
        _FakeProvider(calls=calls, responses=responses), enabled=True,
    )
    query = "Какие сейчас правила въезда в Индонезию?"
    service.maybe_search(query)
    fallback_call_query = calls[1][0]
    assert fallback_call_query == _official_fallback_query(query)
    assert "official" in fallback_call_query.lower()


def test_fallback_never_passes_a_site_restriction():
    """Requirement 4: never use site= for the fallback - there is no known
    official domain to restrict to, and guessing one would mean building the
    country->domain registry this task explicitly avoids."""
    calls: list = []
    responses = [_sample_response("q"), None]
    service = WebSearchService(
        _FakeProvider(calls=calls, responses=responses), enabled=True,
    )
    service.maybe_search("Какие сейчас правила въезда в Индонезию?")
    assert calls[1][1] is None  # (query, site, limit, search_type) -> site


# ── Wider fallback search (15) + deterministic no-official-source notice ───
#
# Live prod test after the dual JSON/XML parser fix confirmed the fallback
# genuinely runs under SEARCH_TYPE_COM and gets real results - but none of
# the first 5 was an official domain, and OFFICIAL_SOURCE_STATUS: NOT_FOUND
# reached the context correctly while the model's actual answer still did
# not mention the missing confirmation. Two fixes: (1) the fallback now
# requests 15 raw results instead of 5, so an official domain ranked below
# position 5 still has a chance to be found and promoted - the final
# user-facing size is UNCHANGED (still capped at 5); (2) a deterministic,
# non-LLM notice (official_source_missing() / OFFICIAL_SOURCE_MISSING_USER_
# NOTICE) that callers append directly to the user-visible answer, so it no
# longer depends on the model choosing to follow the prompt instruction.

def _fallback_response_with_n_secondary_and_official_at(
    position: int, total: int, *, query: str = "q",
) -> SearchResponse:
    """Builds a fallback SearchResponse with ``total`` results, all
    secondary except one official domain at zero-based index ``position``."""
    results = []
    for i in range(total):
        if i == position:
            results.append(SearchResult(
                title="Imigrasi RI", url=f"https://imigrasi.go.id/{i}", snippet="...",
                domain="imigrasi.go.id", published_at=None, provider="fake", rank=i + 1,
            ))
        else:
            results.append(SearchResult(
                title=f"Secondary {i}", url=f"https://secondary{i}.example/a", snippet="...",
                domain=f"secondary{i}.example", published_at=None, provider="fake", rank=i + 1,
            ))
    return SearchResponse(query=query, results=results, provider="fake", elapsed_ms=5)


def test_fallback_search_requests_limit_15():
    calls: list = []
    responses = [_sample_response("q"), None]
    service = WebSearchService(_FakeProvider(calls=calls, responses=responses), enabled=True)
    service.maybe_search("Какие сейчас правила въезда в Индонезию?")
    assert len(calls) == 2
    assert calls[1][2] == 15  # (query, site, limit, search_type, allow_exceeding_configured_max)
    assert calls[1][4] is True


def test_default_search_does_not_request_exceeding_configured_max():
    """The default/normal search must stay completely unaffected - it never
    asks to exceed the provider's configured max_results."""
    calls: list = []
    service = WebSearchService(_FakeProvider(_multi_source_response(), calls=calls), enabled=True)
    service.maybe_search("Какие сейчас правила въезда в Индонезию?")
    assert calls[0][4] is False


def test_official_at_position_eight_of_fifteen_is_promoted_to_top_five():
    """An official domain that only shows up at position 8 (index 7) out of
    15 raw fallback results must still end up first in the final,
    user-facing top-5 - this only works because the fallback now actually
    requests 15 results instead of being silently limited to 5."""
    original = _sample_response("Какие правила въезда?")  # secondary only
    fallback = _fallback_response_with_n_secondary_and_official_at(7, 15)
    service = WebSearchService(_FakeProvider(responses=[original, fallback]), enabled=True)
    result = service.maybe_search("Какие правила въезда?")
    assert result.results[0].domain == "imigrasi.go.id"
    assert len(result.results) <= 5


def _secondary_only_response(count: int, *, query: str = "q") -> SearchResponse:
    return SearchResponse(
        query=query,
        results=[
            SearchResult(
                title=f"Secondary {i}", url=f"https://original{i}.example/a", snippet="...",
                domain=f"original{i}.example", published_at=None, provider="fake", rank=i + 1,
            )
            for i in range(count)
        ],
        provider="fake", elapsed_ms=5,
    )


def test_final_merged_result_still_capped_at_five():
    """Requirement: the final, user-facing merged output must stay the same
    size as before (max 5), even though the fallback SEARCH now goes wider
    (15) to find the official domain in the first place. Original search
    already has 5 secondary results on its own, so adding the fallback's
    official result would make 6 without the cap."""
    original = _secondary_only_response(5, query="Какие правила въезда?")
    fallback = _fallback_response_with_n_secondary_and_official_at(0, 15)
    service = WebSearchService(_FakeProvider(responses=[original, fallback]), enabled=True)
    result = service.maybe_search("Какие правила въезда?")
    assert len(result.results) == 5
    assert result.results[0].domain == "imigrasi.go.id"


def test_official_source_missing_true_when_high_risk_and_no_official():
    response = _sample_response("Какие правила въезда?")
    assert official_source_missing(response) is True


def test_official_source_missing_false_when_official_present():
    response = _multi_source_response("Какие правила въезда?")  # includes imigrasi.go.id
    assert official_source_missing(response) is False


def test_official_source_missing_false_for_non_high_risk_query():
    response = _sample_response("Что нового у Travel Advantage?")
    assert official_source_missing(response) is False


@pytest.mark.parametrize("response", [None, SearchResponse(query="Какие правила въезда?", results=[])])
def test_official_source_missing_false_for_none_or_empty_response(response):
    assert official_source_missing(response) is False


def test_official_source_missing_user_notice_wording():
    """Guardrail on the exact required content of the deterministic caveat -
    callers rely on this constant, never a re-derived string."""
    lowered = OFFICIAL_SOURCE_MISSING_USER_NOTICE.lower()
    assert "официальный государственный источник" in lowered
    assert "не найден" in lowered
    assert "вторичных источниках" in lowered
    assert "дополнительной проверки" in lowered


def test_end_to_end_not_found_after_fallback_still_marks_official_source_missing():
    """Full maybe_search() path: first search secondary-only, fallback ALSO
    finds nothing official -> official_source_missing() must be True on the
    final result, guaranteeing the deterministic notice fires downstream."""
    original = _sample_response("Какие правила въезда?")
    fallback = SearchResponse(
        query="Какие правила въезда? official government immigration entry requirements",
        results=[
            SearchResult(
                title="Ещё вторичный", url="https://otherblog.example/a", snippet="",
                domain="otherblog.example", published_at=None, provider="fake", rank=1,
            ),
        ],
        provider="fake", elapsed_ms=5,
    )
    service = WebSearchService(_FakeProvider(responses=[original, fallback]), enabled=True)
    result = service.maybe_search("Какие правила въезда?")
    assert official_source_missing(result) is True


def test_end_to_end_found_after_fallback_clears_official_source_missing():
    original = _sample_response("Какие правила въезда?")
    fallback = _fallback_response_with_n_secondary_and_official_at(0, 15)
    service = WebSearchService(_FakeProvider(responses=[original, fallback]), enabled=True)
    result = service.maybe_search("Какие правила въезда?")
    assert official_source_missing(result) is False


# ══════════════════════════════════════════════════════════════════════════
# TEMPORARY PRODUCTION DIAGNOSTIC LOGGING - delete alongside the "# DIAG:"
# blocks in app/services/web_search/service.py once the investigation is
# done (see that file's matching banner comment for context: a live prod
# report that the fallback still returns zero official domains even after
# 471a689's worldwide search type + English query fix).
# ══════════════════════════════════════════════════════════════════════════

def test_diag_logs_original_query_and_first_search_results(caplog):
    caplog.set_level(logging.INFO, logger="app.services.web_search.service")
    service = WebSearchService(_FakeProvider(_multi_source_response()), enabled=True)
    service.maybe_search("Какие сейчас правила въезда в Индонезию?")
    text = caplog.text
    assert "web_search_fallback_diag: original_query=" in text
    assert "Какие сейчас правила въезда в Индонезию?" in text
    assert "first_search_result:" in text
    assert "domain='imigrasi.go.id'" in text


def test_diag_logs_fallback_not_called_when_official_already_present(caplog):
    caplog.set_level(logging.INFO, logger="app.services.web_search.service")
    service = WebSearchService(_FakeProvider(_multi_source_response()), enabled=True)
    service.maybe_search("Какие сейчас правила въезда в Индонезию?")
    assert "web_search_fallback_diag: fallback_called=False" in caplog.text


def test_diag_logs_fallback_called_with_query_and_search_type(caplog):
    caplog.set_level(logging.INFO, logger="app.services.web_search.service")
    original = _sample_response("q")
    fallback = _fallback_response(_official_result())
    service = WebSearchService(_FakeProvider(responses=[original, fallback]), enabled=True)
    service.maybe_search("Какие правила въезда?")
    text = caplog.text
    assert "web_search_fallback_diag: fallback_called=True" in text
    assert "official government immigration entry requirements" in text
    assert "SEARCH_TYPE_COM" in text
    assert "fallback_result:" in text
    assert "fallback_result_is_official domain='imigrasi.go.id' is_official=True" in text
    assert "merged_result:" in text


def test_diag_logs_official_source_status_and_confirms_it_is_in_context(caplog):
    caplog.set_level(logging.INFO, logger="app.services.web_search.service")
    result = _sample_response("Какие правила въезда?")
    format_search_context(result)
    text = caplog.text
    assert "web_search_fallback_diag: official_source_status='OFFICIAL_SOURCE_STATUS: NOT_FOUND'" in text
    assert "status_line_in_context=True" in text
