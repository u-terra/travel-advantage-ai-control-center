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
    _merge_official_fallback,
    _official_fallback_query,
    decide_web_search,
    format_search_context,
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

    def search(self, query, *, site=None, limit=5, search_type=None):
        self._calls.append((query, site, limit, search_type))
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
