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
