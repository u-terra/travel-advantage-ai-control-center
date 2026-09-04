from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.domain.telemetry import EventSeverity
from app.repositories.operational_event_repository import OperationalEventRepository


def run(coro):
    return asyncio.run(coro)


def _iso(days_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def test_record_and_count_since(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    repo = OperationalEventRepository(db_path)
    run(repo.init())

    run(repo.record(module="chat", event_type="message", success=True, workspace_id=1))
    run(repo.record(module="chat", event_type="message", success=False, workspace_id=1))
    run(repo.record(module="auth", event_type="login", success=True, workspace_id=2))

    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert run(repo.count_since(since)) == 3
    assert run(repo.count_since(since, module="chat")) == 2
    assert run(repo.count_since(since, module="chat", success=False)) == 1
    assert run(repo.count_since(since, success=False)) == 1


def test_count_since_excludes_events_before_the_window(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    repo = OperationalEventRepository(db_path)
    run(repo.init())
    run(repo.record(module="chat", event_type="message", success=True))

    future_since = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert run(repo.count_since(future_since)) == 0


def test_distinct_workspace_and_web_user_counts(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    repo = OperationalEventRepository(db_path)
    run(repo.init())
    run(repo.record(module="chat", event_type="message", success=True, workspace_id=1, web_user_id=10))
    run(repo.record(module="chat", event_type="message", success=True, workspace_id=1, web_user_id=10))
    run(repo.record(module="chat", event_type="message", success=True, workspace_id=2, web_user_id=20))
    run(repo.record(module="auth", event_type="login", success=True, workspace_id=3, web_user_id=30))

    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert run(repo.distinct_workspace_count_since(since)) == 3
    assert run(repo.distinct_workspace_count_since(since, module="chat")) == 2
    assert run(repo.distinct_web_user_count_since(since)) == 3
    assert run(repo.distinct_web_user_count_since(since, module="chat")) == 2


def test_metadata_is_stored_as_json_and_safe_message_is_truncated(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    repo = OperationalEventRepository(db_path)
    run(repo.init())
    run(repo.record(
        module="chat", event_type="message", success=True,
        safe_message="x" * 1000, metadata={"provider": "openai", "count": 3},
    ))
    events = run(repo.list_recent_events(limit=1))
    assert len(events[0].safe_message) <= 500
    assert "openai" in events[0].metadata_json


def test_error_summary_groups_by_module_event_error_code(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    repo = OperationalEventRepository(db_path)
    run(repo.init())
    run(repo.record(
        module="chat", event_type="message", success=False, workspace_id=2,
        severity=EventSeverity.ERROR, error_code="provider_error",
    ))
    for _ in range(3):
        run(repo.record(
            module="chat", event_type="message", success=False, workspace_id=1,
            severity=EventSeverity.ERROR, error_code="provider_error",
            safe_message="chat provider call failed",
        ))
    run(repo.record(
        module="billing", event_type="robokassa_callback", success=False,
        severity=EventSeverity.CRITICAL, error_code="bad_signature",
    ))
    run(repo.record(module="chat", event_type="message", success=True, workspace_id=1))

    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    groups = run(repo.error_summary_since(since))

    chat_group = next(g for g in groups if g.module == "chat")
    assert chat_group.occurrences == 4
    assert chat_group.workspace_count == 2
    assert chat_group.sample_safe_message == "chat provider call failed"

    # critical severity sorts first regardless of occurrence count.
    assert groups[0].module == "billing"
    assert groups[0].severity.value == "critical"


def test_error_summary_filters_by_module_severity_workspace(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    repo = OperationalEventRepository(db_path)
    run(repo.init())
    run(repo.record(module="chat", event_type="message", success=False, workspace_id=1, severity=EventSeverity.ERROR))
    run(repo.record(module="billing", event_type="robokassa_callback", success=False, workspace_id=2, severity=EventSeverity.WARNING))

    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert len(run(repo.error_summary_since(since, module="chat"))) == 1
    assert len(run(repo.error_summary_since(since, severity="warning"))) == 1
    assert len(run(repo.error_summary_since(since, workspace_id=2))) == 1
    assert len(run(repo.error_summary_since(since, workspace_id=999))) == 0


def test_last_success_returns_none_when_never_happened(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    repo = OperationalEventRepository(db_path)
    run(repo.init())
    assert run(repo.last_success(module="chat", event_type="message")) is None


def test_last_success_ignores_failures(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    repo = OperationalEventRepository(db_path)
    run(repo.init())
    run(repo.record(module="chat", event_type="message", success=False))
    assert run(repo.last_success(module="chat", event_type="message")) is None
    run(repo.record(module="chat", event_type="message", success=True))
    result = run(repo.last_success(module="chat", event_type="message"))
    assert result is not None and result.success is True


def test_return_visit_requires_two_distinct_calendar_days(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    repo = OperationalEventRepository(db_path)
    run(repo.init())

    async def _seed():
        async with __import__("aiosqlite").connect(db_path) as db:
            await db.execute(
                "INSERT INTO operational_events (occurred_at, web_user_id, module, "
                "event_type, severity, success) VALUES (?, ?, 'chat', 'message', 'info', 1)",
                ("2026-01-01T10:00:00+00:00", 1),
            )
            await db.execute(
                "INSERT INTO operational_events (occurred_at, web_user_id, module, "
                "event_type, severity, success) VALUES (?, ?, 'chat', 'message', 'info', 1)",
                ("2026-01-02T10:00:00+00:00", 1),
            )
            # user 2 only has same-day events - not a "return".
            await db.execute(
                "INSERT INTO operational_events (occurred_at, web_user_id, module, "
                "event_type, severity, success) VALUES (?, ?, 'chat', 'message', 'info', 1)",
                ("2026-01-01T09:00:00+00:00", 2),
            )
            await db.execute(
                "INSERT INTO operational_events (occurred_at, web_user_id, module, "
                "event_type, severity, success) VALUES (?, ?, 'chat', 'message', 'info', 1)",
                ("2026-01-01T11:00:00+00:00", 2),
            )
            await db.commit()

    run(_seed())
    assert run(repo.distinct_web_user_count_with_event_on_a_later_day()) == 1
