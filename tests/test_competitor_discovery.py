from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.domain.competitor_discovery import (
    CandidateClassification,
    CandidateConfidence,
    CandidateStatus,
    canonical_domain,
)
from app.handlers.competitors import (
    add_competitor_candidate,
    discover_new_competitors,
)
from app.keyboards import COMPETITOR_DISCOVERY_ADD_PREFIX, COMPETITOR_DISCOVERY_START
from app.planner.fetch import FetchedPublicSource, PublicSourceFetchError
from app.repositories.competitor_repository import CompetitorRepository
from app.repositories.partner_repository import PartnerRepository, empty_business_context
from app.repositories.workspace_signal_repository import WorkspaceSignalRecord
from app.services.competitor_discovery import CompetitorDiscoveryService
from app.services.llm.models import SourceAnalysisPayload
from tests.llm_fakes import FakeLLMProvider
from tests.test_journal_handlers import Callback, business_profile, context, profile_repository


def run(coro):
    return asyncio.run(coro)


def signal(
    interpretation_id: int, *, item_title: str, item_summary: str, item_url: str,
    source_name: str = "Travel News",
) -> WorkspaceSignalRecord:
    return WorkspaceSignalRecord(
        interpretation_id=interpretation_id, workspace_id=42,
        radar_signal_id=interpretation_id, source_id="src-1",
        usage_role_snapshot="monitoring", status="new", notes="",
        ai_score=None, ai_category=None, ai_reason=None, suggested_message=None,
        created_at="2026-08-31T00:00:00+00:00", raw_created_at="2026-08-31T00:00:00+00:00",
        source_type="rss", origin_type="publisher_post",
        item_title=item_title, item_summary=item_summary, item_url=item_url,
        source_name=source_name,
    )


def fake_signal_repository(records):
    return SimpleNamespace(list_for_workspace=AsyncMock(return_value=records))


# --- domain: canonical_domain dedup (real prod case - Trip.com via VPN/locale) ---

@pytest.mark.parametrize("url", [
    "https://nl.trip.com/?locale=nl-nl",
    "https://www.trip.com/",
    "https://trip.com/guide/all-content/",
])
def test_canonical_domain_dedups_locale_and_www_variants(url):
    assert canonical_domain(url) == "trip.com"


# --- repository: candidate storage ---

def test_upsert_candidate_then_repeat_updates_evidence_not_duplicate(tmp_path: Path):
    partners = PartnerRepository(tmp_path / "db.sqlite3")
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))
    repo = CompetitorRepository(tmp_path / "db.sqlite3")
    run(repo.init())

    first = run(repo.upsert_candidate(
        workspace.id, name="Example Travel",
        discovered_url="https://www.example-travel.com/blog",
        source_title="Example Travel launches", source_url="https://news.example/1",
        description="A new booking platform", evidence=("Сигнал упоминает: booking platform",),
        confidence=CandidateConfidence.MEDIUM,
        classification=CandidateClassification.DIRECT_COMPETITOR, why_it_matters="Test why",
    ))
    run(repo.update_candidate_status(workspace.id, first.candidate_id, CandidateStatus.REVIEWED))

    second = run(repo.upsert_candidate(
        workspace.id, name="Example Travel",
        discovered_url="https://example-travel.com/newsroom",
        source_title="Example Travel raises funding", source_url="https://news.example/2",
        description="Updated summary", evidence=("New evidence line",),
        confidence=CandidateConfidence.HIGH,
        classification=CandidateClassification.DIRECT_COMPETITOR, why_it_matters="Updated why",
    ))

    all_candidates = run(repo.list_candidates_for_workspace(workspace.id))
    assert len(all_candidates) == 1
    assert second.candidate_id == first.candidate_id
    assert second.evidence == ("New evidence line",)
    assert second.confidence is CandidateConfidence.HIGH
    # status a user already set before the repeat run survives the refresh
    assert second.status is CandidateStatus.REVIEWED


def test_known_domains_for_workspace_matches_locale_variant(tmp_path: Path):
    partners = PartnerRepository(tmp_path / "db.sqlite3")
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))
    repo = CompetitorRepository(tmp_path / "db.sqlite3")
    run(repo.init())
    run(repo.add_competitor(workspace.id, "https://nl.trip.com/?locale=nl-nl", label="Trip.com"))

    assert "trip.com" in run(repo.known_domains_for_workspace(workspace.id))


def test_candidate_workspace_isolation(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    partners = PartnerRepository(db_path)
    run(partners.init())
    owner_a, _ = run(partners.ensure_owner_workspace(100))
    provisioned_b = run(partners.provision_partner(
        200, "Independent Agency", "independent-agency",
        business_name="Independent Agency", business_type="independent_agent",
        short_description="Сторонний тревел-агент.", context=empty_business_context(),
    ))
    workspace_b = provisioned_b.workspace.id

    repo = CompetitorRepository(db_path)
    run(repo.init())
    candidate = run(repo.upsert_candidate(
        owner_a.id, name="A", discovered_url="https://a-travel.com",
        source_title="t", source_url="https://a-travel.com", description="d",
        evidence=("e",), confidence=CandidateConfidence.MEDIUM,
        classification=CandidateClassification.MARKET_SIGNAL, why_it_matters="w",
    ))
    assert run(repo.get_candidate_for_workspace(workspace_b, candidate.candidate_id)) is None
    assert run(repo.list_candidates_for_workspace(workspace_b)) == []


# --- service: discovery from already-synced public market signals ---

def test_discover_finds_new_candidates_skips_known_competitor_and_noise(tmp_path: Path):
    partners = PartnerRepository(tmp_path / "db.sqlite3")
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))
    competitor_repo = CompetitorRepository(tmp_path / "db.sqlite3")
    run(competitor_repo.init())
    # Real prod case: Trip.com already known, reached via a VPN/locale URL.
    run(competitor_repo.add_competitor(
        workspace.id, "https://nl.trip.com/?locale=nl-nl", label="Trip.com",
    ))

    records = [
        signal(1, item_title="Trip.com launches new feature",
               item_summary="Trip.com adds AI planning", item_url="https://nl.trip.com/blog/x",
               source_name="Trip.com"),
        signal(2, item_title="Example Travel: new hotel booking platform",
               item_summary="Example Travel is a new travel platform offering hotel booking "
                             "and OTA-style deals across Europe.",
               item_url="https://www.example-travel.io/news", source_name="Travel Weekly"),
        signal(3, item_title="Loyalty trends in travel",
               item_summary="Industry report on new loyalty program mechanics adopted by "
                             "several travel startups this year.",
               item_url="https://industry-news.example/loyalty-report",
               source_name="Industry News"),
        signal(4, item_title="Best beaches in Spain",
               item_summary="A relaxing guide to Spanish beaches for your next vacation.",
               item_url="https://travel-blog.example/spain-beaches", source_name="Travel Blog"),
    ]
    signals = fake_signal_repository(records)
    provider = FakeLLMProvider(analysis=SourceAnalysisPayload(
        summary="Example Travel — независимая booking-платформа для отелей.",
        key_facts=("Запущен AI travel planner", "Есть loyalty-программа"),
        disputed_claims=(), audience_value="v", target_audiences=(), content_angles=(),
        recommended_formats=(), warnings=(),
    ))

    def fetch(url: str) -> FetchedPublicSource:
        if "example-travel" in url:
            return FetchedPublicSource(
                url=url, final_url=url, title="Example Travel",
                text="Example Travel details " * 5, content_type="text/html",
            )
        raise PublicSourceFetchError("blocked")

    service = CompetitorDiscoveryService(signals, competitor_repo, provider, fetcher=fetch)
    candidates = run(service.discover(workspace.id))

    domains = {c.canonical_domain for c in candidates}
    assert "trip.com" not in domains, "known competitor must not be re-suggested"
    assert "travel-blog.example" not in domains, "generic vacation post is noise, not a signal"
    assert "example-travel.io" in domains

    direct = next(c for c in candidates if c.canonical_domain == "example-travel.io")
    assert direct.classification is CandidateClassification.DIRECT_COMPETITOR
    assert direct.confidence is CandidateConfidence.HIGH
    assert direct.status is CandidateStatus.NEW
    assert direct.source_url and direct.source_title  # provenance
    assert direct.evidence
    assert direct.why_it_matters
    assert direct.description  # real LLM-derived description, not a placeholder

    signal_candidate = next(
        c for c in candidates if c.canonical_domain == "industry-news.example"
    )
    assert signal_candidate.classification is CandidateClassification.MARKET_SIGNAL
    assert signal_candidate.evidence and signal_candidate.why_it_matters


def test_discover_excludes_workspaces_own_domain(tmp_path: Path):
    partners = PartnerRepository(tmp_path / "db.sqlite3")
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))
    competitor_repo = CompetitorRepository(tmp_path / "db.sqlite3")
    run(competitor_repo.init())

    records = [signal(
        1, item_title="Example Travel: new hotel booking platform",
        item_summary="Example Travel is a new travel platform offering hotel booking.",
        item_url="https://www.example-travel.io/news", source_name="Travel Weekly",
    )]
    signals = fake_signal_repository(records)
    provider = FakeLLMProvider()

    def always_blocked(url: str) -> FetchedPublicSource:
        raise PublicSourceFetchError("blocked")

    service = CompetitorDiscoveryService(
        signals, competitor_repo, provider, fetcher=always_blocked,
    )
    candidates = run(service.discover(workspace.id, own_domain="https://example-travel.io"))
    assert candidates == ()


# --- Telegram flow: discover -> add -> existing competitor card works ---

def test_telegram_flow_discover_then_add_enables_existing_competitor_card(tmp_path: Path):
    partners = PartnerRepository(tmp_path / "db.sqlite3")
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))
    competitor_repo = CompetitorRepository(tmp_path / "db.sqlite3")
    run(competitor_repo.init())

    records = [signal(
        1, item_title="Example Travel: new hotel booking platform",
        item_summary="Example Travel is a new travel platform offering hotel booking.",
        item_url="https://www.example-travel.io/news", source_name="Travel Weekly",
    )]
    signals = fake_signal_repository(records)
    provider = FakeLLMProvider()
    profiles = profile_repository(business_profile(workspace.id))

    callback = Callback()
    callback.data = COMPETITOR_DISCOVERY_START
    with patch(
        "app.services.competitor_discovery.fetch_public_source_sync",
        side_effect=PublicSourceFetchError("blocked"),
    ):
        run(discover_new_competitors(
            callback, competitor_repo, context(workspace.id), signals, provider, profiles,
        ))

    candidates = run(competitor_repo.list_candidates_for_workspace(workspace.id))
    assert len(candidates) == 1
    candidate = candidates[0]
    # Telegram actually saw the candidate card with an "add" button.
    card_texts = [text for text, _ in callback.message.answers]
    assert any(candidate.name in text for text in card_texts)

    add_callback = Callback()
    add_callback.data = f"{COMPETITOR_DISCOVERY_ADD_PREFIX}{candidate.candidate_id}"
    run(add_competitor_candidate(add_callback, competitor_repo, context(workspace.id)))

    saved = run(competitor_repo.list_for_workspace(workspace.id))
    assert len(saved) == 1
    assert saved[0].label == candidate.name
    assert saved[0].url == candidate.discovered_url

    last_markup = add_callback.message.answers[-1][1]["reply_markup"]
    button_texts = [button.text for row in last_markup.inline_keyboard for button in row]
    assert "🔎 Анализ конкурента" in button_texts
    assert "💡 Идеи для постов" in button_texts
    assert "✍️ Создать материал" in button_texts

    refreshed = run(competitor_repo.get_candidate_for_workspace(workspace.id, candidate.candidate_id))
    assert refreshed.status is CandidateStatus.ADDED
