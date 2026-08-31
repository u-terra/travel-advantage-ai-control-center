from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

from app.domain.competitor_intelligence import (
    CompetitorIntelligence,
    CompetitorSourceEvidence,
    ContentOpportunity,
)
from app.domain.competitors import Competitor
from app.planner.fetch import FetchedPublicSource, PublicSourceFetchError, fetch_public_source_sync
from app.services.knowledge_service import KnowledgeService
from app.services.llm.base import LLMProvider
from app.services.llm.models import SourceAnalysisPayload

_MAX_SOURCES = 5
_MAX_OPPORTUNITIES = 8
_OPPORTUNITY_CATEGORIES = (
    ("travel trends", ("trend", "traveler", "traveller", "tourism", "booking data")),
    ("направления", ("destination", "city", "country", "disneyland", "legoland", "resort")),
    ("практический travel guide", ("guide", "visa", "airport", "transit", "itinerary", "tips", "passport", "how to")),
    ("AI и технологии в travel", (" ai ", "chatgpt", "technology", "digital", "biometric", "esim", "app")),
    ("loyalty и promotions", ("loyal", "member", "reward", "coin", "promo", "discount", "deal", "coupon", "sale")),
    ("customer UX", ("support", "payment", "cancel", "refund", "search", "booking", "flexib")),
    ("новый продукт или сервис", ("launch", "new product", "new service", "new feature", "introduc")),
    ("изменение спроса", ("demand", "surge", "growth", "increase", "decrease", "year-on-year")),
)
_DEDUP_STOP_WORDS = frozenset({
    "the", "and", "for", "with", "from", "this", "that", "trip", "com",
    "как", "что", "для", "или", "это", "при", "про",
})
_DATE_RE = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+20\d{2}\b",
    re.IGNORECASE,
)


class CompetitorIntelligenceUnavailable(RuntimeError):
    pass


class CompetitorIntelligenceService:
    """Bounded public-source analysis for any saved competitor.

    Conventional discovery paths are generic hints, never competitor identity:
    localized/final URLs remain provenance attached to the one persisted id.
    """

    def __init__(
        self,
        provider: LLMProvider,
        knowledge_service: KnowledgeService,
        *,
        fetcher: Callable[[str], FetchedPublicSource] = fetch_public_source_sync,
    ) -> None:
        self._provider = provider
        self._knowledge = knowledge_service
        self._fetcher = fetcher

    async def analyze(self, competitor: Competitor) -> CompetitorIntelligence:
        discovered_at = datetime.now(timezone.utc).isoformat()
        fetched: list[FetchedPublicSource] = []
        for url in _candidate_urls(competitor.url):
            try:
                source = await asyncio.to_thread(self._fetcher, url)
            except PublicSourceFetchError:
                continue
            if source.final_url not in {item.final_url for item in fetched}:
                fetched.append(source)
            if len(fetched) == _MAX_SOURCES:
                break
        if not fetched:
            raise CompetitorIntelligenceUnavailable("Не удалось прочитать публичные источники.")

        evidence: list[CompetitorSourceEvidence] = []
        analyses = []
        for source in fetched:
            analysis = await asyncio.to_thread(
                self._provider.analyze_source, source_text=source.text[:6_000],
            )
            if analysis is None:
                analysis = _fallback_analysis(source)
            analyses.append((source, analysis))
            evidence.append(CompetitorSourceEvidence(
                title=source.title or source.final_url,
                url=source.url,
                final_url=source.final_url,
                discovered_at=discovered_at,
                freshness=_freshness(source.text),
                summary=analysis.summary,
                key_facts=analysis.key_facts,
            ))
        if not analyses:
            raise CompetitorIntelligenceUnavailable("Источники прочитаны, но анализ недоступен.")

        bundle = await self._knowledge.retrieve("Что такое Travel Advantage и какие услуги доступны")
        ta_facts = tuple(
            f"Travel Advantage — {item.content} [источник: {item.source_ref}]"
            for item in bundle.primary_items[:3]
        )
        ta_link = ta_facts[0] if ta_facts else None
        opportunities = _opportunities(competitor.id, analyses, ta_link)
        all_facts = tuple(fact for _, a in analyses for fact in a.key_facts)
        summaries = tuple(a.summary for _, a in analyses)

        return CompetitorIntelligence(
            competitor_id=competitor.id,
            competitor_label=competitor.label,
            analyzed_at=discovered_at,
            positioning=summaries[:2],
            products=_matching(all_facts, "hotel", "flight", "train", "car", "cruise", "tour", "booking"),
            destinations_and_categories=_matching(all_facts, "destination", "city", "country", "travel", "hotel", "flight"),
            promotions=_matching(all_facts, "deal", "discount", "promo", "coupon", "sale", "offer"),
            loyalty_mechanics=_matching(all_facts, "member", "loyal", "coin", "reward", "tier", "perk"),
            service_and_ux=_matching(all_facts, "app", "service", "support", "search", "flex", "ai", "booking"),
            strengths=tuple(dict.fromkeys((*summaries, *all_facts)))[:5],
            travel_advantage_comparison=ta_facts,
            fresh_signals=tuple(
                f"{source.title}: {analysis.summary}"
                for source, analysis in analyses if _freshness(source.text)
            )[:5],
            sources=tuple(evidence),
            opportunities=opportunities,
        )


def _candidate_urls(url: str) -> tuple[str, ...]:
    parts = urlsplit(url.strip())
    labels = (parts.hostname or "").split(".")
    if len(labels) >= 3 and (labels[0] == "www" or len(labels[0]) <= 3):
        root_host = ".".join(labels[1:])
    else:
        root_host = parts.hostname or ""
    origin = urlunsplit((parts.scheme or "https", f"www.{root_host}", "", "", ""))
    candidates = (
        url.strip(),
        origin + "/blog",
        origin + "/guide/all-content/",
        origin + "/newsroom/",
        origin + "/customer/loyalty",
    )
    return tuple(dict.fromkeys(candidates))


def _freshness(text: str) -> str | None:
    match = _DATE_RE.search(text)
    return match.group(0) if match else None


def _fallback_analysis(source: FetchedPublicSource) -> SourceAnalysisPayload:
    lines = tuple(dict.fromkeys(
        line.strip() for line in source.text.splitlines()
        if 25 <= len(line.strip()) <= 240
    ))
    facts = lines[:8] or (source.text[:240],)
    angles = _fallback_angles(source)
    return SourceAnalysisPayload(
        summary=(
            f"Публичная страница «{source.title or source.final_url}» содержит "
            "актуальные продукты, направления и темы конкурента."
        ),
        key_facts=facts,
        disputed_claims=(),
        audience_value=(
            "Источник показывает, какие travel-задачи и информационные поводы "
            "конкурент считает важными для путешественников."
        ),
        target_audiences=("путешественники",),
        content_angles=angles,
        recommended_formats=("post",),
        warnings=("Автоматический LLM-разбор недоступен; использована bounded текстовая проекция.",),
    )


def _fallback_angles(source: FetchedPublicSource) -> tuple[str, ...]:
    lower = f"{source.title}\n{source.text}".lower()
    rules = (
        (("promo", "discount", "deal", "coupon", "sale"), "Как находить и проверять актуальные travel-акции"),
        (("guide", "destination", "travel", "city"), "Сезонные направления и практические советы путешественникам"),
        (("visa", "airport", "transit", "entry"), "Что проверить до поездки: документы, аэропорты и транзит"),
        (("member", "loyal", "reward", "coin", "tier"), "Как loyalty-механики влияют на выбор travel-сервиса"),
        (("app", " ai ", "search", "booking"), "Какие UX-функции упрощают планирование и бронирование"),
        (("news", "data", "growth", "trend"), "Новые сигналы в поведении путешественников"),
    )
    selected = [title for words, title in rules if any(word in lower for word in words)]
    if not selected:
        selected.append("Что этот публичный материал говорит о запросах путешественников")
    return tuple(selected[:5])


def _matching(facts: tuple[str, ...], *keywords: str) -> tuple[str, ...]:
    selected = [fact for fact in facts if any(word in fact.lower() for word in keywords)]
    return tuple(dict.fromkeys(selected))[:6]


def _opportunities(competitor_id: int, analyses, ta_link: str | None) -> tuple[ContentOpportunity, ...]:
    ranked: list[tuple[int, int, FetchedPublicSource, SourceAnalysisPayload, str, str]] = []
    sequence = 0
    for source, analysis in analyses:
        theses = analysis.key_facts or (analysis.summary,)
        for thesis in theses:
            category = _opportunity_category(thesis)
            if category is None:
                continue
            category_index, category_name = category
            ranked.append((category_index, sequence, source, analysis, thesis, category_name))
            sequence += 1
    ranked.sort(key=lambda item: (item[0], item[1]))

    result: list[ContentOpportunity] = []
    fingerprints: list[frozenset[str]] = []
    for _, _, source, analysis, thesis, category_name in ranked:
        fingerprint = _semantic_fingerprint(thesis)
        if not fingerprint or any(_semantic_duplicate(fingerprint, seen) for seen in fingerprints):
            continue
        fingerprints.append(fingerprint)
        clean_thesis = " ".join(thesis.split())
        angle = (
            f"{category_name}: что этот конкретный сигнал меняет для путешественника — "
            f"{clean_thesis}"
        )
        result.append(ContentOpportunity(
            id=f"opp-{len(result) + 1}", competitor_id=competitor_id,
            topic=clean_thesis, source_title=source.title or source.final_url,
            source_url=source.final_url, freshness=_freshness(source.text),
            key_thesis=clean_thesis, audience_value=analysis.audience_value,
            own_post_angle=angle, travel_advantage_link=ta_link,
        ))
        if len(result) == _MAX_OPPORTUNITIES:
            break
    return tuple(result)


def _opportunity_category(thesis: str) -> tuple[int, str] | None:
    normalized = f" {thesis.lower()} "
    for index, (category, markers) in enumerate(_OPPORTUNITY_CATEGORIES):
        if any(marker in normalized for marker in markers):
            return index, category
    return None


def _semantic_fingerprint(value: str) -> frozenset[str]:
    tokens = re.findall(r"[0-9a-zа-яё]+", value.lower())
    return frozenset(token for token in tokens if len(token) > 2 and token not in _DEDUP_STOP_WORDS)


def _semantic_duplicate(first: frozenset[str], second: frozenset[str]) -> bool:
    if first == second:
        return True
    union = first | second
    return bool(union) and len(first & second) / len(union) >= 0.60
