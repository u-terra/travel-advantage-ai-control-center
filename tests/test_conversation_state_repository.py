from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app.domain.conversation_state import OfferItem, OfferValidationError
from app.repositories.conversation_state_repository import (
    ConversationStateConflictError,
    ConversationStateRepository,
    ConversationStateSerializationError,
)

WORKSPACE_A = 1
WORKSPACE_B = 2
USER_A = 586249067
USER_B = 111222333


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _repository(tmp_path: Path) -> ConversationStateRepository:
    return ConversationStateRepository(tmp_path / "journal.sqlite3")


def _past() -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()


def _future() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()


def _items() -> tuple[OfferItem, ...]:
    return (
        OfferItem(id="1", label="Тема раз", payload={"angle": "history"}),
        OfferItem(id="2", label="Тема два", payload={"angle": "food"}),
        OfferItem(id="3", label="Тема три", payload={"angle": "nature"}),
    )


# ── init ─────────────────────────────────────────────────────────────────


def test_init_is_idempotent(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    _run(repository.init())  # must not raise on second call

    with sqlite3.connect(repository.db_path) as db:
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {
        "conversation_state", "conversation_offer", "conversation_pending_question",
    } <= tables


# ── ConversationState ────────────────────────────────────────────────────


def test_get_state_returns_none_when_absent(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())

    assert _run(repository.get_state(WORKSPACE_A, USER_A)) is None


def test_upsert_state_then_get(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())

    state = _run(
        repository.upsert_state(
            WORKSPACE_A, USER_A,
            active_module="content_factory",
            current_task="написать пост",
            current_artifact_id=42,
            last_action="generate_content",
        )
    )
    assert state.active_module == "content_factory"
    assert state.current_artifact_id == 42

    fetched = _run(repository.get_state(WORKSPACE_A, USER_A))
    assert fetched == state


def test_upsert_state_fully_replaces_previous_row(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())

    _run(
        repository.upsert_state(
            WORKSPACE_A, USER_A, active_module="content_factory", current_artifact_id=1,
        )
    )
    replaced = _run(repository.upsert_state(WORKSPACE_A, USER_A, active_module="radar"))

    assert replaced.active_module == "radar"
    assert replaced.current_artifact_id is None  # not carried over - full replace


def test_state_survives_repository_recreation(tmp_path: Path) -> None:
    db_path = tmp_path / "journal.sqlite3"
    first = ConversationStateRepository(db_path)
    _run(first.init())
    _run(
        first.upsert_state(
            WORKSPACE_A, USER_A, active_module="content_factory", current_artifact_id=7,
        )
    )

    # A fresh instance pointed at the same file simulates a process restart -
    # nothing about `first` is reused.
    second = ConversationStateRepository(db_path)
    _run(second.init())
    state = _run(second.get_state(WORKSPACE_A, USER_A))

    assert state is not None
    assert state.active_module == "content_factory"
    assert state.current_artifact_id == 7


def test_clear_state_removes_row(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    _run(repository.upsert_state(WORKSPACE_A, USER_A, active_module="content_factory"))

    _run(repository.clear_state(WORKSPACE_A, USER_A))

    assert _run(repository.get_state(WORKSPACE_A, USER_A)) is None


def test_clear_state_on_absent_row_does_not_raise(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())

    _run(repository.clear_state(WORKSPACE_A, USER_A))  # must not raise


def test_state_workspace_isolation(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    _run(repository.upsert_state(WORKSPACE_A, USER_A, active_module="content_factory"))

    assert _run(repository.get_state(WORKSPACE_B, USER_A)) is None


def test_state_user_isolation(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    _run(repository.upsert_state(WORKSPACE_A, USER_A, active_module="content_factory"))

    assert _run(repository.get_state(WORKSPACE_A, USER_B)) is None


# ── patch_state (F2A) ────────────────────────────────────────────────────


def test_patch_state_on_absent_row_creates_it_with_only_given_fields(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())

    state = _run(
        repository.patch_state(WORKSPACE_A, USER_A, last_action="rename_competitor_started")
    )
    assert state.last_action == "rename_competitor_started"
    assert state.active_module is None
    assert state.current_artifact_id is None


def test_patch_state_partial_update_does_not_clear_untouched_fields(
    tmp_path: Path,
) -> None:
    """The exact scenario required by the F2A spec: patching only
    last_action must not wipe current_artifact_id/active_module/etc."""
    repository = _repository(tmp_path)
    _run(repository.init())
    _run(
        repository.upsert_state(
            WORKSPACE_A, USER_A,
            active_module="content_factory",
            current_task="generate_source_material",
            current_artifact_id=42,
            last_action="content_factory_generate",
        )
    )

    patched = _run(
        repository.patch_state(WORKSPACE_A, USER_A, last_action="content_factory_regenerate")
    )

    assert patched.last_action == "content_factory_regenerate"
    assert patched.active_module == "content_factory"
    assert patched.current_task == "generate_source_material"
    assert patched.current_artifact_id == 42


def test_patch_state_can_clear_a_field_by_passing_none_explicitly(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    _run(repository.upsert_state(WORKSPACE_A, USER_A, current_artifact_id=42))

    patched = _run(repository.patch_state(WORKSPACE_A, USER_A, current_artifact_id=None))

    assert patched.current_artifact_id is None


def test_patch_state_subject_ref_must_be_patched_as_a_pair(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())

    with pytest.raises(Exception):
        _run(
            repository.patch_state(
                WORKSPACE_A, USER_A, current_subject_ref_type="competitor",
            )
        )
    with pytest.raises(Exception):
        _run(repository.patch_state(WORKSPACE_A, USER_A, current_subject_ref_id=7))


def test_patch_state_can_set_and_clear_subject_ref_together(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())

    set_state = _run(
        repository.patch_state(
            WORKSPACE_A, USER_A,
            current_subject_ref_type="competitor", current_subject_ref_id=7,
        )
    )
    assert set_state.current_subject_ref_type == "competitor"
    assert set_state.current_subject_ref_id == 7

    cleared = _run(
        repository.patch_state(
            WORKSPACE_A, USER_A,
            current_subject_ref_type=None, current_subject_ref_id=None,
        )
    )
    assert cleared.current_subject_ref_type is None
    assert cleared.current_subject_ref_id is None


def test_patch_state_survives_repository_recreation(tmp_path: Path) -> None:
    db_path = tmp_path / "journal.sqlite3"
    first = ConversationStateRepository(db_path)
    _run(first.init())
    _run(first.upsert_state(WORKSPACE_A, USER_A, active_module="content_factory"))
    _run(first.patch_state(WORKSPACE_A, USER_A, current_artifact_id=99))

    second = ConversationStateRepository(db_path)
    _run(second.init())
    state = _run(second.get_state(WORKSPACE_A, USER_A))

    assert state is not None
    assert state.active_module == "content_factory"
    assert state.current_artifact_id == 99


def test_patch_state_workspace_isolation(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    _run(repository.patch_state(WORKSPACE_A, USER_A, current_artifact_id=1))

    assert _run(repository.get_state(WORKSPACE_B, USER_A)) is None


# ── PendingOffer ─────────────────────────────────────────────────────────


def test_create_offer_then_get_active(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())

    offer = _run(
        repository.create_offer(WORKSPACE_A, USER_A, "content_topics", _items())
    )
    assert offer.offer_type == "content_topics"
    assert [item.id for item in offer.items] == ["1", "2", "3"]
    assert offer.items[2].label == "Тема три"

    active = _run(repository.get_active_offer(WORKSPACE_A, USER_A, "content_topics"))
    assert active == offer


def test_active_offers_of_different_types_coexist(tmp_path: Path) -> None:
    """F2D: the unique index is per (workspace_id, telegram_user_id,
    offer_type) - an active radar_content_ideas offer must not block
    creating an active content_topics offer for the same user, and vice
    versa. This is the exact architectural conflict flagged in the F2D
    report (section 14): before this fix, the second create_offer call
    below would have raised ConversationStateConflictError."""
    repository = _repository(tmp_path)
    _run(repository.init())

    radar_offer = _run(
        repository.create_offer(WORKSPACE_A, USER_A, "radar_content_ideas", _items())
    )
    topics_offer = _run(
        repository.create_offer(WORKSPACE_A, USER_A, "content_topics", _items())
    )

    assert _run(
        repository.get_active_offer(WORKSPACE_A, USER_A, "radar_content_ideas")
    ) == radar_offer
    assert _run(
        repository.get_active_offer(WORKSPACE_A, USER_A, "content_topics")
    ) == topics_offer


def test_create_offer_still_conflicts_within_the_same_offer_type(tmp_path: Path) -> None:
    """The per-type unique index still enforces "at most one active offer
    of the SAME type" - F2D only relaxed the cross-type restriction."""
    repository = _repository(tmp_path)
    _run(repository.init())
    _run(repository.create_offer(WORKSPACE_A, USER_A, "content_topics", _items()))

    with pytest.raises(ConversationStateConflictError):
        _run(repository.create_offer(WORKSPACE_A, USER_A, "content_topics", _items()))


def test_offer_survives_repository_recreation(tmp_path: Path) -> None:
    db_path = tmp_path / "journal.sqlite3"
    first = ConversationStateRepository(db_path)
    _run(first.init())
    created = _run(first.create_offer(WORKSPACE_A, USER_A, "content_topics", _items()))

    second = ConversationStateRepository(db_path)
    _run(second.init())
    active = _run(second.get_active_offer(WORKSPACE_A, USER_A, "content_topics"))

    assert active is not None
    assert active.id == created.id
    assert active.items == created.items


def test_expired_offer_is_not_returned_active(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    _run(
        repository.create_offer(
            WORKSPACE_A, USER_A, "content_topics", _items(), expires_at=_past(),
        )
    )

    assert _run(repository.get_active_offer(WORKSPACE_A, USER_A, "content_topics")) is None


def test_non_expired_offer_is_returned_active(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    offer = _run(
        repository.create_offer(
            WORKSPACE_A, USER_A, "content_topics", _items(), expires_at=_future(),
        )
    )

    assert _run(repository.get_active_offer(WORKSPACE_A, USER_A, "content_topics")) == offer


def test_create_offer_raises_conflict_when_active_offer_exists(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    _run(repository.create_offer(WORKSPACE_A, USER_A, "content_topics", _items()))

    with pytest.raises(ConversationStateConflictError):
        _run(repository.create_offer(WORKSPACE_A, USER_A, "content_topics", _items()))


def test_create_offer_succeeds_after_previous_one_expired(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    _run(
        repository.create_offer(
            WORKSPACE_A, USER_A, "content_topics", _items(), expires_at=_past(),
        )
    )

    # Must not raise ConversationStateConflictError - the stale offer is
    # auto-superseded, not a permanent lock.
    new_offer = _run(
        repository.create_offer(WORKSPACE_A, USER_A, "content_topics", _items())
    )
    assert _run(repository.get_active_offer(WORKSPACE_A, USER_A, "content_topics")) == new_offer


def test_consume_offer_exactly_once(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    offer = _run(repository.create_offer(WORKSPACE_A, USER_A, "content_topics", _items()))

    first = _run(repository.consume_offer(WORKSPACE_A, USER_A, offer.id))
    second = _run(repository.consume_offer(WORKSPACE_A, USER_A, offer.id))

    assert first is not None
    assert first.consumed_at is not None
    assert second is None  # already consumed - second caller gets nothing
    assert _run(repository.get_active_offer(WORKSPACE_A, USER_A, "content_topics")) is None


def test_consume_offer_concurrent_callers_exactly_one_wins(tmp_path: Path) -> None:
    """Two 'simultaneous' consume attempts on the same offer - only one
    may succeed, proving the CAS UPDATE guard (not just single-threaded
    ordering) is what enforces exactly-once."""
    repository = _repository(tmp_path)
    _run(repository.init())
    offer = _run(repository.create_offer(WORKSPACE_A, USER_A, "content_topics", _items()))

    async def _race() -> list[Any]:
        return await asyncio.gather(
            repository.consume_offer(WORKSPACE_A, USER_A, offer.id),
            repository.consume_offer(WORKSPACE_A, USER_A, offer.id),
        )

    results = _run(_race())
    winners = [result for result in results if result is not None]
    assert len(winners) == 1


def test_consume_offer_wrong_workspace_returns_none(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    offer = _run(repository.create_offer(WORKSPACE_A, USER_A, "content_topics", _items()))

    assert _run(repository.consume_offer(WORKSPACE_B, USER_A, offer.id)) is None
    # Original offer untouched by the failed cross-workspace attempt.
    assert _run(repository.get_active_offer(WORKSPACE_A, USER_A, "content_topics")) == offer


def test_consume_offer_wrong_user_returns_none(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    offer = _run(repository.create_offer(WORKSPACE_A, USER_A, "content_topics", _items()))

    assert _run(repository.consume_offer(WORKSPACE_A, USER_B, offer.id)) is None


def test_offer_workspace_isolation(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    _run(repository.create_offer(WORKSPACE_A, USER_A, "content_topics", _items()))

    assert _run(repository.get_active_offer(WORKSPACE_B, USER_A, "content_topics")) is None


def test_offer_user_isolation(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    _run(repository.create_offer(WORKSPACE_A, USER_A, "content_topics", _items()))

    assert _run(repository.get_active_offer(WORKSPACE_A, USER_B, "content_topics")) is None


def test_offer_item_rejects_non_json_safe_payload(tmp_path: Path) -> None:
    with pytest.raises(OfferValidationError):
        OfferItem(id="1", label="Тема раз", payload={"bad": object()})


def test_create_offer_rejects_non_json_safe_payload_before_writing(
    tmp_path: Path,
) -> None:
    """OfferItem itself is the fail-closed gate: a caller cannot even
    construct an unsafe item to pass to create_offer."""
    with pytest.raises(OfferValidationError):
        OfferItem(id="1", label="x", payload={"bad": {1, 2, 3}})


def test_stored_offer_items_that_are_corrupt_fail_closed_on_read(
    tmp_path: Path,
) -> None:
    """Simulates DB-level corruption (e.g. hand-edited row) - reading must
    raise, never silently return a partially-parsed/guessed offer."""
    import aiosqlite

    db_path = tmp_path / "journal.sqlite3"
    repository = ConversationStateRepository(db_path)
    _run(repository.init())

    async def _corrupt() -> None:
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO conversation_offer "
                "(workspace_id, telegram_user_id, offer_type, items_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (WORKSPACE_A, USER_A, "content_topics", "not-json", "2026-01-01T00:00:00+00:00"),
            )
            await db.commit()

    _run(_corrupt())

    with pytest.raises(ConversationStateSerializationError):
        _run(repository.get_active_offer(WORKSPACE_A, USER_A, "content_topics"))


# ── PendingQuestion ──────────────────────────────────────────────────────


def test_create_question_then_get_active(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())

    question = _run(
        repository.create_question(
            WORKSPACE_A, USER_A, "confirm_yes_no", "Сделать пост по второй теме?",
        )
    )
    assert question.question_type == "confirm_yes_no"

    active = _run(repository.get_active_question(WORKSPACE_A, USER_A))
    assert active == question


def test_question_survives_repository_recreation(tmp_path: Path) -> None:
    db_path = tmp_path / "journal.sqlite3"
    first = ConversationStateRepository(db_path)
    _run(first.init())
    created = _run(
        first.create_question(WORKSPACE_A, USER_A, "free_text_label", "Как назвать конкурента?")
    )

    second = ConversationStateRepository(db_path)
    _run(second.init())
    active = _run(second.get_active_question(WORKSPACE_A, USER_A))

    assert active is not None
    assert active.id == created.id
    assert active.prompt_text == "Как назвать конкурента?"


def test_expired_question_is_not_returned_active(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    _run(
        repository.create_question(
            WORKSPACE_A, USER_A, "confirm_yes_no", "Сделать пост?", expires_at=_past(),
        )
    )

    assert _run(repository.get_active_question(WORKSPACE_A, USER_A)) is None


def test_create_question_raises_conflict_when_active_question_exists(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    _run(repository.create_question(WORKSPACE_A, USER_A, "confirm_yes_no", "Да?"))

    with pytest.raises(ConversationStateConflictError):
        _run(repository.create_question(WORKSPACE_A, USER_A, "confirm_yes_no", "Другой вопрос?"))


def test_create_question_succeeds_after_previous_one_expired(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    _run(
        repository.create_question(
            WORKSPACE_A, USER_A, "confirm_yes_no", "Старый вопрос?", expires_at=_past(),
        )
    )

    new_question = _run(
        repository.create_question(WORKSPACE_A, USER_A, "confirm_yes_no", "Новый вопрос?")
    )
    assert _run(repository.get_active_question(WORKSPACE_A, USER_A)) == new_question


def test_answer_question_exactly_once(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    question = _run(
        repository.create_question(WORKSPACE_A, USER_A, "confirm_yes_no", "Да?")
    )

    first = _run(repository.answer_question(WORKSPACE_A, USER_A, question.id))
    second = _run(repository.answer_question(WORKSPACE_A, USER_A, question.id))

    assert first is not None
    assert first.answered_at is not None
    assert second is None
    assert _run(repository.get_active_question(WORKSPACE_A, USER_A)) is None


def test_answer_question_concurrent_callers_exactly_one_wins(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    question = _run(
        repository.create_question(WORKSPACE_A, USER_A, "confirm_yes_no", "Да?")
    )

    async def _race() -> list[Any]:
        return await asyncio.gather(
            repository.answer_question(WORKSPACE_A, USER_A, question.id),
            repository.answer_question(WORKSPACE_A, USER_A, question.id),
        )

    results = _run(_race())
    winners = [result for result in results if result is not None]
    assert len(winners) == 1


def test_question_with_subject_ref_round_trips(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())

    question = _run(
        repository.create_question(
            WORKSPACE_A, USER_A, "free_text_label", "Как назвать?",
            subject_ref_type="competitor", subject_ref_id=7,
        )
    )
    assert question.subject_ref_type == "competitor"
    assert question.subject_ref_id == 7


def test_question_workspace_isolation(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    _run(repository.create_question(WORKSPACE_A, USER_A, "confirm_yes_no", "Да?"))

    assert _run(repository.get_active_question(WORKSPACE_B, USER_A)) is None


def test_question_user_isolation(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    _run(repository.create_question(WORKSPACE_A, USER_A, "confirm_yes_no", "Да?"))

    assert _run(repository.get_active_question(WORKSPACE_A, USER_B)) is None
