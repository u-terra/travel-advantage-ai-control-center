from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.domain.feedback import FeedbackRating, FeedbackStatus
from app.repositories.feedback_repository import FeedbackRepository
from app.repositories.partner_repository import PartnerRepository


def run(coro):
    return asyncio.run(coro)


def _workspace(db_path: Path, telegram_id: int = 100) -> int:
    partners = PartnerRepository(db_path)
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(telegram_id))
    return workspace.id


def test_submit_creates_a_new_feedback_row(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _workspace(db_path)
    repo = FeedbackRepository(db_path)
    run(repo.init())

    feedback = run(repo.submit(
        workspace_id=workspace_id, web_user_id=1, conversation_id=5, message_id=42,
        rating=FeedbackRating.UP,
    ))

    assert feedback.rating is FeedbackRating.UP
    assert feedback.status is FeedbackStatus.NEW
    assert feedback.reason is None


def test_submit_upserts_on_resubmission_for_the_same_message(tmp_path: Path):
    """One (web_user_id, message_id) pair -> one row, never a duplicate -
    resubmitting (👍 then 👎) updates the same row."""
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _workspace(db_path)
    repo = FeedbackRepository(db_path)
    run(repo.init())

    first = run(repo.submit(
        workspace_id=workspace_id, web_user_id=1, conversation_id=5, message_id=42,
        rating=FeedbackRating.UP,
    ))
    second = run(repo.submit(
        workspace_id=workspace_id, web_user_id=1, conversation_id=5, message_id=42,
        rating=FeedbackRating.DOWN, reason="too_generic", comment="Мало конкретики",
    ))

    assert first.id == second.id
    assert second.rating is FeedbackRating.DOWN
    assert second.reason == "too_generic"
    all_rows = run(repo.list_recent())
    assert len(all_rows) == 1


def test_comment_is_truncated_to_a_short_length(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _workspace(db_path)
    repo = FeedbackRepository(db_path)
    run(repo.init())

    feedback = run(repo.submit(
        workspace_id=workspace_id, web_user_id=1, conversation_id=5, message_id=42,
        rating=FeedbackRating.DOWN, reason="other", comment="x" * 5000,
    ))
    assert len(feedback.comment) <= 500


def test_list_recent_filters_by_status_and_workspace(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _workspace(db_path)
    repo = FeedbackRepository(db_path)
    run(repo.init())
    a = run(repo.submit(workspace_id=workspace_id, web_user_id=1, conversation_id=1, message_id=1, rating=FeedbackRating.UP))
    run(repo.submit(workspace_id=workspace_id, web_user_id=1, conversation_id=1, message_id=2, rating=FeedbackRating.DOWN))
    run(repo.set_status(a.id, FeedbackStatus.RESOLVED))

    assert len(run(repo.list_recent(status="resolved"))) == 1
    assert len(run(repo.list_recent(status="new"))) == 1
    assert len(run(repo.list_recent(workspace_id=workspace_id))) == 2
    assert len(run(repo.list_recent(workspace_id=999999))) == 0


def test_set_status_transitions_and_get_returns_current_row(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _workspace(db_path)
    repo = FeedbackRepository(db_path)
    run(repo.init())
    feedback = run(repo.submit(workspace_id=workspace_id, web_user_id=1, conversation_id=1, message_id=1, rating=FeedbackRating.UP))

    updated = run(repo.set_status(feedback.id, FeedbackStatus.REVIEWED))
    assert updated.status is FeedbackStatus.REVIEWED
    fetched = run(repo.get(feedback.id))
    assert fetched.status is FeedbackStatus.REVIEWED


def test_count_since_filters_by_rating_and_window(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _workspace(db_path)
    repo = FeedbackRepository(db_path)
    run(repo.init())
    run(repo.submit(workspace_id=workspace_id, web_user_id=1, conversation_id=1, message_id=1, rating=FeedbackRating.UP))
    run(repo.submit(workspace_id=workspace_id, web_user_id=1, conversation_id=1, message_id=2, rating=FeedbackRating.DOWN))

    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert run(repo.count_since(since)) == 2
    assert run(repo.count_since(since, rating="up")) == 1
    assert run(repo.count_since(since, rating="down")) == 1

    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert run(repo.count_since(future)) == 0
