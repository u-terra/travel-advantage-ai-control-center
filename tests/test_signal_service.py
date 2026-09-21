"""Unit tests for app.services.signal_service - the shared read path behind
both Telegram's on_find_signals() and Web's GET /api/signals (bugs 1/2/5).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.repositories.web_signal_repository import WebSignalRecord
from app.services.lead_radar import LeadSignal
from app.services.signal_service import build_unified_feed


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
