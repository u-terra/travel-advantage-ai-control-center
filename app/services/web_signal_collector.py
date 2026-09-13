"""Stage 2/3 (ORCHESTRAVEL): on-demand collector for a workspace's own
``platform="web"`` source_catalog subscriptions.

Stage 2 shipped a landing-page-only collector: one fetch of a source's own
canonical URL, one LLM analysis, one stored signal per source. Stage 3 adds
DISCOVERY in front of it - see app.services.web_source_discovery - so a
source contributes its own actual recent articles instead of a description
of its front page. The landing page is now only a FALLBACK, used solely
when discovery finds nothing usable for that source this run (search
disabled/unconfigured, no results, or every discovered candidate failed to
fetch) - see _collect_source and WebSignalRecord.is_fallback.

Reuses the exact same fetch/analyze primitives ``app.services.
competitor_discovery.CompetitorDiscoveryService._scan_curated_sources``
already uses in production - ``fetch_public_source_sync`` (SSRF-hardened
public fetch) and ``LLMProvider.analyze_source`` (same LLM source analysis
every other flow in this repo uses). Deliberately does NOT touch
app.services.competitor_discovery itself - that service is a separate,
already-tested pipeline (candidate discovery, domain verification, brand
slugging) with a different output shape (CompetitorCandidate rows); this
module only needs the same two building blocks, not its machinery.

Storage: WebSignalRepository (Journal DB). Never writes to the external
Travel Lead Radar ``leads.db`` - that boundary stays read-only, exactly as
before this stage (see app.repositories.workspace_signal_repository).

Business logic lives here (service layer), not in a handler, so a future
Web endpoint can call the exact same ``collect_for_workspace`` a Telegram
handler already calls - see app.handlers.menu.on_find_signals for the
current (and, as of Stage 2, only) caller.

Not scheduled: no cron/worker calls this. Stage 3 stays on-demand only,
from the "Найти сигналы" user action, same as Stage 2 - see the task's own
scope note. A future background pass is expected to reuse this same class.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable, Sequence

from app.domain.sources import PLATFORM_WEB, WorkspaceSource
from app.domain.usage import UsageStatus
from app.planner.fetch import (
    FetchedPublicSource,
    PublicSourceFetchError,
    fetch_public_source_sync,
)
from app.repositories.source_catalog_repository import SourceCatalogRepository
from app.repositories.usage_ledger_repository import UsageLedgerRepository
from app.repositories.web_signal_repository import WebSignalRecord, WebSignalRepository
from app.services.llm.base import LLMProvider
from app.services.usage_recorder import record_llm_call
from app.services.web_search.base import WebSearchProvider
from app.services.web_source_discovery import (
    discover_candidate_urls,
    mentions_stale_year,
    normalize_article_url,
    page_looks_like_non_content,
)

log = logging.getLogger(__name__)

_ANALYSIS_TEXT_CHARS = 6_000
_SUMMARY_MAX_CHARS = 600
_TITLE_MAX_CHARS = 300

# Stage 3, requirement 7: technical safety caps (bound network/LLM cost per
# run), NOT a product quota - a source that genuinely has several fresh,
# useful articles gets all of them (up to this cap), not "1-2 like before".
_MAX_DISCOVERY_CANDIDATES = 5
_MAX_ARTICLES_PER_SOURCE = 3


@dataclass(frozen=True)
class WebSignalCollectionOutcome:
    workspace_id: int
    sources_attempted: int
    # Count of DISTINCT sources that contributed at least one signal this
    # run (article or fallback) - not a raw record count, see
    # articles_collected for that. A source contributing 3 articles still
    # counts once here.
    sources_collected: int
    # Total signals actually stored this run, across all sources - can
    # exceed sources_collected now that one source may yield several
    # articles (Stage 2 - before Stage 3 - this always equaled
    # sources_collected, since a source could produce at most one record).
    articles_collected: int
    # Requirement 9: one bad site must never break the run - every source
    # that ended up with zero signals (fetch error, unusable content, LLM
    # failure, or discovery+fallback both came up empty) ends up here
    # instead of raising, so the caller can log it without losing the rest.
    failed_source_ids: tuple[str, ...]


class WebSignalCollector:
    def __init__(
        self,
        source_catalog_repository: SourceCatalogRepository,
        web_signal_repository: WebSignalRepository,
        llm_provider: LLMProvider,
        *,
        fetcher: Callable[[str], FetchedPublicSource] | None = None,
        usage_ledger_repository: UsageLedgerRepository | None = None,
        web_search_provider: WebSearchProvider | None = None,
    ) -> None:
        self._catalog = source_catalog_repository
        self._signals = web_signal_repository
        self._provider = llm_provider
        # Same "resolved at call time, not a bound default" convention as
        # CompetitorDiscoveryService - lets tests patch the module-level
        # fetch_public_source_sync without threading a fake through every
        # call site.
        self._fetcher = fetcher or fetch_public_source_sync
        self._usage_ledger = usage_ledger_repository
        # Stage 3: optional and default-None like every other dependency in
        # this constructor - None (web search disabled/unconfigured, or a
        # caller that predates Stage 3) means discovery is skipped entirely
        # and every source falls straight through to its Stage-2 landing-
        # page fallback. See app.services.web_search.service.WebSearchService
        # .provider for how a caller obtains one.
        self._web_search_provider = web_search_provider
        self._discover = discover_candidate_urls

    async def collect_for_workspace(self, workspace_id: int) -> WebSignalCollectionOutcome:
        """Fetches and stores signals for every ``platform="web"`` source
        this workspace has enabled right now. Multi-tenant by construction:
        the source list comes from THIS workspace's own subscriptions
        (SourceCatalogRepository.list_for_workspace already scopes by
        workspace_id), so a source another workspace enabled is never even
        considered here.
        """
        sources = await self._catalog.list_for_workspace(workspace_id)
        targets = [
            source for source in sources
            if source.enabled and source.platform == PLATFORM_WEB and source.target
        ]
        return await self._collect(workspace_id, targets)

    async def _collect(
        self, workspace_id: int, targets: Sequence[WorkspaceSource]
    ) -> WebSignalCollectionOutcome:
        # Only the landing-page FALLBACK is cached across sources: if two
        # of this workspace's own subscriptions happen to resolve to the
        # same physical landing page (a platform source plus a private
        # duplicate of it), that one page is fetched/analyzed at most once
        # per run. Discovered article candidates are not cached this way -
        # discovery is already domain-restricted per source, so two
        # different sources cannot plausibly discover the same article.
        landing_page_cache: dict[str, WebSignalRecord | None] = {}
        records: list[WebSignalRecord] = []
        failed: list[str] = []
        for source in targets:
            try:
                source_records = await self._collect_source(
                    workspace_id, source, landing_page_cache,
                )
            except Exception:
                log.warning(
                    "web_signal_collector: unexpected error collecting source '%s'",
                    source.id, exc_info=True,
                )
                source_records = []
            if source_records:
                records.extend(source_records)
            else:
                failed.append(source.id)

        collected = await self._signals.save_many(records) if records else 0
        sources_with_signal = len({record.source_id for record in records})
        return WebSignalCollectionOutcome(
            workspace_id=workspace_id, sources_attempted=len(targets),
            sources_collected=sources_with_signal, articles_collected=collected,
            failed_source_ids=tuple(failed),
        )

    async def _collect_source(
        self,
        workspace_id: int,
        source: WorkspaceSource,
        landing_page_cache: dict[str, WebSignalRecord | None],
    ) -> list[WebSignalRecord]:
        """One source, isolated: discovers up to _MAX_ARTICLES_PER_SOURCE
        specific article signals; if none survive (discovery unavailable/
        empty, or every discovered candidate failed to fetch/analyze), falls
        back to a single landing-page signal, same as Stage 2. Requirement 8
        (Trip): this is the ONLY code path for every platform="web" source,
        trip_com included - nothing here reads source.id, so no source can
        be special-cased or force-included by this method.
        """
        candidate_urls: list[str] = []
        if self._web_search_provider is not None:
            try:
                candidate_urls = await asyncio.to_thread(
                    self._discover, self._web_search_provider, source,
                    limit=_MAX_DISCOVERY_CANDIDATES,
                )
            except Exception:
                log.info(
                    "web_signal_collector: discovery failed for '%s'",
                    source.id, exc_info=True,
                )
                candidate_urls = []

        records: list[WebSignalRecord] = []
        for url in candidate_urls[:_MAX_ARTICLES_PER_SOURCE]:
            record = await self._fetch_and_analyze(
                workspace_id, source, url, is_fallback=False,
            )
            if record is not None:
                records.append(record)
        if records:
            return records

        landing_url = source.target
        if landing_url not in landing_page_cache:
            landing_page_cache[landing_url] = await self._fetch_and_analyze(
                workspace_id, source, landing_url, is_fallback=True,
            )
        fallback = landing_page_cache[landing_url]
        if fallback is None:
            return []
        return [replace(fallback, source_id=source.id, source_name=source.name)]

    async def _fetch_and_analyze(
        self, workspace_id: int, source: WorkspaceSource, url: str, *, is_fallback: bool,
    ) -> WebSignalRecord | None:
        """Isolated per-URL: any failure here (bad fetch, unusable content,
        LLM error) returns None instead of raising - the caller moves on to
        the next candidate (or the landing-page fallback) without losing
        the rest of the run."""
        try:
            page = await asyncio.to_thread(self._fetcher, url)
        except PublicSourceFetchError as exc:
            log.info(
                "web_signal_collector: fetch failed for '%s' (%s): %s",
                source.id, url, exc,
            )
            return None

        # Stage 3.1 Quality Gate, requirement 2: catches what the pre-fetch
        # URL heuristic (app.services.web_source_discovery.
        # _rejected_by_url_heuristic) cannot - a 404/vacancy/support page
        # that returns HTTP 200 with an ordinary-looking URL. Checked before
        # the LLM call, not after: no point paying for an analysis of a page
        # we are about to discard anyway.
        rejected_marker = page_looks_like_non_content(page.title, page.text)
        if rejected_marker is not None:
            log.info(
                "web_signal_collector: content rejected for '%s' (%s) - matched '%s'",
                source.id, url, rejected_marker,
            )
            return None

        analysis = await asyncio.to_thread(
            self._provider.analyze_source, source_text=page.text[:_ANALYSIS_TEXT_CHARS],
        )
        await record_llm_call(
            self._usage_ledger, workspace_id=workspace_id, telegram_user_id=None,
            module="web_signal_collector", provider=self._provider.name,
            usage=analysis.usage if analysis is not None else None,
            status=UsageStatus.SUCCESS if analysis is not None else UsageStatus.FAILURE,
        )
        if analysis is None:
            log.info("web_signal_collector: analysis failed for '%s' (%s)", source.id, url)
            return None

        title = (page.title or source.name).strip()[:_TITLE_MAX_CHARS] or source.name
        summary = (analysis.summary or "").strip()
        if not summary and analysis.key_facts:
            summary = analysis.key_facts[0]
        summary = summary[:_SUMMARY_MAX_CHARS]
        item_url = normalize_article_url(page.final_url or url) or (page.final_url or url)

        return WebSignalRecord(
            workspace_id=workspace_id, source_id=source.id, source_name=source.name,
            source_url=source.target, item_url=item_url,
            title=title, summary=summary, fetched_at=_now(),
            # Stage 3, requirement 6: never guessed - fetch_public_source_sync
            # only ever hands back extracted plain text (HTML/meta stripped),
            # so there is no reliable structured date signal to read here.
            # None (unknown) stays honest; see the repository schema comment.
            published_at=None,
            is_fallback=is_fallback,
            # Stage 3.1, requirement 3: a ranking signal only, never a
            # substitute for published_at - checked against our OWN
            # title/summary (not the raw page, which routinely has
            # copyright-footer years unrelated to the article's content).
            is_stale_dated=mentions_stale_year(f"{title} {summary}"),
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Telegram text rendering ──────────────────────────────────────────────────
# A separate, much simpler block than app.services.lead_radar.build_summary:
# web signals have no external action_recommender classification (no
# ai_category/careful_reply/observe/content bucket) to render against - this
# is a plain "here is what your web sources produced" list. Public (no
# leading underscore) for the same reason lead_radar.why_text() is public:
# a single formatter shared by every caller, so a future Web JSON endpoint
# still returns WebSignalRecord objects directly instead of parsing this text.

_WEB_SIGNALS_HEADER = "🌐 Материалы из ваших web-источников"


def format_web_signals_block(records: Sequence[WebSignalRecord], *, limit: int = 5) -> str:
    """Compact text block for a single Telegram message. Empty string when
    there is nothing to show - callers must not send an empty message."""
    visible = list(records)[:limit]
    if not visible:
        return ""
    blocks: list[str] = []
    for index, record in enumerate(visible, start=1):
        lines = [f"{index}. {_truncate(record.title, 110)}"]
        summary = _truncate(record.summary, 220)
        if summary:
            lines.append(summary)
        lines.append(f"Источник: {record.source_name}")
        lines.append(record.item_url or record.source_url)
        blocks.append("\n".join(lines))
    return _WEB_SIGNALS_HEADER + "\n\n" + "\n\n".join(blocks)


def _truncate(text: str, max_len: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"
