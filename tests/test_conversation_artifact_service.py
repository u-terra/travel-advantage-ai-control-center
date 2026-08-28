from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.conversation_state_repository import ConversationStateRepository
from app.repositories.partner_repository import PartnerRepository
from app.services.conversation_artifact_service import ConversationArtifactService

USER_A = 586249067


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _service(
    tmp_path: Path,
) -> tuple[ConversationArtifactService, ArtifactRepository, ConversationStateRepository, int]:
    db_path = tmp_path / "journal.sqlite3"
    # artifacts.workspace_id FK-references a real partner_workspaces row -
    # same shared-db reality as production (see app/main.py: everything
    # lives in one journal.sqlite3 file, and workspaces are provisioned,
    # never just an arbitrary int).
    partners = PartnerRepository(db_path)
    _run(partners.init())
    workspace, _ = _run(partners.ensure_owner_workspace(USER_A))
    artifacts = ArtifactRepository(db_path)
    _run(artifacts.init())
    conversation = ConversationStateRepository(db_path)
    _run(conversation.init())
    return ConversationArtifactService(artifacts, conversation), artifacts, conversation, workspace.id


# ── get_current ──────────────────────────────────────────────────────────


def test_get_current_returns_none_without_conversation_state(tmp_path: Path) -> None:
    service, _, _, workspace_id = _service(tmp_path)
    assert _run(service.get_current(workspace_id, USER_A)) is None


def test_get_current_returns_none_when_state_has_no_artifact(tmp_path: Path) -> None:
    service, _, conversation, workspace_id = _service(tmp_path)
    _run(conversation.upsert_state(workspace_id, USER_A, active_module="content_factory"))
    assert _run(service.get_current(workspace_id, USER_A)) is None


def test_get_current_resolves_artifact_and_current_version(tmp_path: Path) -> None:
    service, artifacts, conversation, workspace_id = _service(tmp_path)
    artifact, version = _run(artifacts.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост", content="Версия 1",
    ))
    _run(conversation.upsert_state(workspace_id, USER_A, current_artifact_id=artifact.id))

    current = _run(service.get_current(workspace_id, USER_A))

    assert current is not None
    assert current.artifact.id == artifact.id
    assert current.version.content == "Версия 1"
    assert current.version.version_number == 1


def test_get_current_reflects_latest_version_after_a_revision(tmp_path: Path) -> None:
    service, artifacts, conversation, workspace_id = _service(tmp_path)
    artifact, _ = _run(artifacts.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост", content="Версия 1",
    ))
    _run(artifacts.add_artifact_version(workspace_id, artifact.id, "Версия 2"))
    _run(conversation.upsert_state(workspace_id, USER_A, current_artifact_id=artifact.id))

    current = _run(service.get_current(workspace_id, USER_A))

    assert current is not None
    assert current.version.content == "Версия 2"
    assert current.version.version_number == 2


def test_get_current_is_workspace_scoped(tmp_path: Path) -> None:
    service, artifacts, conversation, workspace_id = _service(tmp_path)
    artifact, _ = _run(artifacts.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост", content="Версия 1",
    ))
    _run(conversation.upsert_state(workspace_id, USER_A, current_artifact_id=artifact.id))

    assert _run(service.get_current(workspace_id + 1, USER_A)) is None


# ── get_previous_version ─────────────────────────────────────────────────


def test_get_previous_version_is_none_for_a_single_version_artifact(tmp_path: Path) -> None:
    service, artifacts, _, workspace_id = _service(tmp_path)
    artifact, _ = _run(artifacts.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост", content="Версия 1",
    ))
    assert _run(service.get_previous_version(workspace_id, artifact.id)) is None


def test_get_previous_version_returns_the_version_before_current(tmp_path: Path) -> None:
    service, artifacts, _, workspace_id = _service(tmp_path)
    artifact, _ = _run(artifacts.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост", content="Версия 1",
    ))
    _run(artifacts.add_artifact_version(workspace_id, artifact.id, "Версия 2"))
    _run(artifacts.add_artifact_version(workspace_id, artifact.id, "Версия 3"))

    previous = _run(service.get_previous_version(workspace_id, artifact.id))

    assert previous is not None
    assert previous.version_number == 2
    assert previous.content == "Версия 2"


def test_get_previous_version_is_none_for_missing_artifact(tmp_path: Path) -> None:
    service, _, _, workspace_id = _service(tmp_path)
    assert _run(service.get_previous_version(workspace_id, 999)) is None


# ── add_revision_if_current ──────────────────────────────────────────────


def test_add_revision_if_current_appends_a_new_version(tmp_path: Path) -> None:
    service, artifacts, _, workspace_id = _service(tmp_path)
    artifact, version = _run(artifacts.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост", content="Версия 1",
    ))

    revised = _run(service.add_revision_if_current(
        workspace_id, artifact.id, version.id, "Версия 2 (живее)",
        generation_note="revision pilot",
    ))

    assert revised is not None
    assert revised.version_number == 2
    assert revised.content == "Версия 2 (живее)"
    current = _run(artifacts.get_current_artifact_version(workspace_id, artifact.id))
    assert current is not None
    assert current.id == revised.id


def test_add_revision_if_current_fails_closed_on_stale_expected_version(tmp_path: Path) -> None:
    """History is never rewritten: a stale expected_current_version_id (the
    artifact moved on since it was read) must not silently overwrite it."""
    service, artifacts, _, workspace_id = _service(tmp_path)
    artifact, version = _run(artifacts.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост", content="Версия 1",
    ))
    _run(artifacts.add_artifact_version(workspace_id, artifact.id, "Версия 2"))

    result = _run(service.add_revision_if_current(
        workspace_id, artifact.id, version.id, "Конфликтующая версия",
    ))

    assert result is None
    current = _run(artifacts.get_current_artifact_version(workspace_id, artifact.id))
    assert current is not None
    assert current.content == "Версия 2"  # unchanged
