"""ORCHESTRAVEL: single shared "Сигналы и идеи" read path for Telegram and
Web.

Before this module existed, Telegram's ``on_find_signals`` (app/handlers/
menu.py) and Web's ``GET /api/signals`` (app/web_api.py) each re-implemented
the same three-step Radar read (``sync_eligible()`` -> ``list_for_workspace()``
-> ``build_workspace_signals()``), and only the Telegram copy called
``sync_eligible()``. That is why the Web feed went stale: new rows land in
the external Radar ``leads.db`` (read-only) continuously, but they only
become workspace-visible once ``sync_eligible()`` materializes them into
``workspace_signal_interpretations`` for the current subscriptions - and
nothing on the Web path ever called it. If nobody opened Telegram and
pressed "Найти сигналы" (and the bot process did not just restart), the
workspace's interpretation rows - and therefore the Web feed - simply
stopped advancing, even though Radar collection itself kept working.

``sync_eligible()`` is a cheap idempotent upsert (``ON CONFLICT ... DO
NOTHING``) reading from a small, already-open local Journal DB - it is not
the expensive part of the pipeline (that is Radar's own collection, and the
Stage 2/3 on-demand web-source collector), so calling it on every read is
safe and keeps both surfaces on one code path instead of two that can drift.

This module also merges the legacy Radar feed with the Stage 2/3
``web_source_signals`` feed (Trip/Aviasales/Т-Ж/OneTwoTrip, etc.) into one
ranked, deduplicated list - see ``build_unified_feed()`` - so Web's
"Сигналы и идеи" is no longer Radar-only.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from app.repositories.web_signal_repository import WebSignalRecord, WebSignalRepository
from app.repositories.workspace_signal_repository import (
    WorkspaceSignalRecord,
    WorkspaceSignalRepository,
)
from app.services.lead_radar import LeadRadarConfig, LeadSignal, build_workspace_signals

_DEFAULT_LIMIT = 5
# Freshness gate (bug 1): rank strictly-fresher material ahead of older
# material using the ORIGINAL SOURCE timestamp when one is available
# (WorkspaceSignalRecord.raw_created_at / WebSignalRecord.published_at or
# fetched_at) - never a fabricated/backfilled date. Tiers: <=72h, then
# <=7 days, then everything else (still shown - nothing is dropped here,
# only ordered - the per-action freshness gate inside build_workspace_signals
# already drops what is too old to act on).
_FRESH_TIER_HOURS = 72.0
_STALE_TIER_HOURS = 24.0 * 7

# Bug 2: Trip gets a tie-break nudge in ranking only - never a hard quota or
# a guarantee of inclusion.
_TRIP_TIE_BREAK_MARKER = "trip"


@dataclass(frozen=True)
class UnifiedSignal:
    """One row of the merged Radar + web-source feed, already ranked."""

    id: str
    kind: str  # "radar" | "web"
    title: str
    summary: str
    category: str | None
    category_label: str | None
    recommended_action: str | None
    source_type: str
    source_name: str
    created_at: str
    url: str
    score: float | None
    action_reason: str | None
    content_hint: str | None
    freshness_hours: Optional[float]


async def sync_and_list_radar_signals(
    workspace_id: int,
    *,
    lead_radar_config: LeadRadarConfig,
    workspace_signal_repository: WorkspaceSignalRepository,
    limit: int = _DEFAULT_LIMIT,
) -> tuple[Optional[list[LeadSignal]], list[WorkspaceSignalRecord]]:
    """Shared Radar read: sync newly-eligible rows, then read+rank.

    Returns ``(signals, records)`` - ``signals`` is ``None`` when the
    recommender could not be loaded (same "Радар недоступен" contract
    ``build_workspace_signals`` already had); ``records`` is always the
    underlying list (used to resolve e.g. ``source_name`` for display).
    """
    await workspace_signal_repository.sync_eligible()
    records = await workspace_signal_repository.list_for_workspace(
        workspace_id, limit=200,
    )
    signals = build_workspace_signals(lead_radar_config, records, limit=limit)
    return signals, records


def _freshness_hours(timestamp: str | None) -> Optional[float]:
    raw = (timestamp or "").strip()
    if not raw:
        return None
    normalized = raw.replace(" ", "T", 1) if "T" not in raw else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed).total_seconds() / 3600.0


def _freshness_tier(hours: Optional[float]) -> int:
    """Lower is fresher. Unknown age sorts after everything with a known age -
    it is neither promoted nor dropped, just not preferred over dated items."""
    if hours is None:
        return 3
    if hours <= _FRESH_TIER_HOURS:
        return 0
    if hours <= _STALE_TIER_HOURS:
        return 1
    return 2


_WS = re.compile(r"\s+")


def _dedupe_fingerprint(title: str, url: str) -> str:
    if url and url.strip():
        return "url:" + url.strip().lower()
    normalized = _WS.sub(" ", (title or "").strip().lower())
    return "title:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def build_unified_feed(
    radar_signals: list[LeadSignal],
    radar_records: list[WorkspaceSignalRecord],
    web_records: list[WebSignalRecord],
    *,
    limit: int,
    category_label,
    why_text,
    content_angle_hint,
) -> list[UnifiedSignal]:
    """Merge Radar + Stage 2/3 web-source signals into one ranked, deduped
    feed. Both inputs are expected already tenant/enable-filtered by their
    own repositories (``WorkspaceSignalRepository.list_for_workspace`` /
    ``WebSignalRepository.list_for_workspace`` both hide disabled/deactivated
    sources) - this function only merges, ranks and dedupes what it is given.
    """
    source_names = {r.interpretation_id: r.source_name for r in radar_records}
    unified: list[UnifiedSignal] = []

    for signal in radar_signals:
        hours = _freshness_hours(signal.created_at)
        unified.append(UnifiedSignal(
            id=f"radar:{signal.id}",
            kind="radar",
            title=signal.title or "(без заголовка)",
            summary="",
            category=signal.category,
            category_label=category_label(signal.category),
            recommended_action=signal.recommended_action,
            source_type=signal.source_type,
            source_name=source_names.get(signal.id) or "",
            created_at=signal.created_at,
            url=signal.url,
            score=signal.score,
            action_reason=why_text(signal),
            content_hint=(
                content_angle_hint() if signal.recommended_action == "content" else None
            ),
            freshness_hours=hours,
        ))

    for record in web_records:
        # Stage 3: published_at only ever set from a reliably-found date;
        # fall back to fetched_at (when the page was actually collected) -
        # never a fabricated date.
        best_timestamp = record.published_at or record.fetched_at
        hours = _freshness_hours(best_timestamp)
        unified.append(UnifiedSignal(
            id=f"web:{record.id}",
            kind="web",
            title=record.title or "(без заголовка)",
            summary=record.summary or "",
            category=None,
            category_label=None,
            recommended_action="content",
            source_type="web",
            source_name=record.source_name or "",
            created_at=best_timestamp or record.created_at,
            url=record.item_url,
            score=None,
            action_reason=None,
            content_hint=None,
            freshness_hours=hours,
        ))

    # Dedupe: same URL (or, lacking one, same normalized title) keeps only
    # the freshest instance - Radar and the web-source pipeline can
    # independently pick up the same underlying article.
    best_by_fingerprint: dict[str, UnifiedSignal] = {}
    for item in unified:
        fingerprint = _dedupe_fingerprint(item.title, item.url)
        current = best_by_fingerprint.get(fingerprint)
        if current is None:
            best_by_fingerprint[fingerprint] = item
            continue
        current_hours = current.freshness_hours
        item_hours = item.freshness_hours
        if item_hours is not None and (current_hours is None or item_hours < current_hours):
            best_by_fingerprint[fingerprint] = item

    def _trip_tie_break(item: UnifiedSignal) -> int:
        return 0 if _TRIP_TIE_BREAK_MARKER in (item.source_name or "").lower() else 1

    def _hour_bucket(item: UnifiedSignal) -> float:
        # Rounded to the nearest hour so two items collected moments apart
        # (typical of a single collection run) count as "equally fresh" and
        # fall through to the Trip tie-break below, instead of an
        # imperceptible timestamp difference silently deciding the order.
        if item.freshness_hours is None:
            return 1e12
        return round(item.freshness_hours)

    ranked = sorted(
        best_by_fingerprint.values(),
        key=lambda item: (
            _freshness_tier(item.freshness_hours),
            _hour_bucket(item),
            _trip_tie_break(item),
        ),
    )
    return ranked[:limit]
