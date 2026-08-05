from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app.repositories.partner_repository import PartnerRepository
from app.storage import Journal


OWNER_ID = 586249067
OTHER_ID = 111222333


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
    assert {"partner_workspaces", "partner_profiles"} <= tables


def test_existing_journal_data_is_preserved(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    journal = Journal(repository.db_path)
    _run(journal.init())
    entry_id = _run(journal.add("Задача", "content", (), "low"))

    _run(repository.init())

    entry = _run(journal.last())
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
