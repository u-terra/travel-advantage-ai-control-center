from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.partner_repository import PartnerRepository


def run(value):
    return asyncio.run(value)


def test_review_save_appends_version_and_rejects_stale_current(tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    partners = PartnerRepository(db)
    artifacts = ArtifactRepository(db)
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))
    run(artifacts.init())
    artifact, first = run(artifacts.create_artifact_with_initial_version(
        workspace.id, artifact_type="post", title="Текст", content="Версия 1"
    ))

    second = run(artifacts.add_artifact_version_if_current(
        workspace.id, artifact.id, first.id, "Версия 2", "review"
    ))
    assert second is not None and second.version_number == 2
    assert run(artifacts.get_current_artifact_version(workspace.id, artifact.id)) == second
    assert [item.content for item in run(
        artifacts.list_artifact_versions(workspace.id, artifact.id)
    )] == ["Версия 1", "Версия 2"]

    stale = run(artifacts.add_artifact_version_if_current(
        workspace.id, artifact.id, first.id, "Затирание"
    ))
    assert stale is None
    assert len(run(artifacts.list_artifact_versions(workspace.id, artifact.id))) == 2


def test_review_save_is_tenant_scoped(tmp_path: Path) -> None:
    db = tmp_path / "tenant.db"
    partners = PartnerRepository(db)
    artifacts = ArtifactRepository(db)
    run(partners.init())
    owner, _ = run(partners.ensure_owner_workspace(100))
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db) as connection:
        cursor = connection.execute(
            "INSERT INTO partner_workspaces (name, slug, status, created_at, updated_at) "
            "VALUES ('Other', 'other-review', 'active', ?, ?)", (now, now)
        )
        other_id = cursor.lastrowid
    run(artifacts.init())
    artifact, first = run(artifacts.create_artifact_with_initial_version(
        owner.id, artifact_type="post", title="Текст", content="Версия 1"
    ))
    assert run(artifacts.add_artifact_version_if_current(
        other_id, artifact.id, first.id, "Чужая версия"
    )) is None
