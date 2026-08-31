"""Competitor Discovery Radar: turns public market signals into reviewable
CompetitorCandidate rows.

Deliberately reuses existing infrastructure end to end instead of a second
search/fetch/LLM/repository stack:
  - signal source #1: WorkspaceSignalRepository (already-synced Radar
    signals, scoped to the workspace's own active source subscriptions);
  - signal source #2: a small curated list of public travel-industry-news
    LISTING pages (_CURATED_MARKET_SOURCES below) - see "coverage problem"
    note further down for why source #1 alone is almost never enough;
  - page fetch (both sources): app.planner.fetch.fetch_public_source_sync,
    the same public fetcher CompetitorIntelligenceService already uses -
    no new fetcher, no new crawler, no RSS/XML parser added;
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

--- Coverage problem (why source #1 alone found nothing in production) ---
WorkspaceSignalRepository only PROJECTS rows that already exist in the
external "Travel Lead Radar" project's own leads.db (opened read-only, see
workspace_signal_repository.py) - this repo has zero collector code
(`IMPLEMENTED_COLLECTOR_PLATFORMS` in app/services/source_registry.py is an
explicitly empty frozenset). Registering a new source in THIS repo's
source_catalog does not make anything crawl it: the external project has to
independently start monitoring the same URL and write to lead_signals
before any local subscription can ever surface it. Today's actual seeded
catalog (config/sources.json) is almost entirely destination/deals content
(VK/Telegram travel-blog and booking-deal channels) plus two admin-owned
product pages - essentially none of it is industry-news/startup/OTA-launch
material, and even the few industry-flavored entries depend on the same
external crawler actually publishing matching lead_signals rows.

_CURATED_MARKET_SOURCES works around this without touching the external
project, a new crawler, a new scheduler, or a new DB: each entry is a public
listing page (verified reachable and text/html - RSS/Atom feeds are served
as application/rss+xml and fetch_public_source_sync deliberately only
accepts text/html or text/plain, so feed URLs are not usable here) fetched
on-demand by the SAME existing fetcher, then read through the SAME
analyze_source call already used everywhere else - just applied to a
listing page instead of a single competitor's site.
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
from app.domain.usage import UsageStatus
from app.planner.fetch import FetchedPublicSource, PublicSourceFetchError, fetch_public_source_sync
from app.repositories.competitor_repository import CompetitorRepository
from app.repositories.usage_ledger_repository import UsageLedgerRepository
from app.repositories.workspace_signal_repository import (
    WorkspaceSignalRecord,
    WorkspaceSignalRepository,
)
from app.services.llm.base import LLMProvider
from app.services.usage_recorder import record_llm_call

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

# Verified by hand (2026-08-31): each URL actually fetches as real,
# dated, headline-dense text/html via fetch_public_source_sync (not a
# paywall/bot-wall/nav-only shell) - see the task's report for samples.
# Priority coverage: travel industry news (TTG Media, Travel Weekly UK,
# Travel Market Report, Business Traveller, Travel And Tour World),
# travel-tech/AI in travel (Travolution Technology, Hotel Online),
# OTA/booking industry (Travolution), loyalty/travel commerce (The Points
# Guy). Startup launches/funding shows up inside Travolution Technology's
# own coverage (e.g. "TravelX ... closes $45M Series A") rather than a
# dedicated feed - no reliably fetchable dedicated startup-funding travel
# source was found (Crunchbase/FinSMEs/TechCrunch all blocked the fetcher).
_CURATED_MARKET_SOURCES: tuple[tuple[str, str], ...] = (
    ("Travolution", "https://www.travolution.com/"),
    ("Travolution — Technology", "https://www.travolution.com/travel-sectors/technology"),
    ("Hotel Online", "https://hotel-online.com/"),
    ("The Points Guy — News", "https://thepointsguy.com/news/"),
    ("The Points Guy — Airline", "https://thepointsguy.com/airline/"),
    ("TTG Media", "https://www.ttgmedia.com/news"),
    ("Travel And Tour World", "https://www.travelandtourworld.com/"),
    ("Travel Weekly UK", "https://www.travelweekly.co.uk/"),
    ("Business Traveller", "https://www.businesstraveller.com/"),
    ("Travel Market Report", "https://www.travelmarketreport.com/"),
)
_MAX_FACTS_PER_CURATED_SOURCE = 3

# Filters out generic sentence-leading capitalized words that are not brand
# names, so a headline like "New AI Travel Planner Launches in Asia" does
# not get slugged as "new".
_GENERIC_LEADING_WORDS = frozenset({
    "the", "a", "an", "new", "top", "best", "how", "why", "what", "global",
    "industry", "travel", "hotel", "hotels", "airline", "airlines", "world",
    "this", "these", "major", "leading",
})

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
        usage_ledger_repository: UsageLedgerRepository | None = None,
    ) -> None:
        self._signals = workspace_signal_repository
        self._competitors = competitor_repository
        self._provider = llm_provider
        # Resolved at call time (not a bound default value) so tests can
        # patch app.services.competitor_discovery.fetch_public_source_sync
        # without needing to thread a fake through every caller/handler.
        self._fetcher = fetcher or fetch_public_source_sync
        self._usage_ledger = usage_ledger_repository

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

        built.extend(await self._scan_curated_sources(workspace_id, known_domains, seen_domains))

        built.sort(key=_rank_key)
        return tuple(built[:_MAX_CANDIDATES_RETURNED])

    async def _scan_curated_sources(
        self, workspace_id: int, known_domains: set[str], seen_domains: set[str],
    ) -> list[CompetitorCandidate]:
        """Fixes the coverage gap: WorkspaceSignalRepository alone almost
        never has industry-news/startup/OTA material (see module docstring),
        so this reads a small curated list of real, fetchable travel-
        industry listing pages through the exact same fetch+analyze pipeline
        as _evaluate_signal above, just with the LLM's key_facts standing in
        for individual market items instead of one signal = one item.

        upsert_candidate() derives canonical_domain from discovered_url
        internally (repository is out of scope to change here) - so a fact
        that names a real, independently verifiable company becomes its own
        candidate keyed by THAT company's own domain (no collision risk,
        each is genuinely distinct). A fact that only names something we
        cannot verify a standalone site for is never a competitor-candidate
        - see _select_unverified_signal below."""
        built: list[CompetitorCandidate] = []
        for source_name, source_url in _CURATED_MARKET_SOURCES:
            try:
                page = await asyncio.to_thread(self._fetcher, source_url)
            except PublicSourceFetchError:
                continue
            analysis = await asyncio.to_thread(
                self._provider.analyze_source, source_text=page.text[:6_000],
            )
            await record_llm_call(
                self._usage_ledger, workspace_id=workspace_id, telegram_user_id=None,
                module="competitor_discovery", provider=self._provider.name,
                usage=analysis.usage if analysis is not None else None,
                status=UsageStatus.SUCCESS if analysis is not None else UsageStatus.FAILURE,
            )
            if analysis is None:
                continue

            unverified_candidates: list[tuple[tuple[int, int], str, list[str]]] = []
            for fact in analysis.key_facts[:_MAX_FACTS_PER_CURATED_SOURCE]:
                haystack = fact.lower()
                classification = _classify(haystack)
                if classification is None:
                    continue
                matched = _matched_markers(haystack, classification)
                slugs = (
                    _brand_slug_candidates(fact)
                    if classification is not CandidateClassification.MARKET_SIGNAL else ()
                )

                # A headline's FIRST capitalized word is often the already-
                # known subject ("Ryanair approves Fliggy as OTA partner"),
                # not the newer/more interesting entity later in the
                # sentence ("Fliggy") - try each candidate in order rather
                # than only the first, so a later entity that DOES verify
                # is not missed just because an earlier one didn't.
                verified_url: str | None = None
                slug: str | None = None
                for candidate_slug in slugs:
                    try:
                        verified = await asyncio.to_thread(
                            self._fetcher, f"https://{candidate_slug}.com",
                        )
                    except PublicSourceFetchError:
                        continue
                    # A successful fetch alone is not enough: Title Case
                    # headlines capitalize ordinary words too ("...Empower
                    # Independent Hotels...") and some of those happen to be
                    # real, unrelated companies' domains (empower.com is a
                    # real fintech site, not a travel one). Require the
                    # verified page to actually look travel-relevant itself
                    # before trusting it as a competitor's own site.
                    if not _looks_travel_relevant(f"{verified.title} {verified.text[:2000]}"):
                        continue
                    verified_url = verified.final_url
                    slug = candidate_slug
                    break

                if verified_url is not None:
                    domain = canonical_domain(verified_url)
                    if not domain or domain in _EXCLUDED_DOMAINS:
                        continue
                    if domain in known_domains or domain in seen_domains:
                        continue
                    confidence = (
                        CandidateConfidence.HIGH if len(matched) >= 2 else CandidateConfidence.MEDIUM
                    )
                    candidate = await self._competitors.upsert_candidate(
                        workspace_id, name=slug.capitalize(), discovered_url=verified_url,
                        source_title=f"{source_name}: {fact}"[:200], source_url=source_url,
                        description=fact,
                        evidence=(
                            f'Сигнал упоминает: «{matched[0]}»', f'Источник: {source_name}',
                        ),
                        confidence=confidence, classification=classification,
                        why_it_matters=_WHY_IT_MATTERS_TEMPLATES[classification].format(
                            evidence=matched[0],
                        ),
                    )
                    built.append(candidate)
                    seen_domains.add(candidate.canonical_domain)
                else:
                    priority = (_CLASSIFICATION_PRIORITY[classification], -len(matched))
                    unverified_candidates.append((priority, fact, matched))

            # At most ONE market-signal candidate per curated page (real
            # domain = the page itself, honest provenance) - keeps every
            # fact this source couldn't independently verify from colliding
            # on the same canonical_domain via the repository's upsert.
            fallback = _select_unverified_signal(unverified_candidates)
            if fallback is not None:
                fact, matched = fallback
                domain = canonical_domain(source_url)
                if domain and domain not in _EXCLUDED_DOMAINS and \
                        domain not in known_domains and domain not in seen_domains:
                    candidate = await self._competitors.upsert_candidate(
                        workspace_id, name=source_name, discovered_url=source_url,
                        source_title=f"{source_name}: {fact}"[:200], source_url=source_url,
                        description=fact,
                        evidence=(f'Сигнал упоминает: «{matched[0]}»',),
                        confidence=CandidateConfidence.MEDIUM,
                        classification=CandidateClassification.MARKET_SIGNAL,
                        why_it_matters=_WHY_IT_MATTERS_TEMPLATES[
                            CandidateClassification.MARKET_SIGNAL
                        ].format(evidence=matched[0]),
                    )
                    built.append(candidate)
                    seen_domains.add(candidate.canonical_domain)
        return built

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
            await record_llm_call(
                self._usage_ledger, workspace_id=workspace_id, telegram_user_id=None,
                module="competitor_discovery", provider=self._provider.name,
                usage=analysis.usage if analysis is not None else None,
                status=UsageStatus.SUCCESS if analysis is not None else UsageStatus.FAILURE,
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


_TRAVEL_RELEVANCE_WORDS = (
    "travel", "trip", "hotel", "flight", "book", "tour", "vacation",
    "airline", "cruise", "destination", "itinerary", "resort", "vacat",
    "путешеств", "тур", "отел", "авиа", "брониров", "поездк",
)


def _looks_travel_relevant(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in _TRAVEL_RELEVANCE_WORDS)


_MAX_BRAND_SLUG_CANDIDATES = 3


def _brand_slug_candidates(text: str) -> tuple[str, ...]:
    """Capitalized, non-generic words in a headline-like sentence, in
    order of appearance - a cheap stand-in for named-entity recognition (no
    ML classifier per spec). Each is only a domain-verification GUESS: a
    candidate is trusted as a real company site only if
    fetch_public_source_sync actually reaches https://{slug}.com - see
    _scan_curated_sources. Multiple candidates (not just the first word)
    matter because a headline's leading word is often the already-known
    subject ("Ryanair approves Fliggy..."), not the newer entity the
    sentence is actually about ("Fliggy")."""
    seen: list[str] = []
    for word in text.split():
        core = word.strip(".,;:!?()\"'").replace("’s", "").replace("'s", "")
        if core and core.isalpha() and len(core) > 2 and core[0].isupper():
            lowered = core.lower()
            if lowered not in _GENERIC_LEADING_WORDS and lowered not in seen:
                seen.append(lowered)
                if len(seen) >= _MAX_BRAND_SLUG_CANDIDATES:
                    break
    return tuple(seen)


def _select_unverified_signal(
    candidates: list[tuple[tuple[int, int], str, list[str]]],
) -> tuple[str, list[str]] | None:
    """Strongest fact among those a curated page offered but could not be
    independently verified as a real company site - lowest priority tuple
    (best classification, then most matched keywords) wins."""
    if not candidates:
        return None
    _, fact, matched = min(candidates, key=lambda item: item[0])
    return fact, matched
