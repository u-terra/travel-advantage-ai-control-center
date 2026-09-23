from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.competitor_intelligence import (
    DATA_ORIGIN_DIRECT_FETCH,
    DATA_ORIGIN_RADAR_SIGNAL,
    CompetitorIntelligence,
)
from app.domain.competitors import Competitor
from app.handlers.competitors import create_from_competitor_opportunity
from app.keyboards import COMPETITOR_OPEN_PREFIX, competitors_list_keyboard
from app.planner.fetch import FetchedPublicSource, PublicSourceFetchError
from app.repositories.competitor_repository import CompetitorRepository
from app.repositories.partner_repository import PartnerRepository
from app.repositories.workspace_signal_repository import (
    WorkspaceSignalRecord,
    WorkspaceSignalRepository,
)
from app.services.competitor_intelligence import (
    CompetitorIntelligenceService,
    CompetitorIntelligenceUnavailable,
    _opportunities,
)
from app.services.knowledge_service import KnowledgeBundle
from app.services.llm.models import ContentDraft, SourceAnalysisPayload
from tests.llm_fakes import FakeLLMProvider
from tests.test_journal_handlers import (
    Callback, artifact_repository, business_profile, context, profile_repository,
    user_preferences,
)
from tests.test_workspace_signal_repository import setup as _radar_setup
from tests.test_workspace_signal_repository import workspace as _radar_workspace


def run(coro):
    return asyncio.run(coro)


def _analysis() -> SourceAnalysisPayload:
    return SourceAnalysisPayload(
        summary="Глобальная OTA-платформа объединяет бронирования и travel content.",
        key_facts=(
            "Flights, hotels, trains and attractions are available in one app.",
            "Member deals and Trip Coins support loyalty.",
            "Seasonal destination guides connect inspiration with booking.",
        ),
        disputed_claims=(), audience_value="Помогает путешественникам выбрать идею и спланировать поездку.",
        target_audiences=("путешественники",),
        content_angles=(
            "Как выбирать направление по сезону",
            "Что проверить перед самостоятельным бронированием",
        ),
        recommended_formats=("post",), warnings=(),
    )


def _knowledge() -> KnowledgeBundle:
    item = SimpleNamespace(
        content="OTA-платформа для поиска и бронирования туристических услуг.",
        source_ref="verified TA knowledge", stable_key="ta.platform",
    )
    return KnowledgeBundle(
        question="Travel Advantage", primary_items=(item,), related_items=(),
        facts=(), compliance_facts=(), examples=(), sources=(),
        potentially_ambiguous=False, ambiguity_reasons=(), missing_definitions=(),
    )


def _service(*, analysis=_analysis()):
    calls = []
    def fetch(url: str):
        calls.append(url)
        if "loyalty" in url:
            raise PublicSourceFetchError("blocked")
        return FetchedPublicSource(
            url=url, final_url=url, title=f"Public source {len(calls)}",
            text="Aug 28, 2026 Travel inspiration, member deals, flights and hotels " * 4,
            content_type="text/html",
        )
    provider = FakeLLMProvider(analysis=analysis)
    knowledge = SimpleNamespace(retrieve=AsyncMock(return_value=_knowledge()))
    return CompetitorIntelligenceService(provider, knowledge, fetcher=fetch), provider, calls


def test_trip_com_vertical_slice_has_provenance_and_aligned_opportunities():
    service, provider, calls = _service()
    competitor = Competitor(7, 42, "https://nl.trip.com/?locale=nl-nl", "Trip.com", "now")

    result = run(service.analyze(competitor, ta_affiliated=True))

    assert result.competitor_id == 7
    assert len(result.sources) == 4
    assert result.sources[0].url == "https://nl.trip.com/?locale=nl-nl"
    assert all(source.discovered_at for source in result.sources)
    assert len(result.opportunities) == 3
    assert all(item.competitor_id == 7 and item.source_url for item in result.opportunities)
    assert all(item.travel_advantage_link and "verified TA knowledge" in item.travel_advantage_link for item in result.opportunities)
    assert provider.analyze_source.call_count == 4
    assert "https://www.trip.com/blog" in calls


def test_ta_affiliated_true_queries_ta_knowledge_exactly_once():
    """Regression guard for category A (unchanged behaviour): a TA-affiliated
    workspace still triggers exactly one Travel Advantage knowledge lookup."""
    service, _, _ = _service()
    competitor = Competitor(7, 42, "https://nl.trip.com/?locale=nl-nl", "Trip.com", "now")

    result = run(service.analyze(competitor, ta_affiliated=True))

    service._knowledge.retrieve.assert_awaited_once()
    assert result.travel_advantage_comparison


def test_ta_affiliated_false_never_queries_or_includes_ta_knowledge():
    """Isolation audit fix: an independent (ta_affiliated=False) workspace's
    competitor analysis must not look up Travel Advantage knowledge at
    all - not just leave the comparison unpopulated."""
    service, _, _ = _service()
    competitor = Competitor(7, 42, "https://nl.trip.com/?locale=nl-nl", "Trip.com", "now")

    result = run(service.analyze(competitor, ta_affiliated=False))

    service._knowledge.retrieve.assert_not_awaited()
    assert result.travel_advantage_comparison == ()
    assert all(item.travel_advantage_link is None for item in result.opportunities)


def test_existing_competitor_list_opens_saved_entity():
    keyboard = competitors_list_keyboard(((7, "Trip.com"),))
    button = keyboard.inline_keyboard[0][0]
    assert button.text == "🎯 Trip.com"
    assert button.callback_data == f"{COMPETITOR_OPEN_PREFIX}7"


def test_provider_failure_keeps_real_sources_and_content_opportunities():
    service, _, _ = _service(analysis=None)
    competitor = Competitor(7, 42, "https://nl.trip.com/?locale=nl-nl", "Trip.com", "now")
    result = run(service.analyze(competitor, ta_affiliated=True))
    assert len(result.sources) == 4
    assert len(result.opportunities) == 1
    assert all(item.topic for item in result.opportunities)


def test_intelligence_round_trip_and_selected_opportunity_uses_content_factory(tmp_path):
    partners = PartnerRepository(tmp_path / "db.sqlite3")
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))
    repository = CompetitorRepository(tmp_path / "db.sqlite3")
    run(repository.init())
    competitor = run(repository.add_competitor(
        workspace.id, "https://nl.trip.com/?locale=nl-nl", label="Trip.com",
    ))
    service, _, _ = _service()
    intelligence: CompetitorIntelligence = run(service.analyze(competitor, ta_affiliated=True))
    run(repository.save_intelligence(workspace.id, intelligence))
    restored = run(repository.get_intelligence(workspace.id, competitor.id))
    assert restored is not None and len(restored.opportunities) == 3

    callback = Callback()
    callback.data = f"competitor:create:{competitor.id}:{restored.opportunities[0].id}"
    provider = FakeLLMProvider(draft=ContentDraft("Оригинальный материал", ()))
    artifacts = artifact_repository()
    run(create_from_competitor_opportunity(
        callback, repository, context(workspace.id), provider,
        profile_repository(business_profile(workspace.id)), artifacts,
    ))
    provider.generate_draft.assert_called_once()
    source_text = provider.generate_draft.call_args.kwargs["source_text"]
    assert restored.opportunities[0].source_url in source_text
    assert "не рекламируй" in source_text
    assert "проверяйте срок акции, условия, направление и даты" in source_text
    assert "Оригинальный материал" in callback.message.answers[-1][0]
    # Web/Telegram parity: the draft must also be persisted as a real
    # Artifact (same as the Radar flow), not just shown in chat.
    artifacts.create_artifact_with_initial_version.assert_awaited_once()
    saved_kwargs = artifacts.create_artifact_with_initial_version.call_args.kwargs
    assert saved_kwargs["content"] == "Оригинальный материал"


def test_competitor_opportunity_draft_inherits_personal_style(tmp_path):
    """Same Stage 3B1 parity as every other Content Factory flow
    (material_generation.py, tasks.py): the current user's personal style
    must reach the provider request, not just the workspace Business
    Profile."""
    partners = PartnerRepository(tmp_path / "db.sqlite3")
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))
    repository = CompetitorRepository(tmp_path / "db.sqlite3")
    run(repository.init())
    competitor = run(repository.add_competitor(
        workspace.id, "https://nl.trip.com/?locale=nl-nl", label="Trip.com",
    ))
    service, _, _ = _service()
    intelligence = run(service.analyze(competitor, ta_affiliated=True))
    run(repository.save_intelligence(workspace.id, intelligence))

    callback = Callback()
    callback.data = f"competitor:create:{competitor.id}:{intelligence.opportunities[0].id}"
    provider = FakeLLMProvider(draft=ContentDraft("Черновик", ()))
    profiles = profile_repository(business_profile(workspace.id))
    profiles.get_user_preferences = AsyncMock(return_value=user_preferences(
        telegram_user_id=100, workspace_id=workspace.id,
        style_description="Пишу с юмором", example_posts=("Пример поста",),
        avoid_phrases=("лучший тур",),
    ))

    run(create_from_competitor_opportunity(
        callback, repository, context(workspace.id), provider, profiles,
        artifact_repository(),
    ))
    source_text = provider.generate_draft.call_args.kwargs["source_text"]
    assert "[PERSONAL STYLE - DATA]" in source_text
    assert "Пишу с юмором" in source_text
    assert "Пример поста" in source_text
    assert "лучший тур" in source_text


def test_competitor_opportunity_material_records_data_origin_in_provenance(tmp_path):
    """Fallback provenance must survive into the saved Artifact's
    generation_note - a Radar-signal-fallback analysis must never look
    identical to a fresh direct fetch once it becomes a material."""
    from app.domain.competitor_intelligence import ContentOpportunity

    partners = PartnerRepository(tmp_path / "db.sqlite3")
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))
    repository = CompetitorRepository(tmp_path / "db.sqlite3")
    run(repository.init())
    competitor = run(repository.add_competitor(
        workspace.id, "https://nl.trip.com/?locale=nl-nl", label="Trip.com",
    ))
    opportunity = ContentOpportunity(
        id="opp-fallback", competitor_id=competitor.id, topic="Тема",
        source_title="Источник", source_url="https://example.com", freshness="сегодня",
        key_thesis="Тезис", audience_value="Ценность", own_post_angle="Угол",
        travel_advantage_link=None,
    )
    intelligence = CompetitorIntelligence(
        competitor_id=competitor.id, competitor_label="Trip.com", analyzed_at="now",
        positioning=(), products=(), destinations_and_categories=(), promotions=(),
        loyalty_mechanics=(), service_and_ux=(), strengths=(), travel_advantage_comparison=(),
        fresh_signals=(), sources=(), opportunities=(opportunity,),
        data_origin=DATA_ORIGIN_RADAR_SIGNAL,
    )
    run(repository.save_intelligence(workspace.id, intelligence))

    callback = Callback()
    callback.data = f"competitor:create:{competitor.id}:opp-fallback"
    provider = FakeLLMProvider(draft=ContentDraft("Черновик", ()))
    artifacts = artifact_repository()

    run(create_from_competitor_opportunity(
        callback, repository, context(workspace.id), provider,
        profile_repository(business_profile(workspace.id)), artifacts,
    ))

    saved_kwargs = artifacts.create_artifact_with_initial_version.call_args.kwargs
    assert "radar_signal" in saved_kwargs["generation_note"]


def _source(title="Source"):
    return FetchedPublicSource(
        url="https://example.com/source", final_url="https://example.com/source",
        title=title, text="Aug 28, 2026", content_type="text/html",
    )


def _payload(*facts: str, angles: tuple[str, ...] = ()):
    return SourceAnalysisPayload(
        summary="Competitor source", key_facts=facts, disputed_claims=(),
        audience_value="Полезно путешественникам", target_audiences=(),
        content_angles=angles, recommended_formats=("post",), warnings=(),
    )


def test_opportunity_builds_short_editorial_topic_distinct_from_raw_thesis():
    analysis = _payload(
        "Disneyland vs LEGOLAND: comparison for a family trip",
        "iF Design Award for visual identity",
        angles=("Городской транспорт", "Travel-акции", "Сезонные направления"),
    )
    result = _opportunities(7, [(_source(), analysis)], None)
    assert len(result) == 1
    opportunity = result[0]
    assert opportunity.key_thesis == "Disneyland vs LEGOLAND: comparison for a family trip"
    assert opportunity.topic != opportunity.key_thesis
    assert "Disneyland vs LEGOLAND" in opportunity.topic
    assert "Disneyland vs LEGOLAND" in opportunity.own_post_angle
    assert opportunity.own_post_angle != opportunity.key_thesis
    assert "городской транспорт" not in opportunity.own_post_angle.lower()
    assert "iF Design Award" not in opportunity.own_post_angle
    assert "iF Design Award" not in opportunity.topic


def test_opportunity_semantic_duplicates_are_removed():
    analysis = _payload(
        "Travel demand grows 70% in football host cities",
        "Football host cities see 70% growth in travel demand",
        "Airport transit guide for international travelers",
    )
    result = _opportunities(7, [(_source(), analysis)], None)
    assert len(result) == 2
    assert sum("70%" in item.key_thesis for item in result) == 1


def test_unrelated_angles_are_rejected_instead_of_padding_to_eight():
    analysis = _payload(
        "New destination guide for family resorts",
        "AI travel app adds hotel search",
        "Member rewards and seasonal travel discounts",
        "Booking demand increases for city trips",
        "iF Design Award for corporate typography",
        angles=("Travel-акции", "Сезонные направления", "Городской транспорт"),
    )
    result = _opportunities(7, [(_source(), analysis)], None)
    assert len(result) <= 5
    assert all("Design Award" not in item.key_thesis for item in result)


def test_navigation_and_source_meta_noise_is_dropped():
    analysis = _payload(
        "Read More materials about promo codes and China eSIM guide.",
        "В источнике перечислены статьи Trip.com в разделе Travel Inspiration & Tips.",
        "На сайте перечислены разделы: Hotels, Vluchten, Vlucht+Hotel, Treinen.",
        "https://www.trip.com/blog",
        "Travel the world with Trip.com",
        "2026 Guide to Shanghai Pudong Airport: Transit Visa & PVG Airport Shuttle",
    )
    result = _opportunities(7, [(_source(), analysis)], None)
    assert len(result) == 1
    assert "Pudong" in result[0].topic


def test_one_material_keeps_a_single_strong_opportunity_not_every_raw_fact():
    analysis = _payload(
        "2026 Guide to Shanghai Pudong Airport: Transit Visa & PVG Airport Shuttle",
        "PVG Airport has 2 main terminals: Terminal 1 and Terminal 2. Shanghai "
        "airport shuttle connect PVG Airport to Shanghai downtown and other "
        "nearby cities. Both terminals provide currency exchange service.",
    )
    result = _opportunities(7, [(_source(), analysis)], None)
    assert len(result) == 1
    assert "2026 Guide to Shanghai Pudong Airport" in result[0].key_thesis


def test_composite_index_fact_is_split_into_independent_named_themes():
    analysis = _payload(
        "Темы материалов включают Yiwu Market, ChatGPT Travel Planning, "
        "Shanghai Disneyland vs LEGOLAND Shanghai, Alipay vs Wechat Pay, "
        "визы, транзит, аэропорты, метро, такси, поезда, eSIM, шопинг.",
    )
    result = _opportunities(7, [(_source(), analysis)], None)
    theses = {item.key_thesis for item in result}
    assert "ChatGPT Travel Planning" in theses
    assert all("Темы материалов включают" not in thesis for thesis in theses)


# ── Radar-signal fallback: direct site unreadable, relevant recent signals ──
#
# Step 1-6 of the fallback order (see app.services.competitor_intelligence.
# CompetitorIntelligenceService.analyze): direct fetch first, unchanged when
# it works; only on total direct-fetch failure does the service look for
# genuinely matching, genuinely recent Radar signals already visible to
# THIS workspace; matched evidence is explicitly tagged (never presented as
# the competitor's own site); no match at all falls through to a precise,
# honest refusal instead of a vague one.

def _always_failing_fetch(url: str):
    raise PublicSourceFetchError("blocked")


def _signal_record(
    *, interpretation_id: int = 1, workspace_id: int = 42, item_title: str = "",
    item_summary: str = "", item_url: str = "", source_name: str = "",
    raw_created_at: str | None = None, source_id: str | None = None,
) -> WorkspaceSignalRecord:
    when = raw_created_at or (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    return WorkspaceSignalRecord(
        interpretation_id=interpretation_id, workspace_id=workspace_id,
        radar_signal_id=interpretation_id, source_id=source_id, usage_role_snapshot=None,
        status="new", notes="", ai_score=None, ai_category=None, ai_reason=None,
        suggested_message=None, created_at=when, raw_created_at=when,
        source_type="rss", origin_type="publisher_post", item_title=item_title,
        item_summary=item_summary, item_url=item_url, source_name=source_name,
    )


class _StubSignalRepository:
    """Fake WorkspaceSignalRepository - workspace isolation itself is a
    property of the real repository (tested against a real DB below); this
    stub only needs to hand back canned records and record which
    workspace_id it was asked for."""

    def __init__(self, records: list[WorkspaceSignalRecord]) -> None:
        self._records = records
        self.workspace_ids_queried: list[int] = []

    async def list_for_workspace(self, workspace_id: int, *, limit: int = 200):
        self.workspace_ids_queried.append(workspace_id)
        return self._records


def _service_with_signals(records: list[WorkspaceSignalRecord], *, analysis=None):
    provider = FakeLLMProvider(analysis=analysis or _analysis())
    knowledge = SimpleNamespace(retrieve=AsyncMock(return_value=_knowledge()))
    stub = _StubSignalRepository(records)
    return CompetitorIntelligenceService(
        provider, knowledge, fetcher=_always_failing_fetch,
        workspace_signal_repository=stub,
    ), stub


def test_direct_fetch_success_leaves_data_origin_as_direct_fetch_unchanged():
    """A readable site takes the existing path unchanged and never even
    consults the signal repository."""
    service, _, _ = _service()
    stub = _StubSignalRepository([])
    service._workspace_signal_repository = stub
    competitor = Competitor(7, 42, "https://nl.trip.com/?locale=nl-nl", "Trip.com", "now")

    result = run(service.analyze(competitor, ta_affiliated=True))

    assert result.data_origin == DATA_ORIGIN_DIRECT_FETCH
    assert all(s.origin == DATA_ORIGIN_DIRECT_FETCH for s in result.sources)
    assert stub.workspace_ids_queried == []


def test_direct_fetch_fails_but_matching_recent_signal_used_as_fallback():
    record = _signal_record(
        item_title="Booking.com launches AI trip planner",
        item_summary="New feature announced this week.",
        item_url="https://skift.com/booking-ai-planner",
        workspace_id=42,
    )
    service, stub = _service_with_signals([record])
    competitor = Competitor(7, 42, "https://www.booking.com", "Booking.com", "now")

    result = run(service.analyze(competitor, ta_affiliated=True))

    assert result.data_origin == DATA_ORIGIN_RADAR_SIGNAL
    assert len(result.sources) == 1
    assert result.sources[0].origin == DATA_ORIGIN_RADAR_SIGNAL
    assert result.sources[0].final_url == "https://skift.com/booking-ai-planner"
    assert result.sources[0].freshness == record.raw_created_at[:10]
    assert stub.workspace_ids_queried == [42]
    assert result.positioning  # LLM analysis still ran over the reused path


def test_signal_fallback_matches_by_domain_even_when_label_text_differs():
    record = _signal_record(
        item_title="OTA platform expands loyalty program",
        item_summary="",
        item_url="https://www.booking.com/blog/loyalty-2026",
    )
    service, _ = _service_with_signals([record])
    competitor = Competitor(7, 42, "https://www.booking.com", "Booking.com", "now")

    result = run(service.analyze(competitor, ta_affiliated=True))

    assert result.data_origin == DATA_ORIGIN_RADAR_SIGNAL


def test_signal_fallback_rejects_unrelated_travel_news():
    """Only a genuine match - never any travel-news signal that happens to
    already be in the workspace."""
    record = _signal_record(
        item_title="New direct flight route Paris-Tokyo announced",
        item_summary="Airline adds seasonal service.",
        item_url="https://skift.com/paris-tokyo-route",
    )
    service, _ = _service_with_signals([record])
    competitor = Competitor(7, 42, "https://www.booking.com", "Booking.com", "now")

    with pytest.raises(CompetitorIntelligenceUnavailable) as exc_info:
        run(service.analyze(competitor, ta_affiliated=True))
    assert "Свежие источники" in str(exc_info.value)


# ── Bug fix (live production, competitor_id=6 "Яндекс-путешествия",
# https://travel.yandex.ru/): direct_fetch is correctly blocked by Yandex's
# own SmartCaptcha, and the signal fallback's domain check can never match
# the product's own Telegram channel (item_url domain is "t.me", not
# "travel.yandex.ru") while the label check is a literal, punctuation-
# sensitive match ("Яндекс-путешествия" the saved label vs "Яндекс
# Путешествия" the actual signal text - hyphen vs space). See
# _COMPETITOR_DOMAIN_SOURCE_ID_ALIASES's own docstring in
# app.services.competitor_intelligence for the full reasoning. ─────────────

def test_signal_fallback_matches_known_telegram_alias_for_yandex_travel():
    record = _signal_record(
        item_title="Новые направления на майские",
        item_summary="Подборка курортов с прямыми рейсами.",
        item_url="https://t.me/yandex_travel/12652",
        source_name="Telegram Яндекс Путешествия",
        source_id="telegram_yandex_travel",
    )
    service, _ = _service_with_signals([record])
    competitor = Competitor(6, 42, "https://travel.yandex.ru/", "Яндекс-путешествия", "now")

    result = run(service.analyze(competitor, ta_affiliated=True))

    assert result.data_origin == DATA_ORIGIN_RADAR_SIGNAL
    assert result.sources[0].final_url == "https://t.me/yandex_travel/12652"


def test_signal_fallback_alias_is_scoped_to_the_exact_competitor_domain():
    """The telegram_yandex_travel alias is keyed to travel.yandex.ru only -
    it must not make the same signal match some unrelated competitor that
    happens to also be a travel site."""
    record = _signal_record(
        item_title="Новые направления на майские",
        item_summary="Подборка курортов с прямыми рейсами.",
        item_url="https://t.me/yandex_travel/12652",
        source_name="Telegram Яндекс Путешествия",
        source_id="telegram_yandex_travel",
    )
    service, _ = _service_with_signals([record])
    competitor = Competitor(7, 42, "https://www.booking.com", "Booking.com", "now")

    with pytest.raises(CompetitorIntelligenceUnavailable):
        run(service.analyze(competitor, ta_affiliated=True))


def test_signal_fallback_does_not_alias_other_telegram_channels_to_yandex_travel():
    """A different Telegram channel (different source_id) must not match
    travel.yandex.ru just because its text also mentions the word "Яндекс" -
    the alias map is by exact source_id, never a bare brand-word guess."""
    record = _signal_record(
        item_title="Яндекс запускает новый сервис для бизнеса",
        item_summary="Экспресс-обзор новостей технологического рынка.",
        item_url="https://t.me/some_other_tech_channel/42",
        source_name="Telegram Технологии Яндекса",
        source_id="telegram_some_other_tech_channel",
    )
    service, _ = _service_with_signals([record])
    competitor = Competitor(6, 42, "https://travel.yandex.ru/", "Яндекс-путешествия", "now")

    with pytest.raises(CompetitorIntelligenceUnavailable):
        run(service.analyze(competitor, ta_affiliated=True))


def test_signal_fallback_bare_yandex_word_alone_still_insufficient():
    """Regression guard: a signal that only contains the generic word
    "Яндекс" (no source_id alias, no domain match, no exact label match)
    must still be rejected - this fix must not have loosened the existing
    label-match narrowness."""
    record = _signal_record(
        item_title="Яндекс объявил финансовые результаты за квартал",
        item_summary="Общий разбор показателей группы компаний.",
        item_url="https://skift.com/yandex-earnings",
        source_name="Skift",
    )
    service, _ = _service_with_signals([record])
    competitor = Competitor(6, 42, "https://travel.yandex.ru/", "Яндекс-путешествия", "now")

    with pytest.raises(CompetitorIntelligenceUnavailable):
        run(service.analyze(competitor, ta_affiliated=True))


def test_signal_fallback_onetwotrip_domain_match_still_works_unchanged():
    """Control: this fix must not affect the existing, already-working
    domain-match path for an unrelated competitor (OneTwoTrip, competitor_id
    7 in the live production workspace)."""
    record = _signal_record(
        item_title="OneTwoTrip запускает новую программу лояльности",
        item_summary="Кэшбэк на билеты и отели.",
        item_url="https://www.onetwotrip.com/blog/loyalty-2026",
    )
    service, _ = _service_with_signals([record])
    competitor = Competitor(7, 42, "https://www.onetwotrip.com", "OneTwoTrip", "now")

    result = run(service.analyze(competitor, ta_affiliated=True))

    assert result.data_origin == DATA_ORIGIN_RADAR_SIGNAL
    assert result.sources[0].final_url == "https://www.onetwotrip.com/blog/loyalty-2026"


def test_signal_fallback_ignores_signals_older_than_the_freshness_window():
    stale = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    record = _signal_record(
        item_title="Booking.com quarterly earnings beat estimates",
        item_summary="", item_url="https://skift.com/booking-earnings",
        raw_created_at=stale,
    )
    service, _ = _service_with_signals([record])
    competitor = Competitor(7, 42, "https://www.booking.com", "Booking.com", "now")

    with pytest.raises(CompetitorIntelligenceUnavailable):
        run(service.analyze(competitor, ta_affiliated=True))


def test_no_direct_source_and_no_signals_raises_precise_strategic_message():
    service, _ = _service_with_signals([])
    competitor = Competitor(7, 42, "https://www.booking.com", "Booking.com", "now")

    with pytest.raises(CompetitorIntelligenceUnavailable) as exc_info:
        run(service.analyze(competitor, ta_affiliated=True))
    assert str(exc_info.value) == (
        "Свежие источники по этому конкуренту найти не удалось, "
        "поэтому анализ основан на доступных устойчивых данных."
    )


def test_signal_fallback_never_attempted_without_a_repository():
    """Backward compatibility: existing callers that don't pass
    workspace_signal_repository (default None) get the old behaviour -
    unavailable, no fallback attempted, no crash."""
    service, _, _ = _service()
    service._fetcher = _always_failing_fetch
    competitor = Competitor(7, 42, "https://unreachable.example", "Unreachable", "now")

    with pytest.raises(CompetitorIntelligenceUnavailable):
        run(service.analyze(competitor, ta_affiliated=True))


def test_ta_affiliated_true_and_false_get_identical_signal_fallback_evidence():
    """ta_affiliated only ever changes travel_advantage_comparison (see the
    isolation-audit tests above) - the Radar-signal fallback must behave
    identically either way."""
    record = _signal_record(
        item_title="Booking.com launches AI trip planner",
        item_summary="New feature announced this week.",
        item_url="https://skift.com/booking-ai-planner",
    )
    competitor = Competitor(7, 42, "https://www.booking.com", "Booking.com", "now")

    service_true, _ = _service_with_signals([record])
    result_true = run(service_true.analyze(competitor, ta_affiliated=True))

    service_false, _ = _service_with_signals([record])
    result_false = run(service_false.analyze(competitor, ta_affiliated=False))

    assert result_true.data_origin == result_false.data_origin == DATA_ORIGIN_RADAR_SIGNAL
    # discovered_at is a real timestamp (two separate analyze() calls), so
    # compare everything else about the evidence instead of full equality.
    fields = lambda sources: [  # noqa: E731
        (s.title, s.final_url, s.freshness, s.origin, s.summary, s.key_facts)
        for s in sources
    ]
    assert fields(result_true.sources) == fields(result_false.sources)
    assert result_true.travel_advantage_comparison
    assert result_false.travel_advantage_comparison == ()


def _seed_radar_signal(
    radar_db: Path, *, source_id: str, item_title: str, item_summary: str,
    item_url: str, created_at: str,
) -> None:
    with sqlite3.connect(radar_db) as db:
        db.execute("""CREATE TABLE lead_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT, created_at TEXT,
            source_type TEXT, origin_type TEXT, source_name TEXT, source_url TEXT,
            item_url TEXT UNIQUE, item_title TEXT, item_summary TEXT, published_at TEXT,
            status TEXT DEFAULT 'new', ai_score REAL, ai_category TEXT, ai_reason TEXT,
            suggested_message TEXT, notes TEXT, llm_checked INTEGER DEFAULT 0,
            llm_checked_at TEXT, llm_signal_type TEXT, llm_score REAL,
            llm_relevance TEXT, llm_reason TEXT, llm_suggested_message TEXT
        )""")
        db.execute(
            "INSERT INTO lead_signals(source_id, source_name, created_at, source_type, "
            "origin_type, item_url, item_title, item_summary) "
            "VALUES (?, 'Skift', ?, 'rss', 'publisher_post', ?, ?, ?)",
            (source_id, created_at, item_url, item_title, item_summary),
        )
        db.commit()


def test_independent_workspace_never_receives_another_workspaces_radar_signal(
    tmp_path: Path,
):
    """End-to-end against the REAL WorkspaceSignalRepository (not the stub
    above): a signal synced only into workspace B's subscribed source must
    never reach workspace A's fallback, even though both saved the exact
    same competitor URL/label."""
    app_db, radar_db, workspace_a, _partners, catalog = _radar_setup(tmp_path)
    workspace_b = _radar_workspace(app_db, 200)
    source = run(catalog.add_source(workspace_b, "https://skift.com/feed", "monitoring")).source

    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    _seed_radar_signal(
        radar_db, source_id=source.id,
        item_title="Booking.com launches AI trip planner",
        item_summary="New feature announced this week.",
        item_url="https://skift.com/booking-ai-planner", created_at=recent,
    )

    signal_repo = WorkspaceSignalRepository(app_db, radar_db)
    run(signal_repo.init(None))
    run(signal_repo.sync_eligible())

    competitor_repo = CompetitorRepository(app_db)
    run(competitor_repo.init())
    competitor_a = run(competitor_repo.add_competitor(
        workspace_a, "https://www.booking.com", label="Booking.com",
    ))
    competitor_b = run(competitor_repo.add_competitor(
        workspace_b, "https://www.booking.com", label="Booking.com",
    ))

    service = CompetitorIntelligenceService(
        FakeLLMProvider(analysis=_analysis()),
        SimpleNamespace(retrieve=AsyncMock(return_value=_knowledge())),
        fetcher=_always_failing_fetch, workspace_signal_repository=signal_repo,
    )

    # workspace A never subscribed to the source the signal came through -
    # the same signal must not leak into its fallback (honest refusal).
    with pytest.raises(CompetitorIntelligenceUnavailable):
        run(service.analyze(competitor_a, ta_affiliated=True))

    # workspace B (the actual subscriber) gets the fallback.
    result_b = run(service.analyze(competitor_b, ta_affiliated=True))
    assert result_b.data_origin == DATA_ORIGIN_RADAR_SIGNAL
