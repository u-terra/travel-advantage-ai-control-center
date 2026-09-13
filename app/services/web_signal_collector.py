"""Stage 2 (ORCHESTRAVEL): on-demand collector for a workspace's own
``platform="web"`` source_catalog subscriptions.

Reuses the exact same primitives ``app.services.competitor_discovery.
CompetitorDiscoveryService._scan_curated_sources`` already uses in
production - ``fetch_public_source_sync`` (SSRF-hardened public fetch) and
``LLMProvider.analyze_source`` (same LLM source analysis every other flow in
this repo uses) - applied to a workspace's own enabled subscriptions
(``SourceCatalogRepository.list_for_workspace``) instead of a fixed curated
list. Deliberately does NOT touch app.services.competitor_discovery itself
(no shared code was extracted from it) - that service is a separate,
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

Not scheduled: no cron/worker calls this. Stage 2 is on-demand only, from
the "Найти сигналы" user action - see the task's own scope note. A future
background pass is expected to reuse this same class.
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

log = logging.getLogger(__name__)

_ANALYSIS_TEXT_CHARS = 6_000
_SUMMARY_MAX_CHARS = 600
_TITLE_MAX_CHARS = 300


@dataclass(frozen=True)
class WebSignalCollectionOutcome:
    workspace_id: int
    sources_attempted: int
    sources_collected: int
    # Requirement 9: one bad site must never break the run - every skipped
    # source_id (fetch error, unusable content, LLM failure) ends up here
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
        # Keyed by URL, not source_id: if two of this workspace's own
        # subscriptions happen to resolve to the same physical page (a
        # platform source plus a private duplicate of it), the page is
        # fetched and analyzed only once per run - each subscription still
        # gets its own stored row (its own source_id/source_name), just
        # built from the same fetched content.
        cache: dict[str, WebSignalRecord | None] = {}
        records: list[WebSignalRecord] = []
        failed: list[str] = []
        for source in targets:
            url = source.target
            if url not in cache:
                try:
                    cache[url] = await self._fetch_and_analyze(workspace_id, source)
                except Exception:
                    log.warning(
                        "web_signal_collector: unexpected error collecting source '%s'",
                        source.id, exc_info=True,
                    )
                    cache[url] = None
            base = cache[url]
            if base is None:
                failed.append(source.id)
                continue
            records.append(replace(base, source_id=source.id, source_name=source.name))

        collected = await self._signals.save_many(records) if records else 0
        return WebSignalCollectionOutcome(
            workspace_id=workspace_id, sources_attempted=len(targets),
            sources_collected=collected, failed_source_ids=tuple(failed),
        )

    async def _fetch_and_analyze(
        self, workspace_id: int, source: WorkspaceSource
    ) -> WebSignalRecord | None:
        """Isolated per-source: any failure here (bad fetch, unusable
        content, LLM error) returns None instead of raising - the caller
        records it as a failed source_id and moves on to the rest."""
        url = source.target
        try:
            page = await asyncio.to_thread(self._fetcher, url)
        except PublicSourceFetchError as exc:
            log.info("web_signal_collector: fetch failed for '%s': %s", source.id, exc)
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
            log.info("web_signal_collector: analysis failed for '%s'", source.id)
            return None

        title = (page.title or source.name).strip()[:_TITLE_MAX_CHARS] or source.name
        summary = (analysis.summary or "").strip()
        if not summary and analysis.key_facts:
            summary = analysis.key_facts[0]
        summary = summary[:_SUMMARY_MAX_CHARS]

        return WebSignalRecord(
            workspace_id=workspace_id, source_id=source.id, source_name=source.name,
            source_url=url, item_url=page.final_url or url,
            title=title, summary=summary, fetched_at=_now(),
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
