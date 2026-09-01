from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from app.repositories.partner_repository import PartnerRepository
from app.repositories.workspace_memory_repository import WorkspaceMemoryRepository


def _run(coro):
    return asyncio.run(coro)


def _repository(tmp_path: Path) -> WorkspaceMemoryRepository:
    return WorkspaceMemoryRepository(tmp_path / "journal.sqlite3")


def _existing_workspace(tmp_path: Path) -> int:
    partner = PartnerRepository(tmp_path / "journal.sqlite3")
    _run(partner.init())
    workspace, _ = _run(partner.ensure_owner_workspace(586249067))
    return workspace.id


def test_init_creates_table_additively(tmp_path: Path) -> None:
    workspace_id = _existing_workspace(tmp_path)
    repository = _repository(tmp_path)
    _run(repository.init())

    with sqlite3.connect(repository.db_path) as db:
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "workspace_memory" in tables
    # additive: не трогает уже существующие таблицы того же journal.sqlite3
    assert {"partner_workspaces", "partner_profiles"} <= tables
    assert _run(repository.get(workspace_id)) is None


def test_init_is_idempotent_on_existing_table(tmp_path: Path) -> None:
    _existing_workspace(tmp_path)
    repository = _repository(tmp_path)
    _run(repository.init())
    _run(repository.set_summary(1, "Первая сводка"))

    _run(repository.init())  # повторный init не должен ничего сломать/стереть

    record = _run(repository.get(1))
    assert record is not None
    assert record.summary == "Первая сводка"


def test_set_summary_then_get_round_trip(tmp_path: Path) -> None:
    workspace_id = _existing_workspace(tmp_path)
    repository = _repository(tmp_path)
    _run(repository.init())

    record = _run(repository.set_summary(workspace_id, "  Проект: веб-чат.  "))

    assert record.workspace_id == workspace_id
    assert record.summary == "Проект: веб-чат."
    assert record.created_at and record.updated_at

    fetched = _run(repository.get(workspace_id))
    assert fetched == record


def test_set_summary_upserts_single_row_per_workspace(tmp_path: Path) -> None:
    workspace_id = _existing_workspace(tmp_path)
    repository = _repository(tmp_path)
    _run(repository.init())

    first = _run(repository.set_summary(workspace_id, "Версия 1"))
    second = _run(repository.set_summary(workspace_id, "Версия 2"))

    assert first.created_at == second.created_at
    assert second.summary == "Версия 2"

    with sqlite3.connect(repository.db_path) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM workspace_memory WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()[0]
    assert count == 1


def test_unknown_workspace_id_is_rejected_by_foreign_key(tmp_path: Path) -> None:
    _existing_workspace(tmp_path)
    repository = _repository(tmp_path)
    _run(repository.init())

    with pytest.raises(sqlite3.IntegrityError):
        _run(repository.set_summary(99999, "Не существует"))


def test_get_missing_workspace_returns_none(tmp_path: Path) -> None:
    workspace_id = _existing_workspace(tmp_path)
    repository = _repository(tmp_path)
    _run(repository.init())

    assert _run(repository.get(workspace_id)) is None
