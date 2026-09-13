from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from app.planner.fetch import FetchedPublicSource, PublicSourceFetchError
from app.repositories.source_catalog_repository import DEFAULT_SOURCE_PACK_IDS
from app.repositories.web_signal_repository import WebSignalRepository
from app.services.llm.models import SourceAnalysisPayload
from app.services.web_signal_collector import WebSignalCollector, format_web_signals_block
from tests.llm_fakes import FakeLLMProvider
from tests.test_source_catalog_repository import new_workspace, setup, web_source
from tests.test_web_source_discovery import FakeProvider, result


def run(value):
    return asyncio.run(value)


def page(url: str, *, title: str = "Title", text: str = "Meaningful travel page text.") -> FetchedPublicSource:
    return FetchedPublicSource(url=url, final_url=url, title=title, text=text, content_type="text/html")


def analysis(*, summary: str = "Summary of the page.", key_facts: tuple[str, ...] = ("Fact one",)) -> SourceAnalysisPayload:
    return SourceAnalysisPayload(
        summary=summary, key_facts=key_facts, disputed_claims=(), audience_value="",
        target_audiences=(), content_angles=(), recommended_formats=(), warnings=(),
    )


def make_fetcher(pages: dict[str, FetchedPublicSource], *, fail: frozenset[str] = frozenset(), calls: list | None = None):
    def fetch(url: str) -> FetchedPublicSource:
        if calls is not None:
            calls.append(url)
        if url in fail or url not in pages:
            raise PublicSourceFetchError("blocked or not stubbed in this test")
        return pages[url]
    return fetch


def build(tmp_path: Path, sources: list[dict]):
    db_path, _, _, owner, catalog = setup(tmp_path, sources)
    signals = WebSignalRepository(db_path)
    run(signals.init())
    return db_path, owner, catalog, signals


# --- collection: enabled/disabled/non-web filtering ---

def test_enabled_web_subscription_is_collected(tmp_path: Path) -> None:
    _, owner, catalog, signals = build(tmp_path, [web_source("src-1", url="https://example.com/a")])
    fetcher = make_fetcher({"https://example.com/a": page("https://example.com/a")})
    provider = FakeLLMProvider(analysis=analysis())
    collector = WebSignalCollector(catalog, signals, provider, fetcher=fetcher)

    outcome = run(collector.collect_for_workspace(owner))

    assert outcome.sources_attempted == 1
    assert outcome.sources_collected == 1
    assert outcome.failed_source_ids == ()
    stored = run(signals.list_for_workspace(owner))
    assert len(stored) == 1
    assert stored[0].source_id == "src-1"
    assert stored[0].summary == "Summary of the page."
    assert stored[0].item_url == "https://example.com/a"


def test_disabled_web_subscription_is_not_collected(tmp_path: Path) -> None:
    _, owner, catalog, signals = build(
        tmp_path, [web_source("src-1", url="https://example.com/a", enabled=False)]
    )
    fetcher = make_fetcher({"https://example.com/a": page("https://example.com/a")})
    provider = FakeLLMProvider(analysis=analysis())
    collector = WebSignalCollector(catalog, signals, provider, fetcher=fetcher)

    outcome = run(collector.collect_for_workspace(owner))

    assert outcome.sources_attempted == 0
    assert outcome.sources_collected == 0
    assert run(signals.list_for_workspace(owner)) == []


def test_non_web_platform_subscription_is_ignored(tmp_path: Path) -> None:
    """Stage 2 only implements platform="web" - a legacy telegram/vk/rss
    subscription must never reach the fetcher or LLM here at all (that path
    stays exclusively Travel Lead Radar's, see requirement 7)."""
    telegram_source = {
        "id": "tg-1", "name": "Telegram", "platform": "telegram",
        "source_type": "monitored_source", "purpose": "content", "enabled": True,
        "priority": 40, "notes": "", "username": "example",
    }
    _, owner, catalog, signals = build(tmp_path, [telegram_source])
    calls: list[str] = []
    fetcher = make_fetcher({}, calls=calls)
    provider = FakeLLMProvider(analysis=analysis())
    collector = WebSignalCollector(catalog, signals, provider, fetcher=fetcher)

    outcome = run(collector.collect_for_workspace(owner))

    assert outcome.sources_attempted == 0
    assert calls == []
    assert run(signals.list_for_workspace(owner)) == []


# --- error isolation (requirement 9) ---

def test_one_source_fetch_failure_does_not_break_others(tmp_path: Path) -> None:
    sources = [
        web_source("good", url="https://example.com/good"),
        web_source("bad", url="https://example.com/bad"),
    ]
    _, owner, catalog, signals = build(tmp_path, sources)
    fetcher = make_fetcher(
        {"https://example.com/good": page("https://example.com/good")},
        fail=frozenset({"https://example.com/bad"}),
    )
    provider = FakeLLMProvider(analysis=analysis())
    collector = WebSignalCollector(catalog, signals, provider, fetcher=fetcher)

    outcome = run(collector.collect_for_workspace(owner))

    assert outcome.sources_attempted == 2
    assert outcome.sources_collected == 1
    assert set(outcome.failed_source_ids) == {"bad"}
    stored_ids = {r.source_id for r in run(signals.list_for_workspace(owner))}
    assert stored_ids == {"good"}


def test_one_source_llm_analysis_failure_does_not_break_others(tmp_path: Path) -> None:
    sources = [
        web_source("good", url="https://example.com/good"),
        web_source("noanalysis", url="https://example.com/na"),
    ]
    _, owner, catalog, signals = build(tmp_path, sources)
    fetcher = make_fetcher({
        "https://example.com/good": page("https://example.com/good", text="good page text"),
        "https://example.com/na": page("https://example.com/na", text="na page text"),
    })
    provider = FakeLLMProvider(analysis=analysis())
    provider.analyze_source.side_effect = (
        lambda *, source_text: None if "na page" in source_text else analysis()
    )
    collector = WebSignalCollector(catalog, signals, provider, fetcher=fetcher)

    outcome = run(collector.collect_for_workspace(owner))

    assert outcome.sources_collected == 1
    assert outcome.failed_source_ids == ("noanalysis",)
    stored_ids = {r.source_id for r in run(signals.list_for_workspace(owner))}
    assert stored_ids == {"good"}


# --- multi-tenant (requirement 5) ---

def test_collector_only_touches_requesting_workspace_sources(tmp_path: Path) -> None:
    db_path, _, _, owner, catalog = setup(tmp_path, [])
    other = new_workspace(db_path, "tenant-b")
    run(catalog.add_source(owner, "https://example.com/owner-only"))
    run(catalog.add_source(other, "https://example.com/other-only"))
    signals = WebSignalRepository(db_path)
    run(signals.init())

    calls: list[str] = []
    fetcher = make_fetcher(
        {
            "https://example.com/owner-only": page("https://example.com/owner-only"),
            "https://example.com/other-only": page("https://example.com/other-only"),
        },
        calls=calls,
    )
    provider = FakeLLMProvider(analysis=analysis())
    collector = WebSignalCollector(catalog, signals, provider, fetcher=fetcher)

    run(collector.collect_for_workspace(owner))

    assert calls == ["https://example.com/owner-only"]
    assert [r.source_url for r in run(signals.list_for_workspace(owner))] == [
        "https://example.com/owner-only"
    ]
    assert run(signals.list_for_workspace(other)) == []


# --- ORCHESTRAVEL Stage 1 gap closed (requirement 11, last bullet) ---

def test_default_source_pack_has_real_path_to_output(tmp_path: Path) -> None:
    """Stage 1 (see audit) left assign_default_sources() producing only DB
    rows - nothing read them. This proves the gap is closed for at least
    one real default-pack source, using the ACTUAL production seed
    (config/sources.json) and the ACTUAL DEFAULT_SOURCE_PACK_IDS, not a
    stand-in list. The other 8 default sources are deliberately left
    unstubbed in the fake fetcher - they fail closed (PublicSourceFetchError)
    and are reported in failed_source_ids instead of raising, which is
    exactly requirement 9's isolation guarantee exercised for real."""
    db_path, _, _, owner, catalog = setup(tmp_path, [])
    run(catalog.assign_default_sources(owner))

    signals = WebSignalRepository(db_path)
    run(signals.init())

    workspace_sources = run(catalog.list_for_workspace(owner))
    trip_com = next(s for s in workspace_sources if s.id == "trip_com")
    assert trip_com.enabled

    fetcher = make_fetcher({trip_com.target: page(trip_com.target, text="Trip.com travel guide content")})
    provider = FakeLLMProvider(analysis=analysis(summary="Гид по направлениям Trip.com"))
    collector = WebSignalCollector(catalog, signals, provider, fetcher=fetcher)

    outcome = run(collector.collect_for_workspace(owner))

    assert outcome.sources_attempted == len(DEFAULT_SOURCE_PACK_IDS)
    assert outcome.sources_collected == 1
    assert "trip_com" not in outcome.failed_source_ids

    stored = run(signals.list_for_workspace(owner))
    assert [r.source_id for r in stored] == ["trip_com"]
    assert stored[0].source_url == trip_com.target
    assert stored[0].summary == "Гид по направлениям Trip.com"


# --- Telegram rendering ---

def test_format_web_signals_block_empty_when_no_records() -> None:
    assert format_web_signals_block([]) == ""


def test_format_web_signals_block_renders_title_and_source() -> None:
    record = SimpleNamespace(
        title="Гид по Италии", summary="Короткое обоснование.",
        item_url="https://example.com/italy", source_url="https://example.com/italy",
        source_name="Trip.com Travel Guide",
    )
    block = format_web_signals_block([record])
    assert "Гид по Италии" in block
    assert "Trip.com Travel Guide" in block
    assert "https://example.com/italy" in block


# ═══════════════════════════════════════════════════════════════════════════
# Stage 3: discovery pipeline
# ═══════════════════════════════════════════════════════════════════════════

def test_discovery_finds_multiple_article_urls_for_one_source(tmp_path: Path) -> None:
    _, owner, catalog, signals = build(tmp_path, [web_source("trip_com", url="https://www.trip.com/travel-guide/")])
    search_provider = FakeProvider(results=[
        result("https://www.trip.com/travel-guide/italy", rank=1),
        result("https://www.trip.com/travel-guide/spain", rank=2),
        result("https://www.trip.com/travel-guide/japan", rank=3),
    ])
    fetch_pages = {
        "https://www.trip.com/travel-guide/italy": page("https://www.trip.com/travel-guide/italy", title="Italy guide"),
        "https://www.trip.com/travel-guide/spain": page("https://www.trip.com/travel-guide/spain", title="Spain guide"),
        "https://www.trip.com/travel-guide/japan": page("https://www.trip.com/travel-guide/japan", title="Japan guide"),
    }
    fetcher = make_fetcher(fetch_pages)
    provider = FakeLLMProvider(analysis=analysis())
    collector = WebSignalCollector(
        catalog, signals, provider, fetcher=fetcher, web_search_provider=search_provider,
    )

    outcome = run(collector.collect_for_workspace(owner))

    assert outcome.sources_attempted == 1
    assert outcome.sources_collected == 1       # one distinct source ...
    assert outcome.articles_collected == 3      # ... but three articles
    stored = run(signals.list_for_workspace(owner))
    assert len(stored) == 3
    assert {r.item_url for r in stored} == set(fetch_pages)
    assert all(not r.is_fallback for r in stored)


def test_repeat_collection_does_not_duplicate_discovered_articles(tmp_path: Path) -> None:
    _, owner, catalog, signals = build(tmp_path, [web_source("trip_com", url="https://www.trip.com/travel-guide/")])
    search_provider = FakeProvider(results=[result("https://www.trip.com/travel-guide/italy")])
    fetcher = make_fetcher({"https://www.trip.com/travel-guide/italy": page("https://www.trip.com/travel-guide/italy")})
    provider = FakeLLMProvider(analysis=analysis())
    collector = WebSignalCollector(catalog, signals, provider, fetcher=fetcher, web_search_provider=search_provider)

    run(collector.collect_for_workspace(owner))
    run(collector.collect_for_workspace(owner))

    stored = run(signals.list_for_workspace(owner))
    assert len(stored) == 1


def test_specific_articles_rank_above_landing_page_fallback(tmp_path: Path) -> None:
    """A source with a real discovered article must always be shown ahead of
    a source that only produced a landing-page fallback - regardless of
    which one happened to be collected/inserted first (requirement 6/11)."""
    sources = [
        web_source("with_article", url="https://a.example.com/hub"),
        web_source("fallback_only", url="https://b.example.com/hub"),
    ]
    _, owner, catalog, signals = build(tmp_path, sources)

    def fake_discover(provider, source, *, limit):
        if source.id == "with_article":
            return ["https://a.example.com/hub/article-1"]
        return []  # "fallback_only" never finds a specific article

    fetcher = make_fetcher({
        "https://a.example.com/hub/article-1": page("https://a.example.com/hub/article-1"),
        "https://b.example.com/hub": page("https://b.example.com/hub"),
    })
    llm_provider = FakeLLMProvider(analysis=analysis())
    collector = WebSignalCollector(
        catalog, signals, llm_provider, fetcher=fetcher, web_search_provider=FakeProvider(),
    )
    collector._discover = fake_discover  # deterministic per-source routing, see helper above

    run(collector.collect_for_workspace(owner))

    stored = run(signals.list_for_workspace(owner))
    assert [r.source_id for r in stored] == ["with_article", "fallback_only"]
    assert stored[0].is_fallback is False
    assert stored[1].is_fallback is True


def test_one_source_discovery_failure_does_not_break_others(tmp_path: Path) -> None:
    sources = [
        web_source("good", url="https://good.example.com/hub"),
        web_source("bad_discovery", url="https://bad.example.com/hub"),
    ]
    _, owner, catalog, signals = build(tmp_path, sources)

    def flaky_discover(provider, source, *, limit):
        if source.id == "bad_discovery":
            raise RuntimeError("search backend down")
        return ["https://good.example.com/hub/article"]

    fetcher = make_fetcher({
        "https://good.example.com/hub/article": page("https://good.example.com/hub/article"),
        "https://bad.example.com/hub": page("https://bad.example.com/hub"),
    })
    llm_provider = FakeLLMProvider(analysis=analysis())
    collector = WebSignalCollector(
        catalog, signals, llm_provider, fetcher=fetcher, web_search_provider=FakeProvider(),
    )
    collector._discover = flaky_discover

    outcome = run(collector.collect_for_workspace(owner))

    # bad_discovery's raised exception is caught in _collect_source, which
    # then falls back to its own landing page rather than losing the source
    # entirely - "good" is completely unaffected either way.
    stored = {r.source_id: r for r in run(signals.list_for_workspace(owner))}
    assert stored["good"].is_fallback is False
    assert stored["bad_discovery"].is_fallback is True
    assert outcome.failed_source_ids == ()


def test_landing_page_fallback_when_discovery_unavailable(tmp_path: Path) -> None:
    """No web_search_provider at all (e.g. WEB_SEARCH_ENABLED=false in prod,
    or a caller that predates Stage 3) - collector must behave exactly like
    Stage 2: one landing-page signal per source, marked as a fallback."""
    _, owner, catalog, signals = build(tmp_path, [web_source("src-1", url="https://example.com/a")])
    fetcher = make_fetcher({"https://example.com/a": page("https://example.com/a")})
    llm_provider = FakeLLMProvider(analysis=analysis())
    collector = WebSignalCollector(catalog, signals, llm_provider, fetcher=fetcher)  # no web_search_provider

    outcome = run(collector.collect_for_workspace(owner))

    assert outcome.articles_collected == 1
    stored = run(signals.list_for_workspace(owner))
    assert len(stored) == 1
    assert stored[0].is_fallback is True
    assert stored[0].item_url == "https://example.com/a"


def test_trip_participates_in_general_pipeline_without_forced_quota(tmp_path: Path) -> None:
    """Requirement 8: trip_com goes through the exact same generic pipeline
    as every other source - no special-casing to always include it, and no
    special-casing to exclude it. When it fails, it simply contributes
    nothing while unrelated sources are collected normally; when it
    succeeds, it is treated identically to every other source (no ranking
    boost - see _collect_source's own docstring: it never reads source.id)."""
    sources = [
        web_source("trip_com", url="https://www.trip.com/travel-guide/"),
        web_source("aviasales_psgr", url="https://www.aviasales.ru/psgr/"),
        web_source("tutu_guide", url="https://www.tutu.ru/geo/"),
    ]
    _, owner, catalog, signals = build(tmp_path, sources)

    # Trip has no usable content today (search + landing page both fail) -
    # the other two succeed normally, with real discovered articles.
    def discover(provider, source, *, limit):
        if source.id == "trip_com":
            return []
        return [f"https://{source.id}.example.com/article"]

    fetcher = make_fetcher({
        "https://aviasales_psgr.example.com/article": page("https://aviasales_psgr.example.com/article"),
        "https://tutu_guide.example.com/article": page("https://tutu_guide.example.com/article"),
        # trip_com's landing page fallback deliberately NOT stubbed -> fails closed.
    })
    llm_provider = FakeLLMProvider(analysis=analysis())
    collector = WebSignalCollector(
        catalog, signals, llm_provider, fetcher=fetcher, web_search_provider=FakeProvider(),
    )
    collector._discover = discover

    outcome = run(collector.collect_for_workspace(owner))

    assert outcome.failed_source_ids == ("trip_com",)
    stored_ids = {r.source_id for r in run(signals.list_for_workspace(owner))}
    assert stored_ids == {"aviasales_psgr", "tutu_guide"}


def test_trip_gets_no_ranking_boost_when_it_does_succeed(tmp_path: Path) -> None:
    """The inverse of the test above: when Trip DOES produce a real article,
    it is stored/ranked exactly like any other source's article - nothing
    in the pipeline reorders results to put Trip first."""
    sources = [
        web_source("trip_com", url="https://www.trip.com/travel-guide/"),
        web_source("aviasales_psgr", url="https://www.aviasales.ru/psgr/"),
    ]
    _, owner, catalog, signals = build(tmp_path, sources)
    fetcher = make_fetcher({
        "https://trip_com.example.com/article": page("https://trip_com.example.com/article"),
        "https://aviasales_psgr.example.com/article": page("https://aviasales_psgr.example.com/article"),
    })
    llm_provider = FakeLLMProvider(analysis=analysis())
    collector = WebSignalCollector(
        catalog, signals, llm_provider, fetcher=fetcher, web_search_provider=FakeProvider(),
    )
    collector._discover = lambda provider, source, *, limit: [f"https://{source.id}.example.com/article"]

    run(collector.collect_for_workspace(owner))

    stored = run(signals.list_for_workspace(owner))
    # Both are real (non-fallback) articles from the same run - ranked by
    # id (insertion order = catalog order), not by source identity.
    assert all(not r.is_fallback for r in stored)
    assert {r.source_id for r in stored} == {"trip_com", "aviasales_psgr"}


def test_tenant_isolation_holds_with_discovery_enabled(tmp_path: Path) -> None:
    db_path, _, _, owner, catalog = setup(tmp_path, [])
    other = new_workspace(db_path, "tenant-b")
    run(catalog.add_source(owner, "https://owner-only.example.com/hub"))
    run(catalog.add_source(other, "https://other-only.example.com/hub"))
    signals = WebSignalRepository(db_path)
    run(signals.init())

    fetcher = make_fetcher({
        "https://owner-only.example.com/hub/a": page("https://owner-only.example.com/hub/a"),
        "https://other-only.example.com/hub/a": page("https://other-only.example.com/hub/a"),
    })
    llm_provider = FakeLLMProvider(analysis=analysis())
    collector = WebSignalCollector(
        catalog, signals, llm_provider, fetcher=fetcher, web_search_provider=FakeProvider(),
    )
    collector._discover = lambda provider, source, *, limit: [f"{source.target.rstrip('/')}/a"]

    run(collector.collect_for_workspace(owner))

    assert len(run(signals.list_for_workspace(owner))) == 1
    assert run(signals.list_for_workspace(other)) == []


# ═══════════════════════════════════════════════════════════════════════════
# Stage 3.1 Quality Gate - pipeline integration
# ═══════════════════════════════════════════════════════════════════════════

def test_content_level_404_rejection_falls_back_to_landing_page(tmp_path: Path) -> None:
    """A candidate whose URL looks fine but whose FETCHED content is a 404/
    vacancy page must be rejected post-fetch, and - if it was the only
    candidate discovered - the source falls back to its landing page
    (requirement 2 + 5)."""
    _, owner, catalog, signals = build(tmp_path, [web_source("aviasales_psgr", url="https://www.aviasales.ru/psgr/")])
    search_provider = FakeProvider(results=[result("https://www.aviasales.ru/psgr/some-page")])
    fetcher = make_fetcher({
        "https://www.aviasales.ru/psgr/some-page": page(
            "https://www.aviasales.ru/psgr/some-page",
            title="Страница не найдена", text="Извините, запрашиваемая страница не найдена",
        ),
        "https://www.aviasales.ru/psgr/": page("https://www.aviasales.ru/psgr/", title="Aviasales ПСЖР"),
    })
    llm_provider = FakeLLMProvider(analysis=analysis())
    collector = WebSignalCollector(catalog, signals, llm_provider, fetcher=fetcher, web_search_provider=search_provider)

    outcome = run(collector.collect_for_workspace(owner))

    stored = run(signals.list_for_workspace(owner))
    assert len(stored) == 1
    assert stored[0].is_fallback is True
    assert stored[0].item_url == "https://www.aviasales.ru/psgr/"


def test_stale_dated_article_ranks_below_fresh_article(tmp_path: Path) -> None:
    """Requirement 3: a discovered article whose content names an old year
    must rank below one that does not, even though both are real
    (non-fallback) articles."""
    sources = [
        web_source("stale_src", url="https://a.example.com/hub"),
        web_source("fresh_src", url="https://b.example.com/hub"),
    ]
    _, owner, catalog, signals = build(tmp_path, sources)
    fetcher = make_fetcher({
        "https://a.example.com/hub/old": page("https://a.example.com/hub/old", title="Подборка за 2024 год"),
        "https://b.example.com/hub/new": page("https://b.example.com/hub/new", title="Гид по Италии"),
    })

    class PerSourceLLM:
        name = "fake"
        is_configured = True

        def analyze_source(self, *, source_text):
            if "2024" in source_text or "Подборка" in source_text:
                return analysis(summary="Подборка отелей за 2024 год")
            return analysis(summary="Актуальный гид по направлениям")

        def generate_draft(self, **kw): return None
        def check_text(self, **kw): return None
        def propose_content_topics(self, **kw): return None

    def discover(provider, source, *, limit):
        if source.id == "stale_src":
            return ["https://a.example.com/hub/old"]
        return ["https://b.example.com/hub/new"]

    collector = WebSignalCollector(
        catalog, signals, PerSourceLLM(), fetcher=fetcher, web_search_provider=FakeProvider(),
    )
    collector._discover = discover

    run(collector.collect_for_workspace(owner))

    stored = run(signals.list_for_workspace(owner))
    assert [r.source_id for r in stored] == ["fresh_src", "stale_src"]
    assert stored[0].is_stale_dated is False
    assert stored[1].is_stale_dated is True


def test_landing_fallback_only_when_all_candidates_rejected(tmp_path: Path) -> None:
    """Discovery finds candidates, but every one of them is quality-rejected
    (fetch failure, content rejection) - the source must still fall back to
    its landing page rather than contributing nothing (requirement 5)."""
    _, owner, catalog, signals = build(tmp_path, [web_source("onetwotrip_blog", url="https://www.onetwotrip.com/ru/blog/")])
    search_provider = FakeProvider(results=[
        result("https://www.onetwotrip.com/ru/blog/broken-link"),
        result("https://www.onetwotrip.com/ru/blog/404-page"),
    ])
    fetcher = make_fetcher({
        "https://www.onetwotrip.com/ru/blog/404-page": page(
            "https://www.onetwotrip.com/ru/blog/404-page",
            title="404", text="Error 404: Page Not Found",
        ),
        "https://www.onetwotrip.com/ru/blog/": page(
            "https://www.onetwotrip.com/ru/blog/", title="OneTwoTrip Blog",
        ),
        # "broken-link" deliberately NOT stubbed -> PublicSourceFetchError
    })
    llm_provider = FakeLLMProvider(analysis=analysis())
    collector = WebSignalCollector(catalog, signals, llm_provider, fetcher=fetcher, web_search_provider=search_provider)

    run(collector.collect_for_workspace(owner))

    stored = run(signals.list_for_workspace(owner))
    assert len(stored) == 1
    assert stored[0].is_fallback is True
    assert stored[0].item_url == "https://www.onetwotrip.com/ru/blog/"
