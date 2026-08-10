from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.repositories.partner_repository import PartnerRepository
from app.storage import Journal


LEGACY_SCHEMA = """
CREATE TABLE journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    task_text TEXT NOT NULL,
    primary_module TEXT NOT NULL,
    secondary_modules TEXT NOT NULL,
    safety_level TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    note TEXT NOT NULL DEFAULT ''
);
"""


def run(coro):
    return asyncio.run(coro)


def setup_workspaces(path: Path, count: int = 2) -> list[int]:
    run(PartnerRepository(path).init())
    ids = []
    with sqlite3.connect(path) as db:
        for index in range(count):
            cursor = db.execute(
                "INSERT INTO partner_workspaces "
                "(name, slug, status, created_at, updated_at) "
                "VALUES (?, ?, 'active', 'now', 'now')",
                (f"Workspace {index}", f"workspace-{index}"),
            )
            ids.append(cursor.lastrowid)
    return ids


def create_legacy_journal(path: Path) -> list[tuple]:
    rows = [
        (3, "2025-01-01T00:00:00+00:00", "Первая", "content", "", "low", "done", "note"),
        (8, "2025-02-02T03:04:05+00:00", "Вторая", "travel", "safety", "high", "new", ""),
    ]
    with sqlite3.connect(path) as db:
        db.executescript(LEGACY_SCHEMA)
        db.executemany(
            "INSERT INTO journal "
            "(id, created_at, task_text, primary_module, secondary_modules, "
            "safety_level, status, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    return rows


def test_schema_has_required_workspace_fk_and_index(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    workspace_id = setup_workspaces(path, 1)[0]
    journal = Journal(path)
    run(journal.init(workspace_id))

    with sqlite3.connect(path) as db:
        columns = {row[1]: row for row in db.execute("PRAGMA table_info(journal)")}
        assert columns["workspace_id"][3] == 1
        foreign_keys = list(db.execute("PRAGMA foreign_key_list(journal)"))
        assert any(row[2:5] == ("partner_workspaces", "workspace_id", "id") for row in foreign_keys)
        indexes = list(db.execute("PRAGMA index_list(journal)"))
        index_name = next(row[1] for row in indexes if row[1] == "idx_journal_workspace_id")
        assert [row[2] for row in db.execute(f"PRAGMA index_info({index_name})")] == [
            "workspace_id", "id"
        ]


def test_new_database_initializes_without_legacy_owner(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    run(Journal(path).init(None))
    with sqlite3.connect(path) as db:
        columns = [row[1] for row in db.execute("PRAGMA table_info(journal)")]
    assert "workspace_id" in columns


def test_invalid_workspace_is_rejected_by_foreign_key(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    workspace_id = setup_workspaces(path, 1)[0]
    journal = Journal(path)
    run(journal.init(workspace_id))
    with pytest.raises(sqlite3.IntegrityError):
        run(journal.add(999, "Задача", "content", (), "low"))


def test_add_and_last_are_strictly_workspace_scoped(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    first, second, empty = setup_workspaces(path, 3)
    journal = Journal(path)
    run(journal.init(first))

    first_id = run(journal.add(first, "A", "content", (), "low"))
    second_id = run(journal.add(second, "B", "travel", ("safety",), "high"))

    first_entry = run(journal.last(first))
    second_entry = run(journal.last(second))
    assert first_entry is not None and first_entry.id == first_id
    assert first_entry.workspace_id == first and first_entry.task_text == "A"
    assert second_entry is not None and second_entry.id == second_id
    assert second_entry.workspace_id == second and second_entry.task_text == "B"
    assert run(journal.last(empty)) is None


def test_legacy_migration_preserves_every_field_and_assigns_exact_workspace(
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal.sqlite3"
    first, owner = setup_workspaces(path, 2)
    legacy_rows = create_legacy_journal(path)
    journal = Journal(path)

    run(journal.init(owner))

    with sqlite3.connect(path) as db:
        rows = list(db.execute(
            "SELECT id, created_at, task_text, primary_module, secondary_modules, "
            "safety_level, status, note, workspace_id FROM journal ORDER BY id"
        ))
        assert [row[:8] for row in rows] == legacy_rows
        assert {row[8] for row in rows} == {owner}
        assert first not in {row[8] for row in rows}
        assert list(db.execute("PRAGMA foreign_key_check")) == []

    new_id = run(journal.add(owner, "Третья", "content", (), "low"))
    assert new_id > max(row[0] for row in legacy_rows)


def test_repeated_init_is_idempotent_and_never_reassigns_rows(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    owner, other = setup_workspaces(path, 2)
    create_legacy_journal(path)
    journal = Journal(path)
    run(journal.init(owner))
    before = run(journal.last(owner))

    run(journal.init(other))

    assert run(journal.last(owner)) == before
    assert run(journal.last(other)) is None


def test_tenant_aware_journal_initializes_without_legacy_owner(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    owner = setup_workspaces(path, 1)[0]
    journal = Journal(path)
    run(journal.init(owner))
    entry_id = run(journal.add(owner, "Задача", "content", (), "low"))

    run(journal.init(None))

    assert run(journal.last(owner)).id == entry_id


def test_legacy_journal_without_owner_fails_without_changing_data(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    setup_workspaces(path, 1)
    legacy_rows = create_legacy_journal(path)

    with pytest.raises(RuntimeError, match="требуется workspace владельца"):
        run(Journal(path).init(None))

    with sqlite3.connect(path) as db:
        columns = [row[1] for row in db.execute("PRAGMA table_info(journal)")]
        rows = list(db.execute(
            "SELECT id, created_at, task_text, primary_module, secondary_modules, "
            "safety_level, status, note FROM journal ORDER BY id"
        ))
    assert "workspace_id" not in columns
    assert rows == legacy_rows


def test_invalid_migration_target_keeps_legacy_table_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    setup_workspaces(path, 1)
    legacy_rows = create_legacy_journal(path)

    with pytest.raises(ValueError):
        run(Journal(path).init(999))

    with sqlite3.connect(path) as db:
        columns = [row[1] for row in db.execute("PRAGMA table_info(journal)")]
        rows = list(db.execute(
            "SELECT id, created_at, task_text, primary_module, secondary_modules, "
            "safety_level, status, note FROM journal ORDER BY id"
        ))
    assert "workspace_id" not in columns
    assert rows == legacy_rows


def test_migration_error_rolls_back_without_destroying_legacy_data(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    owner = setup_workspaces(path, 1)[0]
    legacy_rows = create_legacy_journal(path)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE journal_tenant_migration (id INTEGER)")

    with pytest.raises(sqlite3.OperationalError):
        run(Journal(path).init(owner))

    with sqlite3.connect(path) as db:
        columns = [row[1] for row in db.execute("PRAGMA table_info(journal)")]
        rows = list(db.execute(
            "SELECT id, created_at, task_text, primary_module, secondary_modules, "
            "safety_level, status, note FROM journal ORDER BY id"
        ))
    assert "workspace_id" not in columns
    assert rows == legacy_rows


def test_startup_does_not_require_owner_for_tenant_aware_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import main

    class Partner:
        def __init__(self, _path):
            pass

        async def init(self):
            pass

        async def bootstrap_owner_membership(self, _telegram_user_id):
            return None

    journal = SimpleNamespace(init=AsyncMock())
    repository = SimpleNamespace(init=AsyncMock(), initialize=AsyncMock())
    dispatcher = SimpleNamespace(start_polling=AsyncMock())
    settings = SimpleNamespace(
        journal_db_path=tmp_path / "db.sqlite3",
        admin_telegram_id=1,
        log_level="INFO",
        sources_registry_path=tmp_path / "sources.json",
        content_factory_url="https://example.test",
        content_factory_token="token",
        content_factory_timeout_seconds=1.0,
        content_factory_source_analysis_url="https://example.test/analyze",
        llm_provider="fake",
        lead_radar_db_path=tmp_path / "radar.sqlite3",
        bot_token="token",
        allowed_user_ids=frozenset(),
        v2_menu_enabled=False,
    )
    monkeypatch.setattr(main, "load_settings", lambda: settings)
    monkeypatch.setattr(main, "PartnerRepository", Partner)
    monkeypatch.setattr(main, "Journal", lambda _path: journal)
    monkeypatch.setattr(main, "ArtifactRepository", lambda _path: repository)
    monkeypatch.setattr(main, "SourceAnalysisRepository", lambda _path: repository)
    monkeypatch.setattr(
        main,
        "SourceRegistryStore",
        lambda _path: SimpleNamespace(ensure_bootstrapped=lambda: None),
    )
    monkeypatch.setattr(main, "create_llm_provider", lambda *args, **kwargs: object())
    monkeypatch.setattr(main, "Bot", lambda _token: object())
    monkeypatch.setattr(main, "_build_dispatcher", lambda *args, **kwargs: dispatcher)

    run(main._async_main())

    journal.init.assert_awaited_once_with(None)
    dispatcher.start_polling.assert_awaited_once()
