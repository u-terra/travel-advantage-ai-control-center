from __future__ import annotations

from app.domain.sources import WorkspaceSource
from app.services.web_search.base import SearchResponse, SearchResult
from app.services.web_source_discovery import discover_candidate_urls, normalize_article_url


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
