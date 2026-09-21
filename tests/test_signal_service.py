"""Unit tests for app.services.signal_service - the shared read path behind
both Telegram's on_find_signals() and Web's GET /api/signals (bugs 1/2/5).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app.repositories.web_signal_repository import WebSignalRecord
from app.services.lead_radar import LeadSignal
from app.services.signal_service import (
    build_unified_feed,
    collect_web_signals,
    sync_web_signals_if_stale,
)


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _radar_signal(
    signal_id: int, *, title: str = "Radar title", hours_ago: float = 1.0,
    url: str = "", action: str = "content",
) -> LeadSignal:
    return LeadSignal(
        id=signal_id, created_at=_iso(hours_ago), source_type="rss", score=64.0,
        category="content_signal", title=title, url=url or f"https://radar.example/{signal_id}",
        recommended_action=action, action_label="Тема для контента", action_reason="reason",
    )


def _web_record(
    record_id: int, *, title: str = "Web title", source_name: str = "Aviasales",
    hours_ago: float = 1.0, item_url: str = "",
) -> WebSignalRecord:
    return WebSignalRecord(
        id=record_id, workspace_id=1, source_id=source_name.lower(),
        source_name=source_name, source_url="https://example.com",
        item_url=item_url or f"https://web.example/{record_id}",
        title=title, summary="summary", fetched_at=_iso(hours_ago),
        published_at=_iso(hours_ago),
    )


def _formatters():
    return dict(
        category_label=lambda category: (category or "").upper(),
        why_text=lambda signal: signal.action_reason,
        content_angle_hint=lambda: "hint",
    )


def test_merges_radar_and_web_signals_into_one_feed() -> None:
    radar = [_radar_signal(1)]
    web = [_web_record(1)]

    unified = build_unified_feed(radar, [], web, limit=10, **_formatters())

    kinds = {item.kind for item in unified}
    assert kinds == {"radar", "web"}
    ids = {item.id for item in unified}
    assert ids == {"radar:1", "web:1"}


def test_fresh_signals_rank_ahead_of_stale_ones() -> None:
    fresh = _radar_signal(1, title="Fresh", hours_ago=1.0)
    stale = _radar_signal(2, title="Stale", hours_ago=24.0 * 20, url="https://radar.example/2")

    unified = build_unified_feed([stale, fresh], [], [], limit=10, **_formatters())

    assert [item.title for item in unified] == ["Fresh", "Stale"]


def test_trip_gets_tie_break_priority_not_hard_quota() -> None:
    # Same freshness tier (<=72h) - Trip must win the tie-break, but a
    # clearly fresher non-Trip source still outranks a older Trip item
    # (tie-break only applies within the same freshness tier).
    trip = _web_record(1, title="Trip item", source_name="Trip.com", hours_ago=10.0)
    other = _web_record(2, title="Other item", source_name="Aviasales", hours_ago=10.0)

    unified = build_unified_feed([], [], [trip, other], limit=10, **_formatters())
    assert [item.title for item in unified] == ["Trip item", "Other item"]

    fresher_other = _web_record(
        3, title="Fresher other", source_name="Aviasales", hours_ago=1.0,
    )
    unified2 = build_unified_feed(
        [], [], [trip, fresher_other], limit=10, **_formatters(),
    )
    assert unified2[0].title == "Fresher other"


def test_dedupes_same_url_keeping_the_freshest_copy() -> None:
    same_url = "https://example.org/same-article"
    older = _web_record(1, title="Older copy", hours_ago=48.0, item_url=same_url)
    newer = _web_record(2, title="Newer copy", hours_ago=1.0, item_url=same_url)

    unified = build_unified_feed([], [], [older, newer], limit=10, **_formatters())

    assert len(unified) == 1
    assert unified[0].title == "Newer copy"


def test_already_stored_low_quality_web_records_are_filtered_out_of_the_feed() -> None:
    """Post-deploy bug: the URL Quality Gate only runs at COLLECTION time -
    rows saved before the gate existed (vacancy pages, section homepages)
    are still sitting in web_source_signals and must not resurface in the
    unified feed forever with no DELETE ever issued. Also covers dedupe of
    two already-stored rows that differ only by a tracking param (a
    pre-gate collection run could have left one) and a normalized-title
    duplicate reached via two different URLs."""
    vacancy = _web_record(
        1, title="Vacancy page", item_url="https://example.com/about/vacancies/backend",
    )
    homepage = _web_record(2, title="Homepage", item_url="https://example.com/")
    listing = _web_record(3, title="Blog listing", item_url="https://example.com/blog")
    real_article = _web_record(
        4, title="Real article", item_url="https://example.com/blog/kak-sobrat-chemodan",
    )
    # Older than real_article - a pre-Quality-Gate collection run that kept
    # a tracking param on the same URL. real_article (fresher, clean URL)
    # must win the dedupe, not this one.
    tracked_dup = _web_record(
        5, title="Real article", hours_ago=2.0,
        item_url="https://example.com/blog/kak-sobrat-chemodan?utm_source=old-run",
    )

    unified = build_unified_feed(
        [], [], [vacancy, homepage, listing, real_article, tracked_dup],
        limit=10, **_formatters(),
    )

    assert len(unified) == 1
    assert unified[0].title == "Real article"
    assert unified[0].url == "https://example.com/blog/kak-sobrat-chemodan"


def test_collect_web_signals_calls_the_shared_collector() -> None:
    # Both Telegram's on_find_signals() and Web's GET /api/signals now call
    # this one function instead of each constructing its own
    # WebSignalCollector - assert it actually drives the real collector
    # (collect_for_workspace) rather than silently doing nothing.
    with patch("app.services.signal_service.WebSignalCollector") as collector_cls:
        instance = collector_cls.return_value
        instance.collect_for_workspace = AsyncMock(return_value=None)

        asyncio.run(collect_web_signals(
            42,
            source_catalog_repository=object(),
            web_signal_repository=object(),
            llm_provider=object(),
        ))

        instance.collect_for_workspace.assert_awaited_once_with(42)


def test_collect_web_signals_swallows_collector_failure() -> None:
    # Same best-effort contract Telegram's try/except already had: one
    # source (or the whole collector) failing must not raise out of the
    # shared function and blank the rest of the "Сигналы и идеи" feed.
    with patch("app.services.signal_service.WebSignalCollector") as collector_cls:
        instance = collector_cls.return_value
        instance.collect_for_workspace = AsyncMock(side_effect=RuntimeError("boom"))

        asyncio.run(collect_web_signals(
            42,
            source_catalog_repository=object(),
            web_signal_repository=object(),
            llm_provider=object(),
        ))  # must not raise


class _FakeWebSignalRepository:
    """Bare-bones stand-in with only what sync_web_signals_if_stale reads."""

    def __init__(self, latest: str | None) -> None:
        self._latest = latest

    async def latest_fetched_at(self, workspace_id: int) -> str | None:
        return self._latest


def test_sync_web_signals_if_stale_skips_collection_when_recently_collected() -> None:
    repo = _FakeWebSignalRepository(_iso(0.1))  # 6 minutes ago - well inside guard
    with patch("app.services.signal_service.WebSignalCollector") as collector_cls:
        instance = collector_cls.return_value
        instance.collect_for_workspace = AsyncMock()

        asyncio.run(sync_web_signals_if_stale(
            42,
            source_catalog_repository=object(),
            web_signal_repository=repo,
            llm_provider=object(),
        ))

        instance.collect_for_workspace.assert_not_awaited()


def test_sync_web_signals_if_stale_collects_when_missing_or_older_than_guard() -> None:
    for repo in (
        _FakeWebSignalRepository(None),  # never collected
        _FakeWebSignalRepository(_iso(5.0)),  # older than the 1h guard window
    ):
        with patch("app.services.signal_service.WebSignalCollector") as collector_cls:
            instance = collector_cls.return_value
            instance.collect_for_workspace = AsyncMock()

            asyncio.run(sync_web_signals_if_stale(
                42,
                source_catalog_repository=object(),
                web_signal_repository=repo,
                llm_provider=object(),
            ))

            instance.collect_for_workspace.assert_awaited_once_with(42)
