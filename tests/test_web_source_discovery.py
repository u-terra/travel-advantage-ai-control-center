from __future__ import annotations

from datetime import datetime, timezone

from app.domain.sources import WorkspaceSource
from app.services.web_search.base import SearchResponse, SearchResult
from app.services.web_source_discovery import (
    discover_candidate_urls,
    discovery_query,
    mentions_stale_year,
    normalize_article_url,
    page_looks_like_non_content,
)


def source(source_id="trip_com", url="https://www.trip.com/travel-guide/") -> WorkspaceSource:
    return WorkspaceSource(
        id=source_id, name="Trip.com Travel Guide", platform="web",
        source_type="monitored_source", purpose="travel_content_and_market_signals",
        enabled=True, usage_role="monitoring", url=url,
    )


class FakeProvider:
    def __init__(self, results=None, *, response=None, raises=None):
        self._results = results or []
        self._response = response
        self._raises = raises
        self.calls: list[dict] = []

    def search(self, query, *, site=None, limit=5, search_type=None,
               allow_exceeding_configured_max=False):
        self.calls.append({"query": query, "site": site, "limit": limit})
        if self._raises is not None:
            raise self._raises
        if self._response is not None:
            return self._response
        return SearchResponse(query=query, results=self._results, provider="fake")


def result(url, *, title="Title", rank=1):
    return SearchResult(
        title=title, url=url, snippet="snippet", domain="www.trip.com",
        published_at=None, provider="fake", rank=rank,
    )


# --- normalize_article_url ---

def test_normalize_strips_known_tracking_params_only():
    normalized = normalize_article_url(
        "https://Example.com/guide/italy?utm_source=fb&utm_campaign=x&id=42&yclid=1"
    )
    assert normalized == "https://example.com/guide/italy?id=42"


def test_normalize_preserves_fragment():
    assert normalize_article_url("https://example.com/x?utm_source=fb#section") == (
        "https://example.com/x#section"
    )


def test_normalize_two_urls_differing_only_by_tracking_params_are_equal():
    a = normalize_article_url("https://example.com/article-1?utm_source=telegram")
    b = normalize_article_url("https://example.com/article-1?utm_source=newsletter&utm_medium=email")
    assert a == b == "https://example.com/article-1"


def test_normalize_invalid_url_returns_empty_string():
    assert normalize_article_url("not a url") == ""
    assert normalize_article_url("") == ""


# --- discover_candidate_urls ---

def test_discover_restricts_search_to_source_domain():
    provider = FakeProvider(results=[result("https://www.trip.com/travel-guide/italy")])
    discover_candidate_urls(provider, source(), limit=5)
    assert provider.calls[0]["site"] == "www.trip.com"


def test_discover_excludes_the_landing_page_itself():
    provider = FakeProvider(results=[
        result("https://www.trip.com/travel-guide/", rank=1),  # same as source.target
        result("https://www.trip.com/travel-guide/italy", rank=2),
    ])
    candidates = discover_candidate_urls(provider, source())
    assert candidates == ["https://www.trip.com/travel-guide/italy"]


def test_discover_dedupes_results_that_normalize_to_the_same_url():
    provider = FakeProvider(results=[
        result("https://www.trip.com/travel-guide/italy?utm_source=a", rank=1),
        result("https://www.trip.com/travel-guide/italy?utm_source=b", rank=2),
        result("https://www.trip.com/travel-guide/spain", rank=3),
    ])
    candidates = discover_candidate_urls(provider, source())
    assert candidates == [
        "https://www.trip.com/travel-guide/italy",
        "https://www.trip.com/travel-guide/spain",
    ]


def test_discover_returns_empty_list_when_search_returns_none():
    provider = FakeProvider(response=None)
    assert discover_candidate_urls(provider, source()) == []


def test_discover_returns_empty_list_when_search_raises():
    provider = FakeProvider(raises=RuntimeError("boom"))
    assert discover_candidate_urls(provider, source()) == []


def test_discover_respects_limit():
    provider = FakeProvider(results=[
        result(f"https://www.trip.com/travel-guide/page-{i}", rank=i) for i in range(10)
    ])
    candidates = discover_candidate_urls(provider, source(), limit=2)
    assert len(candidates) == 2


# ═══════════════════════════════════════════════════════════════════════════
# Stage 3.1 Quality Gate
# ═══════════════════════════════════════════════════════════════════════════

# --- pre-fetch URL heuristic (requirement 1) ---

def test_discover_rejects_not_found_url():
    provider = FakeProvider(results=[
        result("https://www.aviasales.ru/about/vacancies/backend/not-found", rank=1),
        result("https://www.aviasales.ru/psgr/best-cities", rank=2),
    ])
    candidates = discover_candidate_urls(provider, source(source_id="aviasales_psgr", url="https://www.aviasales.ru/psgr/"))
    assert candidates == ["https://www.aviasales.ru/psgr/best-cities"]


def test_discover_rejects_vacancy_and_career_urls():
    provider = FakeProvider(results=[
        result("https://example.com/careers/backend-engineer", rank=1),
        result("https://example.com/about/vacancies/", rank=2),
        result("https://example.com/support/faq", rank=3),
        result("https://example.com/login", rank=4),
        result("https://example.com/guide/real-article", rank=5),
    ])
    candidates = discover_candidate_urls(provider, source(url="https://example.com/"))
    assert candidates == ["https://example.com/guide/real-article"]


def test_discover_url_heuristic_is_generic_not_aviasales_specific():
    """The same rejection markers apply to ANY source's domain - nothing in
    the implementation reads source.id, so this cannot be an
    Aviasales-only special case."""
    for domain, path in [
        ("tutu.ru", "/vacancies/moscow"), ("trip.com", "/careers/apply"),
        ("t-j.ru", "/support/contact"), ("onetwotrip.com", "/about/team"),
    ]:
        provider = FakeProvider(results=[result(f"https://{domain}{path}")])
        candidates = discover_candidate_urls(provider, source(url=f"https://{domain}/"))
        assert candidates == [], f"{domain}{path} should have been rejected"


# --- OneTwoTrip-style live bug: homepage/section-listing pages, and dedupe ---

def test_discover_rejects_bare_domain_root_as_homepage():
    provider = FakeProvider(results=[
        result("https://www.onetwotrip.com/"),
        result("https://www.onetwotrip.com/blog/kak-sobrat-chemodan-v-otpusk"),
    ])
    candidates = discover_candidate_urls(provider, source(url="https://www.onetwotrip.com/"))
    assert candidates == ["https://www.onetwotrip.com/blog/kak-sobrat-chemodan-v-otpusk"]


def test_discover_rejects_section_listing_page_not_a_specific_article():
    """Live bug: OneTwoTrip Blog's own homepage/index (e.g. '/blog') showing
    up as a 'signal' instead of one specific article inside it."""
    provider = FakeProvider(results=[
        result("https://www.onetwotrip.com/blog/"),
        result("https://www.onetwotrip.com/blog/kak-sobrat-chemodan-v-otpusk"),
    ])
    candidates = discover_candidate_urls(provider, source(url="https://www.onetwotrip.com/"))
    assert candidates == ["https://www.onetwotrip.com/blog/kak-sobrat-chemodan-v-otpusk"]


def test_discover_accepts_a_normal_specific_article_url():
    provider = FakeProvider(results=[
        result("https://www.onetwotrip.com/blog/kak-sobrat-chemodan-v-otpusk"),
    ])
    candidates = discover_candidate_urls(provider, source(url="https://www.onetwotrip.com/"))
    assert candidates == ["https://www.onetwotrip.com/blog/kak-sobrat-chemodan-v-otpusk"]


# --- post-fetch content validation (requirement 2) ---

def test_page_looks_like_non_content_detects_404_body():
    assert page_looks_like_non_content(
        "Страница не найдена", "Извините, запрашиваемая страница не найдена",
    ) is not None
    assert page_looks_like_non_content("404", "Error 404: Page Not Found") is not None


def test_page_looks_like_non_content_detects_vacancy_body_bilingual():
    assert page_looks_like_non_content("Careers", "We are hiring! Join our team today.") is not None
    assert page_looks_like_non_content("Вакансии в компании", "Открытые вакансии: бэкенд-разработчик") is not None


def test_page_looks_like_non_content_accepts_real_article():
    assert page_looks_like_non_content(
        "Гид по Италии", "Полезная статья про путешествия и достопримечательности",
    ) is None


# --- freshness (requirement 3) ---

def test_mentions_stale_year_flags_old_year():
    now = datetime(2026, 9, 13, tzinfo=timezone.utc)
    assert mentions_stale_year("Подборка отелей за 2024 год", now=now) is True


def test_mentions_stale_year_false_when_current_year_also_present():
    now = datetime(2026, 9, 13, tzinfo=timezone.utc)
    assert mentions_stale_year("Основан в 2015, обновлено в 2026 году", now=now) is False


def test_mentions_stale_year_false_with_no_year():
    now = datetime(2026, 9, 13, tzinfo=timezone.utc)
    assert mentions_stale_year("Гид по лучшим пляжам", now=now) is False


def test_discovery_query_includes_current_year_and_month():
    now = datetime(2026, 9, 13, tzinfo=timezone.utc)
    query = discovery_query(now=now)
    assert "2026" in query
    assert "сентября" in query


# --- URL normalization (requirement 4) ---

def test_normalize_strips_cdwuid_attempt_tracking_param():
    normalized = normalize_article_url(
        "https://t-j.ru/flows/travel/some-article?cdwuid_attempt=1"
    )
    assert normalized == "https://t-j.ru/flows/travel/some-article"


def test_normalize_strips_source_param_but_keeps_meaningful_query():
    normalized = normalize_article_url("https://example.com/article?source=telegram&id=7")
    assert normalized == "https://example.com/article?id=7"
