from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from app.repositories.conversation_state_repository import ConversationStateRepository
from app.services.conversation_state_service import ConversationStateService

WORKSPACE_A = 1
USER_A = 586249067


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_record_artifact_is_a_noop_without_a_repository() -> None:
    service = ConversationStateService(None)
    _run(service.record_artifact(WORKSPACE_A, USER_A, 1, active_module="x", current_task="y", last_action="z"))  # must not raise


def test_record_subject_ref_is_a_noop_without_a_repository() -> None:
    service = ConversationStateService(None)
    _run(service.record_subject_ref(
        WORKSPACE_A, USER_A, subject_ref_type="competitor", subject_ref_id=7,
        active_module="x", current_task="y", last_action="z",
    ))  # must not raise


def test_record_artifact_patches_the_repository(tmp_path: Path) -> None:
    repository = ConversationStateRepository(tmp_path / "journal.sqlite3")
    _run(repository.init())
    service = ConversationStateService(repository)

    _run(service.record_artifact(
        WORKSPACE_A, USER_A, 42,
        active_module="content_factory", current_task="generate_source_material",
        last_action="content_factory_generate",
    ))

    state = _run(repository.get_state(WORKSPACE_A, USER_A))
    assert state is not None
    assert state.current_artifact_id == 42
    assert state.active_module == "content_factory"
    assert state.last_action == "content_factory_generate"


def test_record_subject_ref_patches_the_repository(tmp_path: Path) -> None:
    repository = ConversationStateRepository(tmp_path / "journal.sqlite3")
    _run(repository.init())
    service = ConversationStateService(repository)

    _run(service.record_subject_ref(
        WORKSPACE_A, USER_A, subject_ref_type="competitor", subject_ref_id=7,
        active_module="competitors", current_task="rename_competitor",
        last_action="rename_competitor_started",
    ))

    state = _run(repository.get_state(WORKSPACE_A, USER_A))
    assert state is not None
    assert state.current_subject_ref_type == "competitor"
    assert state.current_subject_ref_id == 7


def test_record_artifact_swallows_repository_failures() -> None:
    """Error policy: a technical write failure must never propagate out of
    this service - the caller has already produced real user-facing content
    by the time this runs (see the F2A report, error policy section)."""
    repository = AsyncMock()
    repository.patch_state.side_effect = RuntimeError("disk full")
    service = ConversationStateService(repository)

    _run(service.record_artifact(
        WORKSPACE_A, USER_A, 42,
        active_module="content_factory", current_task="x", last_action="y",
    ))  # must not raise


def test_record_subject_ref_swallows_repository_failures() -> None:
    repository = AsyncMock()
    repository.patch_state.side_effect = RuntimeError("disk full")
    service = ConversationStateService(repository)

    _run(service.record_subject_ref(
        WORKSPACE_A, USER_A, subject_ref_type="competitor", subject_ref_id=7,
        active_module="competitors", current_task="x", last_action="y",
    ))  # must not raise
