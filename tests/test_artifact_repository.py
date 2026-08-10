from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.partner_repository import PartnerRepository
from app.storage import Journal


OWNER_ID = 586249067


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _setup(tmp_path: Path) -> tuple[ArtifactRepository, PartnerRepository, int, int]:
    db_path = tmp_path / "content.sqlite3"
    partners = PartnerRepository(db_path)
    _run(partners.init())
    owner_workspace, _ = _run(partners.ensure_owner_workspace(OWNER_ID))
    other_workspace_id = _insert_workspace(db_path, "other-workspace")
    artifacts = ArtifactRepository(db_path)
    _run(artifacts.init())
    return artifacts, partners, owner_workspace.id, other_workspace_id


def _insert_workspace(db_path: Path, slug: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as db:
        cursor = db.execute(
            "INSERT INTO partner_workspaces "
            "(name, slug, status, created_at, updated_at) "
            "VALUES (?, ?, 'active', ?, ?)",
            (slug, slug, now, now),
        )
        return cursor.lastrowid or 0


def test_schema_creates_tables_in_empty_database(tmp_path: Path) -> None:
    repository = ArtifactRepository(tmp_path / "empty.sqlite3")

    _run(repository.init())

    with sqlite3.connect(repository.db_path) as db:
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {"sources", "artifacts", "artifact_versions"} <= tables


def test_schema_is_idempotent_and_preserves_existing_data(tmp_path: Path) -> None:
    db_path = tmp_path / "existing.sqlite3"
    partners = PartnerRepository(db_path)
    _run(partners.init())
    workspace, profile = _run(partners.ensure_owner_workspace(OWNER_ID))
    journal = Journal(db_path)
    _run(journal.init(workspace.id))
    entry_id = _run(
        journal.add(workspace.id, "Существующая задача", "content", (), "low")
    )
    repository = ArtifactRepository(db_path)

    _run(repository.init())
    _run(repository.init())

    with sqlite3.connect(db_path) as db:
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {"sources", "artifacts", "artifact_versions"} <= tables
    assert _run(journal.last(workspace.id)).id == entry_id
    assert _run(partners.find_workspace_by_telegram_id(OWNER_ID)) == workspace
    assert _run(partners.get_profile(workspace.id)) == profile


def test_create_source_requires_url_or_text_and_is_workspace_scoped(
    tmp_path: Path,
) -> None:
    repository, _, owner_id, other_id = _setup(tmp_path)

    source = _run(
        repository.create_source(
            owner_id,
            source_type="text",
            title="Заметка",
            original_text="Исходный текст",
        )
    )
    url_source = _run(
        repository.create_source(
            other_id,
            source_type="url",
            title="Ссылка",
            original_url="https://example.com/travel",
        )
    )

    assert source.original_url is None
    assert url_source.original_text is None
    assert _run(repository.get_source(owner_id, source.id)) == source
    assert _run(repository.get_source(other_id, source.id)) is None
    assert _run(repository.list_sources(owner_id)) == [source]
    assert _run(repository.list_sources(other_id)) == [url_source]
    with pytest.raises(ValueError):
        _run(
            repository.create_source(
                owner_id, source_type="note", title="Пустой источник"
            )
        )
    with pytest.raises(ValueError):
        _run(
            repository.create_source(
                owner_id,
                source_type="note",
                title="Пробельный источник",
                original_url="  ",
                original_text="\t",
            )
        )
    with pytest.raises(ValueError, match="title"):
        _run(
            repository.create_source(
                owner_id,
                source_type="text",
                title="  ",
                original_text="Содержимое",
            )
        )
    with pytest.raises(ValueError, match="limit"):
        _run(repository.list_sources(owner_id, limit=0))


def test_create_artifact_with_optional_same_workspace_source(tmp_path: Path) -> None:
    repository, _, owner_id, _ = _setup(tmp_path)
    source = _run(
        repository.create_source(
            owner_id,
            source_type="note",
            title="Идея",
            original_text="Идея поста",
        )
    )

    without_source = _run(
        repository.create_artifact(
            owner_id, artifact_type="post", title="Самостоятельный пост"
        )
    )
    with_source = _run(
        repository.create_artifact(
            owner_id,
            artifact_type="video_script",
            title="Сценарий",
            source_id=source.id,
        )
    )

    assert without_source.source_id is None
    assert without_source.current_version_id is None
    assert with_source.source_id == source.id
    assert _run(repository.get_artifact(owner_id, with_source.id)) == with_source
    assert _run(repository.list_artifacts(owner_id)) == [with_source, without_source]


def test_artifact_rejects_source_from_another_workspace(tmp_path: Path) -> None:
    repository, _, owner_id, other_id = _setup(tmp_path)
    source = _run(
        repository.create_source(
            other_id,
            source_type="text",
            title="Чужой источник",
            original_text="Данные другого workspace",
        )
    )

    with pytest.raises(ValueError, match="не принадлежит"):
        _run(
            repository.create_artifact(
                owner_id,
                artifact_type="post",
                title="Недопустимый материал",
                source_id=source.id,
            )
        )
    with pytest.raises(ValueError, match="не принадлежит"):
        _run(
            repository.create_artifact(
                owner_id,
                artifact_type="post",
                title="Источник не существует",
                source_id=999,
            )
        )
    assert _run(repository.list_artifacts(owner_id)) == []


def test_artifact_reads_and_status_updates_are_workspace_scoped(
    tmp_path: Path,
) -> None:
    repository, _, owner_id, other_id = _setup(tmp_path)
    artifact = _run(
        repository.create_artifact(
            owner_id, artifact_type="faq", title="Вопросы", status="draft"
        )
    )

    assert _run(repository.get_artifact(other_id, artifact.id)) is None
    assert _run(repository.list_artifacts(other_id)) == []
    assert _run(
        repository.update_artifact_status(other_id, artifact.id, "ready")
    ) is None
    assert _run(repository.get_artifact(owner_id, artifact.id)).status == "draft"

    updated = _run(
        repository.update_artifact_status(owner_id, artifact.id, "ready")
    )
    assert updated is not None
    assert updated.status == "ready"
    assert _run(repository.list_artifacts(owner_id, status="ready")) == [updated]
    assert _run(repository.list_artifacts(owner_id, status="draft")) == []
    with pytest.raises(ValueError, match="limit"):
        _run(repository.list_artifacts(owner_id, limit=-1))
    with pytest.raises(ValueError, match="title"):
        _run(
            repository.create_artifact(
                owner_id, artifact_type="post", title="  "
            )
        )


def test_versions_are_sequential_and_current_version_is_updated(tmp_path: Path) -> None:
    repository, _, owner_id, _ = _setup(tmp_path)
    artifact = _run(
        repository.create_artifact(
            owner_id, artifact_type="post", title="Версионный пост"
        )
    )

    first = _run(
        repository.add_artifact_version(owner_id, artifact.id, "Версия 1")
    )
    second = _run(
        repository.add_artifact_version(
            owner_id, artifact.id, "Версия 2", generation_note="Доработано"
        )
    )
    third = _run(
        repository.add_artifact_version(owner_id, artifact.id, "Версия 3")
    )

    assert first is not None and first.version_number == 1
    assert second is not None and second.version_number == 2
    assert third is not None and third.version_number == 3
    assert second.generation_note == "Доработано"
    assert _run(repository.list_artifact_versions(owner_id, artifact.id)) == [
        first,
        second,
        third,
    ]
    assert _run(repository.get_artifact_version(owner_id, artifact.id, 1)) == first
    assert _run(repository.get_current_artifact_version(owner_id, artifact.id)) == third
    assert _run(repository.get_artifact(owner_id, artifact.id)).current_version_id == third.id
    with pytest.raises(ValueError, match="content"):
        _run(repository.add_artifact_version(owner_id, artifact.id, "  \t"))


def test_create_artifact_and_initial_version_is_atomic_and_source_linked(tmp_path: Path) -> None:
    repository, _, owner_id, other_id = _setup(tmp_path)
    source = _run(repository.create_source(
        owner_id, source_type="text", title="Источник", original_text="Текст"
    ))
    artifact, version = _run(repository.create_artifact_with_initial_version(
        owner_id, artifact_type="post", title="Пост", content="Черновик",
        source_id=source.id, generation_note="factory",
    ))
    assert artifact.source_id == source.id
    assert version.version_number == 1 and version.content == "Черновик"
    assert artifact.current_version_id == version.id
    assert _run(repository.get_current_artifact_version(owner_id, artifact.id)) == version
    with pytest.raises(ValueError, match="не принадлежит"):
        _run(repository.create_artifact_with_initial_version(
            other_id, artifact_type="post", title="Чужой", content="Нет",
            source_id=source.id,
        ))
    assert _run(repository.list_artifacts(other_id)) == []


def test_versions_are_hidden_from_other_workspace(tmp_path: Path) -> None:
    repository, _, owner_id, other_id = _setup(tmp_path)
    artifact = _run(
        repository.create_artifact(
            owner_id, artifact_type="stories", title="Истории"
        )
    )
    version = _run(
        repository.add_artifact_version(owner_id, artifact.id, "Содержание")
    )

    assert version is not None
    assert _run(repository.add_artifact_version(other_id, artifact.id, "Чужое")) is None
    assert _run(repository.get_artifact_version(other_id, artifact.id, 1)) is None
    assert _run(repository.list_artifact_versions(other_id, artifact.id)) == []
    assert _run(repository.get_current_artifact_version(other_id, artifact.id)) is None


def test_version_insert_rolls_back_when_current_update_fails(tmp_path: Path) -> None:
    repository, _, owner_id, _ = _setup(tmp_path)
    artifact = _run(
        repository.create_artifact(
            owner_id, artifact_type="post", title="Проверка rollback"
        )
    )
    first = _run(
        repository.add_artifact_version(owner_id, artifact.id, "Сохранённая версия")
    )
    assert first is not None
    with sqlite3.connect(repository.db_path) as db:
        db.execute(
            "CREATE TRIGGER fail_current_version "
            "BEFORE UPDATE OF current_version_id ON artifacts "
            "BEGIN SELECT RAISE(ABORT, 'forced failure'); END"
        )

    with pytest.raises(sqlite3.IntegrityError):
        _run(repository.add_artifact_version(owner_id, artifact.id, "Не сохранится"))

    assert _run(repository.list_artifact_versions(owner_id, artifact.id)) == [first]
    assert _run(repository.get_artifact(owner_id, artifact.id)).current_version_id == first.id
    assert _run(repository.get_current_artifact_version(owner_id, artifact.id)) == first
