from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app.repositories.partner_repository import (
    AmbiguousWorkspaceError,
    OwnerMembershipConflictError,
    PartnerRepository,
)
from app.storage import Journal


OWNER_ID = 586249067
OTHER_ID = 111222333
THIRD_ID = 444555666


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _repository(tmp_path: Path) -> PartnerRepository:
    return PartnerRepository(tmp_path / "workspace.sqlite3")


def test_init_creates_tables_in_empty_database(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())

    with sqlite3.connect(repository.db_path) as db:
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {"partner_workspaces", "partner_profiles", "workspace_memberships"} <= tables


def test_existing_journal_data_is_preserved(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    workspace, _ = _run(repository.ensure_owner_workspace(OWNER_ID))
    journal = Journal(repository.db_path)
    _run(journal.init(workspace.id))
    entry_id = _run(journal.add(workspace.id, "Задача", "content", (), "low"))

    _run(repository.init())

    entry = _run(journal.last(workspace.id))
    assert entry is not None
    assert entry.id == entry_id
    assert entry.task_text == "Задача"


def test_owner_bootstrap_is_idempotent_and_linked(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())

    workspace, profile = _run(repository.ensure_owner_workspace(OWNER_ID))
    repeated_workspace, repeated_profile = _run(
        repository.ensure_owner_workspace(OWNER_ID)
    )

    assert repeated_workspace == workspace
    assert repeated_profile == profile
    assert profile.workspace_id == workspace.id
    assert profile.telegram_user_id == OWNER_ID
    assert _run(repository.get_workspace(workspace.id)) == workspace
    assert _run(repository.get_profile(workspace.id)) == profile
    assert _run(repository.find_workspace_by_telegram_id(OWNER_ID)) == workspace

    with sqlite3.connect(repository.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM partner_workspaces").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM partner_profiles").fetchone()[0] == 1


def _insert_workspace(db_path: Path, slug: str, status: str = "active") -> int:
    with sqlite3.connect(db_path) as db:
        cursor = db.execute(
            "INSERT INTO partner_workspaces "
            "(name, slug, status, created_at, updated_at) VALUES (?, ?, ?, 'now', 'now')",
            (slug, slug, status),
        )
        return cursor.lastrowid


def test_membership_constraints_and_one_user_in_two_workspaces(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    first = _insert_workspace(repository.db_path, "first")
    second = _insert_workspace(repository.db_path, "second")

    one = _run(repository.create_membership(first, OWNER_ID, role="owner"))
    two = _run(repository.create_membership(second, OWNER_ID, role="member"))
    assert [one.workspace_id, two.workspace_id] == [first, second]

    with pytest.raises(sqlite3.IntegrityError):
        _run(repository.create_membership(first, OWNER_ID, role="admin"))
    with pytest.raises(sqlite3.IntegrityError):
        _run(repository.create_membership(999, OTHER_ID, role="member"))
    with pytest.raises(ValueError):
        _run(repository.create_membership(first, OTHER_ID, role="superuser"))
    with pytest.raises(ValueError):
        _run(repository.create_membership(first, OTHER_ID, role="member", status="pending"))

    with sqlite3.connect(repository.db_path) as db:
        for column, value in (("role", "superuser"), ("status", "pending")):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(
                    f"INSERT INTO workspace_memberships "
                    f"(workspace_id, telegram_user_id, role, status, created_at, updated_at) "
                    f"VALUES (?, ?, ?, ?, 'now', 'now')",
                    (first, OTHER_ID, value if column == "role" else "member",
                     value if column == "status" else "active"),
                )


def test_owner_backfill_is_idempotent_and_uses_profile_workspace(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    workspace, _ = _run(repository.ensure_owner_workspace(OWNER_ID))
    first = _run(repository.bootstrap_owner_membership(OWNER_ID))
    repeated = _run(repository.bootstrap_owner_membership(OWNER_ID))
    assert first == repeated
    assert first is not None
    assert first.workspace_id == workspace.id
    assert first.role == "owner"
    assert first.status == "active"


@pytest.mark.parametrize(
    ("role", "status"),
    (("member", "active"), ("admin", "active"), ("owner", "inactive")),
)
def test_owner_backfill_rejects_conflicting_existing_membership(
    tmp_path: Path, role: str, status: str
) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    workspace, _ = _run(repository.ensure_owner_workspace(OWNER_ID))
    existing = _run(
        repository.create_membership(
            workspace.id, OWNER_ID, role=role, status=status
        )
    )

    with pytest.raises(OwnerMembershipConflictError):
        _run(repository.bootstrap_owner_membership(OWNER_ID))

    unchanged = _run(repository.get_membership(workspace.id, OWNER_ID))
    assert unchanged == existing


def test_owner_backfill_never_uses_first_workspace_without_profile(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    _insert_workspace(repository.db_path, "unrelated")
    assert _run(repository.bootstrap_owner_membership(OWNER_ID)) is None
    assert _run(repository.list_memberships_by_telegram_id(OWNER_ID)) == []


def test_empty_database_bootstrap_creates_initial_owner_boundary(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    membership = _run(repository.bootstrap_owner_membership(OWNER_ID))
    assert membership is not None
    assert membership.role == "owner"
    assert _run(repository.find_workspace_by_telegram_id(OWNER_ID)) is not None


def test_workspace_context_resolution_states(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    active = _insert_workspace(repository.db_path, "active")
    inactive_workspace = _insert_workspace(repository.db_path, "inactive", "inactive")

    assert _run(repository.resolve_workspace_context(OWNER_ID)) is None
    _run(repository.create_membership(active, OWNER_ID, role="member", status="inactive"))
    assert _run(repository.resolve_workspace_context(OWNER_ID)) is None
    _run(repository.create_membership(inactive_workspace, THIRD_ID, role="admin"))
    assert _run(repository.resolve_workspace_context(THIRD_ID)) is None

    second_active = _insert_workspace(repository.db_path, "second-active")
    third_active = _insert_workspace(repository.db_path, "third-active")
    _run(repository.create_membership(second_active, OTHER_ID, role="admin"))
    context = _run(repository.resolve_workspace_context(OTHER_ID))
    assert context is not None
    assert context.workspace_id == second_active
    assert context.workspace_status == "active"
    _run(repository.create_membership(third_active, OTHER_ID, role="member"))
    with pytest.raises(AmbiguousWorkspaceError):
        _run(repository.resolve_workspace_context(OTHER_ID))


def test_different_telegram_id_cannot_read_owner_workspace(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    workspace, _ = _run(repository.ensure_owner_workspace(OWNER_ID))

    assert _run(repository.find_workspace_by_telegram_id(OTHER_ID)) is None
    assert _run(repository.find_workspace_by_telegram_id(OWNER_ID)) == workspace


def test_second_telegram_id_conflicts_without_changing_owner_data(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    workspace, profile = _run(repository.ensure_owner_workspace(OWNER_ID))

    with pytest.raises(sqlite3.IntegrityError):
        _run(repository.ensure_owner_workspace(OTHER_ID))

    assert _run(repository.get_profile(workspace.id)) == profile
    assert _run(repository.find_workspace_by_telegram_id(OTHER_ID)) is None
    with sqlite3.connect(repository.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM partner_workspaces").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM partner_profiles").fetchone()[0] == 1


def test_foreign_key_rejects_profile_without_workspace(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())

    with sqlite3.connect(repository.db_path) as db:
        db.execute("PRAGMA foreign_keys = ON")
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO partner_profiles "
                "(workspace_id, telegram_user_id, partner_name, project_name, "
                "business_description, communication_style, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (999, OWNER_ID, "Владелец", "Проект", "Описание", "Стиль", "now", "now"),
            )


def test_profile_can_be_updated_for_future_stages(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    workspace, _ = _run(repository.ensure_owner_workspace(OWNER_ID))

    profile = _run(
        repository.update_profile(
            workspace.id,
            partner_name="Анна",
            project_name="Путешествия с Анной",
            business_description="Помощь путешественникам.",
            communication_style="Дружелюбный",
        )
    )

    assert profile is not None
    assert profile.workspace_id == workspace.id
    assert profile.partner_name == "Анна"
    assert profile.communication_style == "Дружелюбный"

    repeated_workspace, repeated_profile = _run(
        repository.ensure_owner_workspace(OWNER_ID)
    )
    assert repeated_workspace == workspace
    assert repeated_profile == profile
