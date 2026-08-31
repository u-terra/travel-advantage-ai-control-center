from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.domain.competitor_intelligence import CompetitorIntelligence
from app.domain.competitors import Competitor
from app.handlers.competitors import create_from_competitor_opportunity
from app.keyboards import COMPETITOR_OPEN_PREFIX, competitors_list_keyboard
from app.planner.fetch import FetchedPublicSource, PublicSourceFetchError
from app.repositories.competitor_repository import CompetitorRepository
from app.repositories.partner_repository import PartnerRepository
from app.services.competitor_intelligence import (
    CompetitorIntelligenceService,
    _opportunities,
)
from app.services.knowledge_service import KnowledgeBundle
from app.services.llm.models import ContentDraft, SourceAnalysisPayload
from tests.llm_fakes import FakeLLMProvider
from tests.test_journal_handlers import (
    Callback, business_profile, context, profile_repository, user_preferences,
)


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

    result = run(service.analyze(competitor))

    assert result.competitor_id == 7
    assert len(result.sources) == 4
    assert result.sources[0].url == "https://nl.trip.com/?locale=nl-nl"
    assert all(source.discovered_at for source in result.sources)
    assert len(result.opportunities) == 3
    assert all(item.competitor_id == 7 and item.source_url for item in result.opportunities)
    assert all(item.travel_advantage_link and "verified TA knowledge" in item.travel_advantage_link for item in result.opportunities)
    assert provider.analyze_source.call_count == 4
    assert "https://www.trip.com/blog" in calls


def test_existing_competitor_list_opens_saved_entity():
    keyboard = competitors_list_keyboard(((7, "Trip.com"),))
    button = keyboard.inline_keyboard[0][0]
    assert button.text == "🎯 Trip.com"
    assert button.callback_data == f"{COMPETITOR_OPEN_PREFIX}7"


def test_provider_failure_keeps_real_sources_and_content_opportunities():
    service, _, _ = _service(analysis=None)
    competitor = Competitor(7, 42, "https://nl.trip.com/?locale=nl-nl", "Trip.com", "now")
    result = run(service.analyze(competitor))
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
    intelligence: CompetitorIntelligence = run(service.analyze(competitor))
    run(repository.save_intelligence(workspace.id, intelligence))
    restored = run(repository.get_intelligence(workspace.id, competitor.id))
    assert restored is not None and len(restored.opportunities) == 3

    callback = Callback()
    callback.data = f"competitor:create:{competitor.id}:{restored.opportunities[0].id}"
    provider = FakeLLMProvider(draft=ContentDraft("Оригинальный материал", ()))
    run(create_from_competitor_opportunity(
        callback, repository, context(workspace.id), provider,
        profile_repository(business_profile(workspace.id)),
    ))
    provider.generate_draft.assert_called_once()
    source_text = provider.generate_draft.call_args.kwargs["source_text"]
    assert restored.opportunities[0].source_url in source_text
    assert "не рекламируй" in source_text
    assert "проверяйте срок акции, условия, направление и даты" in source_text
    assert "Оригинальный материал" in callback.message.answers[-1][0]


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
    intelligence = run(service.analyze(competitor))
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
    ))
    source_text = provider.generate_draft.call_args.kwargs["source_text"]
    assert "[PERSONAL STYLE - DATA]" in source_text
    assert "Пишу с юмором" in source_text
    assert "Пример поста" in source_text
    assert "лучший тур" in source_text


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
