"""Partner Discovery v0: finds up to 5 PUBLIC travel-related subjects
(agents, tour organizers, guides, content creators, travel clubs/projects)
a club_partner workspace might want to look at - never a claim that any of
them wants a partnership, extra income, or Travel Advantage (see
app.domain.prospects.ProspectCandidate's own docstring).

Reuses exactly three already-existing primitives, no new provider/API:
- WebSearchProvider.search() (app.services.web_search.base) - the same
  Yandex-backed search this codebase already runs in production for
  knowledge-chat web search and per-source article discovery
  (app.services.web_source_discovery);
- fetch_public_source_sync() (app.planner.fetch) - the same SSRF-hardened
  public-page fetch app.services.web_signal_collector and
  app.services.competitor_discovery already use;
- app.domain.competitor_discovery.canonical_domain() for dedup, the same
  root-host normalization an existing discovery flow already relies on.

v0 is entirely stateless: one call in, a bounded list of candidates out,
nothing written anywhere. No DB, no migration, no persistence - see the
task's own explicit v0 scope.

Pipeline (bounded cost - see the module constants below):
    fixed queries (<=5) --search--> raw results --dedupe--> candidates
        --noise filter--> survivors --rank--> top N --page-verify-->
        final top 5, each carrying its own query/url/evidence-source
        provenance.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from urllib.parse import urlsplit

from app.domain.competitor_discovery import canonical_domain
from app.domain.prospects import EvidenceSource, ProspectCandidate, ProspectFit, ProspectType
from app.planner.fetch import PublicSourceFetchError, fetch_public_source_sync
from app.services.web_search.base import SearchResult, WebSearchProvider

log = logging.getLogger(__name__)

# ── Bounded cost knobs ────────────────────────────────────────────────────
# Tenant-aware query rotation (2026-10-07): one hand-picked variant pool per
# category (same 5 categories as _TYPE_MARKERS below, same order), so the
# scoring/dedupe/noise-filter pipeline downstream never changes - only
# WHICH synonymous phrasing is sent per category differs per workspace.
# Index 0 of every tuple is the ORIGINAL, already-live-probe-confirmed
# query (see the Partner Discovery v0 experiment write-up) - this is what
# DISCOVERY_QUERIES (below) and every caller that passes no rotation_seed
# still gets, unchanged.
_DISCOVERY_QUERY_VARIANTS: tuple[tuple[str, ...], ...] = (
    (  # travel_agent
        "турагент авторские туры",
        "туроператор индивидуальные туры",
        "турагентство авторские маршруты",
    ),
    (  # tour_organizer
        "организатор путешествий группа",
        "организатор групповых туров",
        "команда организует путешествия",
    ),
    (  # guide
        "гид экскурсии путешествия",
        "экскурсовод авторские экскурсии",
        "частный гид путешествия",
    ),
    (  # travel_creator
        "travel блог автор путешествий",
        "трэвел-блогер путешествия",
        "блог путешественника авторский",
    ),
    (  # travel_club
        "клуб путешественников сообщество",
        "сообщество путешественников блог",
        "travel клуб единомышленников",
    ),
)

# Fixed, hand-picked query list - the default/no-rotation-seed case. Exactly
# index 0 of every category above, unchanged from before this feature
# existed. Deliberately NOT generated/expanded at runtime beyond the one
# variant pick below: one discovery run is always exactly 5 search calls,
# never a growing/paginated crawl.
DISCOVERY_QUERIES: tuple[str, ...] = tuple(variants[0] for variants in _DISCOVERY_QUERY_VARIANTS)


def _select_all_variant_queries(rotation_seed: int | None) -> tuple[str, ...]:
    """Partner Discovery 2.0 (use_all_variants=True): every hand-picked
    variant for every category, instead of exactly one per category - more
    specialized queries feeding the same downstream dedupe/noise-filter/
    rank/verify pipeline unchanged. Still bounded (3 variants x 5
    categories = 15 queries max), never a growing/paginated crawl."""
    del rotation_seed  # all variants are used regardless of rotation seed
    queries: list[str] = []
    for variants in _DISCOVERY_QUERY_VARIANTS:
        queries.extend(variants)
    return tuple(queries)


def _select_discovery_queries(rotation_seed: int | None) -> tuple[str, ...]:
    """rotation_seed=None (no workspace context available) -> the original
    fixed DISCOVERY_QUERIES, byte-for-byte - existing callers/tests that
    never pass a seed see zero behavior change. Otherwise: one variant per
    category, chosen deterministically from (rotation_seed, category index)
    - the SAME workspace always gets the SAME 5 queries (stable, not
    re-rolled per click), while different workspace_ids are very likely to
    land on different combinations (there are 3^5 = 243 possible
    combinations). Never more than one query per category, so exactly 5
    queries total, same as before - no increase in Yandex calls per run."""
    if rotation_seed is None:
        return DISCOVERY_QUERIES
    selected: list[str] = []
    for category_index, variants in enumerate(_DISCOVERY_QUERY_VARIANTS):
        digest = hashlib.sha256(f"{rotation_seed}:{category_index}".encode("utf-8")).hexdigest()
        variant_index = int(digest, 16) % len(variants)
        selected.append(variants[variant_index])
    return tuple(selected)

_RESULTS_PER_QUERY = 5
# Page-verification (one fetch_public_source_sync call each) only ever runs
# on the best-ranked survivors, bounded independently of how many raw
# results/duplicates came back across all queries - this is what keeps one
# discovery run's cost flat regardless of query/result volume.
_MAX_PAGE_VERIFY_CANDIDATES = 10
# A transient-failure retry of one query is only started when at least this
# many seconds of the search budget remain (a Yandex call can take up to its
# 5 s timeout).
_RETRY_MIN_REMAINING_S = 10.0
_MAX_RESULTS = 5
_EVIDENCE_EXCERPT_CHARS = 400


# ── Noise filter (narrow, reviewable denylist - same style as this
# codebase's existing _IRRELEVANT_FINANCE_MARKERS/_AD_CONTACT_MARKERS) ─────
#
# Known large OTAs/marketplaces/aggregators/classifieds/platforms: a single
# SEARCH RESULT from one of these is a listing of MANY subjects, not one
# identifiable candidate - confirmed in the live probe (Tripster, youtravel.me,
# Avito, Yandex Uslugi all returned multi-subject listing pages, not a single
# project's own page). GuideGo (a guide MARKETPLACE, not a single guide) is
# the same category. travel.yandex.ru is Yandex's own travel media/journal -
# an editorial listicle about several bloggers, not any one of them's own page.
_DENYLIST_DOMAINS: frozenset[str] = frozenset({
    "tripster.ru", "youtravel.me", "avito.ru", "uslugi.yandex.ru",
    "guidego.ru", "travel.yandex.ru",
    # Large/well-known OTAs - never independent prospect candidates even if
    # a query happens to surface them. sletat.ru and sputnik8.com both
    # confirmed via live dry-run (2026-10-06): multi-operator aggregators
    # ("30932 экскурсии в 1108 городах"), not single businesses.
    "booking.com", "ostrovok.ru", "tutu.ru", "onetwotrip.com", "sletat.ru",
    "sputnik8.com",
})

# Phrase markers for categories that are not domain-identifiable.
# Live-probe-confirmed cases (see the Partner Discovery v0 experiment):
# - a Moscow municipal senior-citizens excursion programme ("Московское
#   долголетие") surfaced under "клуб путешественников" - a government
#   social programme, not an independent travel business/creator;
# - a "travel club" ("Ближе к Солнцу") whose own text is an MLM-style
#   referral/commission pitch (60% commission, one-time membership fee,
#   passive dividends) - explicitly NOT a genuine travel subject to
#   prospect, and the wrong kind of thing to approach (same genre as what
#   this very feature could be mistaken for).
_NOISE_MARKERS: tuple[str, ...] = (
    "московского долголетия",
    "реферальная система", "пассивный доход", "дивиденд",
    "единоразовый взнос", "комиссии с каждого тура",
    "вакансия", "резюме соискателя", "ищем сотрудника",
)


def _is_noise(result: SearchResult) -> bool:
    domain = (result.domain or "").lower().removeprefix("www.")
    if domain in _DENYLIST_DOMAINS:
        return True
    haystack = f"{result.title} {result.snippet}".lower()
    return any(marker in haystack for marker in _NOISE_MARKERS)


# ── Deterministic type/fit scoring (same style as lead_radar.py's
# _content_quality_rank - keyword tiers, no LLM, no ai_score reuse) ────────
_TYPE_MARKERS: tuple[tuple[ProspectType, tuple[str, ...]], ...] = (
    (ProspectType.TRAVEL_AGENT, (
        "турагент", "туроператор", "турфирма", "туристический оператор",
        "рто ", "лицензия туристического оператора", "реестре туроператоров",
    )),
    (ProspectType.TOUR_ORGANIZER, (
        "авторские туры", "авторский тур", "организатор путешествий",
        "организуем путешествия", "организовали", "групповые туры",
    )),
    (ProspectType.GUIDE, (
        "гид", "экскурсовод", "экскурсии",
    )),
    (ProspectType.TRAVEL_CREATOR, (
        "travel-блог", "трэвел-блог", "тревел-блог", "блог о путешествиях",
        "блог путешеств", "travel blog",
    )),
    (ProspectType.TRAVEL_CLUB, (
        "клуб путешественников", "клуб путешествий", "сообщество путешественников",
        "travellers club", "комьюнити путешественников",
    )),
)


def _score(result: SearchResult) -> tuple[ProspectType, int]:
    """Dominant type (first tier with the most hits, ties broken by the
    tier order above) + total marker hits across every tier as the
    deterministic rank score. Zero hits -> (OTHER, 0), handled by the
    caller as POSSIBLE fit, not dropped - a travel-context query result
    with no specific-type marker is still travel-related, just unclear."""
    haystack = f"{result.title} {result.snippet}".lower()
    best_type = ProspectType.OTHER
    best_count = 0
    total = 0
    for prospect_type, markers in _TYPE_MARKERS:
        count = sum(1 for marker in markers if marker in haystack)
        total += count
        if count > best_count:
            best_count = count
            best_type = prospect_type
    return best_type, total


def _raw_dedupe_key(url: str) -> str:
    """Collapses only EXACT duplicate URLs seen across different queries
    (e.g. the same page surfacing for two different search terms). Always
    domain+path - this is not where the one-prospect-per-business rule
    lives, see ``_identity_key`` below for that."""
    domain = canonical_domain(url)
    path = urlsplit(url).path.rstrip("/")
    return f"{domain}{path}"


# Multi-tenant/social platforms: one domain legitimately hosts many
# unrelated subjects (vk.ru/vk.com host independent communities/pages, t.me
# independent channels - confirmed in the live probe, 3 different vk.ru
# communities all independently relevant). For these, identity is
# domain+path. Every other site is a business's own domain: two pages under
# it (e.g. magput.ru's "авторские туры" page and its "гильдия гидов" page -
# the exact live-probe duplicate this fix addresses) are the SAME prospect.
_MULTI_TENANT_DOMAINS: frozenset[str] = frozenset({"vk.ru", "vk.com", "t.me"})


def _identity_key(url: str) -> str:
    """The one-prospect-per-business identity used to dedupe the final
    candidate list. Deliberately just these two cases - no generic
    multi-tenant-detection framework, per v0's narrow-fix scope."""
    domain = canonical_domain(url)
    if domain in _MULTI_TENANT_DOMAINS:
        path = urlsplit(url).path.rstrip("/")
        return f"{domain}{path}"
    return domain


def _identity_tie_break(item: tuple[SearchResult, str, ProspectType, int, str, EvidenceSource]) -> tuple:
    """Higher is better. Order: fit (score>0) > deterministic score > page
    evidence over snippet > better (lower) original search rank - exactly
    the 4-step rule for picking the one candidate to keep per business."""
    result, _query, _prospect_type, score, _evidence, evidence_source = item
    return (score > 0, score, evidence_source is EvidenceSource.PAGE, -result.rank)


def _dedupe_by_identity(
    items: list[tuple[SearchResult, str, ProspectType, int, str, EvidenceSource]],
) -> list[tuple[SearchResult, str, ProspectType, int, str, EvidenceSource]]:
    best_by_key: dict[str, tuple] = {}
    for item in items:
        key = _identity_key(item[0].url)
        current_best = best_by_key.get(key)
        if current_best is None or _identity_tie_break(item) > _identity_tie_break(current_best):
            best_by_key[key] = item
    return list(best_by_key.values())


def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


class PartnerDiscoveryService:
    def __init__(self, provider: WebSearchProvider) -> None:
        self._provider = provider

    async def find_candidates(
        self, *, rotation_seed: int | None = None, use_all_variants: bool = False,
        search_deadline: float | None = None, verify_deadline: float | None = None,
        max_results: int = _MAX_RESULTS,
    ) -> list[ProspectCandidate]:
        """Runs the fixed-size (always 5) query list, dedupes, filters
        noise, ranks, page-verifies the best survivors, and returns at most
        ``_MAX_RESULTS`` candidates. Never writes anywhere; safe to call
        repeatedly.

        ``rotation_seed`` (2026-10-07, typically a workspace_id) selects
        which synonymous query variant is used per category - see
        _select_discovery_queries. None (the default) reproduces the
        original fixed DISCOVERY_QUERIES exactly - this parameter changes
        NOTHING about scoring/dedupe/noise-filtering/ranking, only which
        search strings are sent.

        ``search_deadline`` / ``verify_deadline`` (``time.monotonic()``
        values, default None = unlimited, i.e. Telegram's behaviour) bound
        the query phase and the page-verification phase: past the deadline
        no new query is started / pages are no longer fetched and the
        search snippet is used as evidence (tagged SNIPPET as always).

        ``max_results`` caps the returned list (default ``_MAX_RESULTS``).
        The order is unchanged, so a larger value only appends the rest of
        the already-verified pool behind the usual first five."""
        queries = (
            _select_all_variant_queries(rotation_seed) if use_all_variants
            else _select_discovery_queries(rotation_seed)
        )
        log.info("partner_discovery.search_started query_count=%d", len(queries))
        seen: dict[str, tuple[SearchResult, str]] = {}
        for query_index, query in enumerate(queries, start=1):
            if search_deadline is not None and time.monotonic() >= search_deadline:
                log.warning(
                    "partner_discovery.search_budget_exhausted skipped_queries=%d/%d",
                    len(queries) - query_index + 1, len(queries),
                )
                break
            query_started = time.monotonic()
            try:
                response = await asyncio.to_thread(
                    self._provider.search, query, limit=_RESULTS_PER_QUERY,
                )
                # At most ONE retry, only for a transient network failure
                # (timeout / connection error) and only while the search
                # budget still has room - never for HTTP/parse failures.
                if (
                    response is None
                    and getattr(self._provider, "last_failure_transient", False) is True
                    and (
                        search_deadline is None
                        or time.monotonic() + _RETRY_MIN_REMAINING_S < search_deadline
                    )
                ):
                    log.info(
                        "partner_discovery.query_retry index=%d/%d query=%r",
                        query_index, len(queries), query,
                    )
                    response = await asyncio.to_thread(
                        self._provider.search, query, limit=_RESULTS_PER_QUERY,
                    )
            except Exception:
                log.warning(
                    "partner_discovery: search failed for query '%s'", query, exc_info=True,
                )
                continue
            if response is None:
                # Diagnostics only: the provider logs the failure reason on the
                # line right before this one (queries run sequentially).
                log.warning(
                    "partner_discovery.query_failed index=%d/%d query=%r elapsed_ms=%d",
                    query_index, len(queries), query,
                    int((time.monotonic() - query_started) * 1000),
                )
                continue
            for result in response.results:
                if not result.url:
                    continue
                key = _raw_dedupe_key(result.url)
                if key in seen:
                    continue  # first query to find a subject keeps provenance
                seen[key] = (result, query)

        log.info("partner_discovery.results_received raw_count=%d", len(seen))

        ranked: list[tuple[SearchResult, str, ProspectType, int]] = []
        for result, query in seen.values():
            if _is_noise(result):
                continue
            prospect_type, score = _score(result)
            ranked.append((result, query, prospect_type, score))

        # Highest score first; stable sort keeps search-rank order as the
        # tie-breaker (earlier tuples were inserted in query/rank order).
        ranked.sort(key=lambda item: item[3], reverse=True)

        log.info(
            "partner_discovery.ranked raw=%d after_noise_filter=%d top=%s",
            len(seen), len(ranked),
            [(r.url, sc) for r, _q, _t, sc in ranked[:_MAX_PAGE_VERIFY_CANDIDATES]],
        )
        verified: list[tuple[SearchResult, str, ProspectType, int, str, EvidenceSource]] = []
        for result, query, prospect_type, score in ranked[:_MAX_PAGE_VERIFY_CANDIDATES]:
            if verify_deadline is not None and time.monotonic() >= verify_deadline:
                evidence = _collapse_whitespace(result.snippet)[:_EVIDENCE_EXCERPT_CHARS]
                evidence_source = EvidenceSource.SNIPPET
            else:
                evidence, evidence_source = await self._verify(result)
            verified.append((result, query, prospect_type, score, evidence, evidence_source))
        log.info(
            "partner_discovery.verified %s",
            [(r.url, sc, src.value) for r, _q, _t, sc, _e, src in verified],
        )

        # One-prospect-per-business: collapse pages that belong to the same
        # business (see _identity_key) down to their single best candidate
        # before capping the final list - done only now, since the tie-break
        # rule needs each candidate's score AND its verification outcome.
        deduped = _dedupe_by_identity(verified)
        deduped.sort(key=lambda item: item[3], reverse=True)
        log.info(
            "partner_discovery.deduplicated verified_count=%d deduped_count=%d",
            len(verified), len(deduped),
        )

        candidates = [
            ProspectCandidate(
                name=result.title or result.url,
                url=result.url,
                prospect_type=prospect_type,
                query=query,
                evidence=evidence,
                evidence_source=evidence_source,
                fit=ProspectFit.STRONG if score > 0 else ProspectFit.POSSIBLE,
                rank_score=score,
            )
            for result, query, prospect_type, score, evidence, evidence_source in deduped
        ]

        log.info(
            "partner_discovery.final_selection max=%d selected=%s dropped_by_cap=%s",
            max_results,
            [(c.url, c.rank_score) for c in candidates[:max_results]],
            [(c.url, c.rank_score) for c in candidates[max_results:]],
        )
        return candidates[:max_results]

    async def _verify(self, result: SearchResult) -> tuple[str, EvidenceSource]:
        """Best-effort page read. A fetch failure (bot/login wall, oversized
        page, network error - all already handled inside
        fetch_public_source_sync as PublicSourceFetchError) falls back to
        the search snippet as evidence, explicitly tagged SNIPPET - never
        silently treated as page-verified."""
        try:
            page = await asyncio.to_thread(fetch_public_source_sync, result.url)
        except PublicSourceFetchError as exc:
            log.info(
                "partner_discovery: page verification failed for '%s': %s",
                result.url, exc,
            )
            return _collapse_whitespace(result.snippet)[:_EVIDENCE_EXCERPT_CHARS], EvidenceSource.SNIPPET
        except Exception:
            log.warning(
                "partner_discovery: unexpected error verifying '%s'",
                result.url, exc_info=True,
            )
            return _collapse_whitespace(result.snippet)[:_EVIDENCE_EXCERPT_CHARS], EvidenceSource.SNIPPET

        excerpt = _collapse_whitespace(page.text)[:_EVIDENCE_EXCERPT_CHARS]
        if not excerpt:
            return _collapse_whitespace(result.snippet)[:_EVIDENCE_EXCERPT_CHARS], EvidenceSource.SNIPPET
        return excerpt, EvidenceSource.PAGE
