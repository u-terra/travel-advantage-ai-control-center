"""Competitor Discovery Radar: turns public market signals the workspace has
already synced (Lead Radar / WorkspaceSignalRepository) into reviewable
CompetitorCandidate rows.

Deliberately reuses existing infrastructure end to end instead of a second
search/fetch/LLM/repository stack:
  - signal source: WorkspaceSignalRepository (already-synced Radar signals,
    scoped to the workspace's own active source subscriptions) - no new
    search provider, no fake web search;
  - page fetch: app.planner.fetch.fetch_public_source_sync, the same public
    fetcher CompetitorIntelligenceService already uses;
  - page understanding: LLMProvider.analyze_source, the same LLM source
    analysis CompetitorIntelligenceService already uses;
  - storage: CompetitorRepository (same repository/table family as
    competitors, just a second table - see app/repositories/
    competitor_repository.py's competitor_candidates schema).

Classification is a plain keyword-marker scan (three classes, priority
order), not an LLM/ML classifier, and is generic travel-platform language -
not hardcoded to Travel Advantage or any one business. Travel Advantage's
verified Knowledge Base is intentionally never consulted here; that stays
scoped to CompetitorIntelligenceService's own travel_advantage_link field.
"""

from __future__ import annotations

import asyncio
from typing import Callable

from app.domain.competitor_discovery import (
    CandidateClassification,
    CandidateConfidence,
    CompetitorCandidate,
    canonical_domain,
)
from app.planner.fetch import FetchedPublicSource, PublicSourceFetchError, fetch_public_source_sync
from app.repositories.competitor_repository import CompetitorRepository
from app.repositories.workspace_signal_repository import (
    WorkspaceSignalRecord,
    WorkspaceSignalRepository,
)
from app.services.llm.base import LLMProvider

_MAX_SIGNALS_SCANNED = 30
_MAX_CANDIDATES_RETURNED = 5

# General travel-platform relevance language, not tied to any one business -
# checked in priority order (first class whose markers match wins), the same
# pattern app/services/competitor_intelligence.py already uses for content
# categories (_OPPORTUNITY_CATEGORIES).
_CLASSIFICATION_MARKERS: tuple[tuple[CandidateClassification, tuple[str, ...]], ...] = (
    (CandidateClassification.DIRECT_COMPETITOR, (
        "booking platform", "travel platform", "hotel booking", "flight booking",
        "vacation rental platform", "travel membership", "travel club",
        "online travel agency", " ota ", "book your trip", "book your stay",
        "бронирован", "трэвел-клуб", "трэвел клуб", "клуб путешестви",
    )),
    (CandidateClassification.POTENTIAL_COMPETITOR, (
        "ai travel planner", "ai booking assistant", "ai trip planning",
        "travel super-app", "super app for travel", "travel chatbot",
        "ai travel assistant", "trip planning app", "booking assistant",
    )),
    (CandidateClassification.MARKET_SIGNAL, (
        "travel startup", "travel tech", "loyalty program", "loyalty platform",
        "travel deals", "new travel app", "travel trend", "traveler behavior",
        "consumer travel", "raises funding", "travel app launch",
    )),
)

# Generic infrastructure/social/publisher domains that show up as link
# mentions inside travel articles but are never themselves a travel
# competitor - excluded so they never become noise candidates.
_EXCLUDED_DOMAINS = frozenset({
    "google.com", "facebook.com", "youtube.com", "wikipedia.org", "twitter.com",
    "x.com", "instagram.com", "medium.com", "linkedin.com", "tiktok.com",
    "reddit.com", "amazon.com", "apple.com", "microsoft.com", "github.com",
    "t.me", "telegram.org", "vk.com",
})

_WHY_IT_MATTERS_TEMPLATES: dict[CandidateClassification, str] = {
    CandidateClassification.DIRECT_COMPETITOR: (
        "Прямой конкурент: борется примерно за того же путешественника и "
        "похожий travel journey (бронирование / OTA / travel-membership). "
        "Сигнал: «{evidence}». Важно для независимых турагентов, агентств и "
        "партнёров Travel Advantage — это тот же покупатель и тот же момент "
        "выбора сервиса."
    ),
    CandidateClassification.POTENTIAL_COMPETITOR: (
        "Потенциальный конкурент: технология или сервис, который может "
        "забрать часть customer journey (AI-планирование, бронирование, "
        "super-app). Сигнал: «{evidence}». Стоит следить — такие сервисы "
        "быстро меняют ожидания путешественника от привычного бронирования."
    ),
    CandidateClassification.MARKET_SIGNAL: (
        "Рыночный сигнал: не обязательно конкурент, но заметная тенденция "
        "рынка (loyalty-механика, новый канал продаж, изменение поведения "
        "клиента). Сигнал: «{evidence}». Полезно для контента и для "
        "понимания, куда сейчас движется спрос travel-аудитории."
    ),
}

_CLASSIFICATION_PRIORITY = {
    CandidateClassification.DIRECT_COMPETITOR: 0,
    CandidateClassification.POTENTIAL_COMPETITOR: 1,
    CandidateClassification.MARKET_SIGNAL: 2,
}
_CONFIDENCE_PRIORITY = {CandidateConfidence.HIGH: 0, CandidateConfidence.MEDIUM: 1}


class CompetitorDiscoveryService:
    def __init__(
        self,
        workspace_signal_repository: WorkspaceSignalRepository,
        competitor_repository: CompetitorRepository,
        llm_provider: LLMProvider,
        *,
        fetcher: Callable[[str], FetchedPublicSource] | None = None,
    ) -> None:
        self._signals = workspace_signal_repository
        self._competitors = competitor_repository
        self._provider = llm_provider
        # Resolved at call time (not a bound default value) so tests can
        # patch app.services.competitor_discovery.fetch_public_source_sync
        # without needing to thread a fake through every caller/handler.
        self._fetcher = fetcher or fetch_public_source_sync

    async def discover(
        self, workspace_id: int, *, own_domain: str | None = None,
    ) -> tuple[CompetitorCandidate, ...]:
        """Scans the workspace's already-synced Radar signals for unknown
        travel-relevant candidates, upserts every survivor (so repeated runs
        accumulate evidence/last_seen instead of duplicating), and returns
        the top few ranked by classification + confidence - quality over
        quantity, not a list of every domain seen."""
        records = await self._signals.list_for_workspace(
            workspace_id, limit=_MAX_SIGNALS_SCANNED,
        )
        known_domains = await self._competitors.known_domains_for_workspace(workspace_id)
        if own_domain:
            known_domains = known_domains | {canonical_domain(own_domain)}

        built: list[CompetitorCandidate] = []
        seen_domains: set[str] = set()
        for record in records:
            candidate = await self._evaluate_signal(
                workspace_id, record, known_domains, seen_domains,
            )
            if candidate is not None:
                built.append(candidate)
                seen_domains.add(candidate.canonical_domain)

        built.sort(key=_rank_key)
        return tuple(built[:_MAX_CANDIDATES_RETURNED])

    async def _evaluate_signal(
        self, workspace_id: int, record: WorkspaceSignalRecord,
        known_domains: set[str], seen_domains: set[str],
    ) -> CompetitorCandidate | None:
        url = (record.item_url or "").strip()
        if not url.startswith(("http://", "https://")):
            return None
        domain = canonical_domain(url)
        if not domain or domain in _EXCLUDED_DOMAINS:
            return None
        if domain in known_domains or domain in seen_domains:
            return None

        haystack = f"{record.item_title} {record.item_summary} {record.source_name}".lower()
        classification = _classify(haystack)
        if classification is None:
            return None
        matched = _matched_markers(haystack, classification)

        description = record.item_summary or record.item_title
        evidence: list[str] = [f'Сигнал упоминает: «{matched[0]}»']

        fetched_ok = False
        try:
            source = await asyncio.to_thread(self._fetcher, url)
        except PublicSourceFetchError:
            source = None
        if source is not None:
            analysis = await asyncio.to_thread(
                self._provider.analyze_source, source_text=source.text[:6_000],
            )
            if analysis is not None:
                description = analysis.summary or description
                evidence.extend(analysis.key_facts[:2])
                fetched_ok = True

        confidence = (
            CandidateConfidence.HIGH
            if len(matched) >= 2 or fetched_ok
            else CandidateConfidence.MEDIUM
        )
        name = _brand_name(record.source_name, domain)
        why_it_matters = _WHY_IT_MATTERS_TEMPLATES[classification].format(evidence=matched[0])

        return await self._competitors.upsert_candidate(
            workspace_id, name=name, discovered_url=url,
            source_title=record.item_title or record.source_name,
            source_url=url, description=description, evidence=tuple(evidence),
            confidence=confidence, classification=classification,
            why_it_matters=why_it_matters,
        )


def _classify(haystack: str) -> CandidateClassification | None:
    for classification, markers in _CLASSIFICATION_MARKERS:
        if any(marker in haystack for marker in markers):
            return classification
    return None


def _matched_markers(haystack: str, classification: CandidateClassification) -> list[str]:
    markers = next(m for c, m in _CLASSIFICATION_MARKERS if c is classification)
    return [marker.strip() for marker in markers if marker in haystack]


def _brand_name(source_name: str, domain: str) -> str:
    if source_name and source_name.strip():
        return source_name.strip()
    return domain.split(".")[0].capitalize()


def _rank_key(candidate: CompetitorCandidate) -> tuple[int, int]:
    return (
        _CLASSIFICATION_PRIORITY[candidate.classification],
        _CONFIDENCE_PRIORITY[candidate.confidence],
    )
