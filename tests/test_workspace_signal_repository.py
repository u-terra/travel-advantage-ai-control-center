from __future__ import annotations

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.repositories.partner_repository import PartnerRepository
from app.repositories.source_catalog_repository import SourceCatalogRepository
from app.repositories.workspace_signal_repository import WorkspaceSignalRepository


def run(coro):
    return asyncio.run(coro)


def create_radar(path: Path, rows: list[tuple]) -> None:
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE lead_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT, created_at TEXT,
            source_type TEXT, origin_type TEXT, source_name TEXT, source_url TEXT,
            item_url TEXT UNIQUE, item_title TEXT, item_summary TEXT, published_at TEXT,
            status TEXT DEFAULT 'new', ai_score REAL, ai_category TEXT, ai_reason TEXT,
            suggested_message TEXT, notes TEXT, llm_checked INTEGER DEFAULT 0,
            llm_checked_at TEXT, llm_signal_type TEXT, llm_score REAL,
            llm_relevance TEXT, llm_reason TEXT, llm_suggested_message TEXT
        )""")
        db.executemany(
            "INSERT INTO lead_signals(id, source_id, created_at, source_type, origin_type, "
            "item_url, item_title, item_summary, status, notes, ai_score, ai_category, "
            "ai_reason, suggested_message, llm_checked, llm_checked_at, llm_signal_type, "
            "llm_score, llm_relevance, llm_reason, llm_suggested_message) "
            "VALUES (?, ?, '2025-01-01', 'rss', 'publisher_post', ?, ?, 'summary', "
            "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows,
        )


def setup(tmp_path: Path):
    app_db = tmp_path / "app.sqlite3"
    radar_db = tmp_path / "radar.sqlite3"
    partner = PartnerRepository(app_db)
    run(partner.init())
    owner = run(partner.bootstrap_owner_membership(100))
    assert owner is not None
    catalog = SourceCatalogRepository(app_db, tmp_path / "sources.json")
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"schema_version": 2, "sources": []}), encoding="utf-8")
    run(catalog.init(owner.workspace_id, legacy_path=legacy))
    return app_db, radar_db, owner.workspace_id, partner, catalog


def workspace(app_db: Path, telegram_id: int) -> int:
    with sqlite3.connect(app_db) as db:
        cursor = db.execute(
            "INSERT INTO partner_workspaces(name, slug, status, created_at, updated_at) "
            "VALUES (?, ?, 'active', 'x', 'x')", (str(telegram_id), str(telegram_id)),
        )
        workspace_id = int(cursor.lastrowid)
        db.execute(
            "INSERT INTO workspace_memberships(workspace_id, telegram_user_id, role, "
            "status, created_at, updated_at) VALUES (?, ?, 'owner', 'active', 'x', 'x')",
            (workspace_id, telegram_id),
        )
        return workspace_id


def raw(source_id: str | None, row_id: int = 1, **values):
    defaults = dict(status="review", notes="note", score=73.0, category="market_signal",
                    reason="reason", suggested="draft", checked=1, checked_at="checked",
                    signal_type="type", llm_score=81.0, relevance="high",
                    llm_reason="llm reason", llm_message="llm draft")
    defaults.update(values)
    return (row_id, source_id, f"https://item/{row_id}", f"title {row_id}",
            defaults["status"], defaults["notes"], defaults["score"], defaults["category"],
            defaults["reason"], defaults["suggested"], defaults["checked"],
            defaults["checked_at"], defaults["signal_type"], defaults["llm_score"],
            defaults["relevance"], defaults["llm_reason"], defaults["llm_message"])


def test_schema_constraints_and_exact_legacy_owner_backfill(tmp_path: Path) -> None:
    app_db, radar_db, owner, _, _ = setup(tmp_path)
    other = workspace(app_db, 200)
    create_radar(radar_db, [raw(None), raw(None, 2)])
    repo = WorkspaceSignalRepository(app_db, radar_db)
    run(repo.init(owner))
    run(repo.init(other))
    with sqlite3.connect(app_db) as db:
        columns = {row[1]: row for row in db.execute(
            "PRAGMA table_info(workspace_signal_interpretations)"
        )}
        rows = db.execute(
            "SELECT workspace_id, radar_signal_id, usage_role_snapshot, status, notes, "
            "ai_score, ai_category, ai_reason, suggested_message, llm_checked, llm_checked_at, "
            "llm_signal_type, llm_score, llm_relevance, llm_reason, llm_suggested_message "
            "FROM workspace_signal_interpretations ORDER BY radar_signal_id"
        ).fetchall()
        assert columns["workspace_id"][3] == 1
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT COUNT(*) FROM workspace_signal_migrations").fetchone()[0] == 1
        db.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO workspace_signal_interpretations "
                "(workspace_id, radar_signal_id, usage_role_snapshot, created_at, updated_at) "
                "VALUES (99999, 1, NULL, 'x', 'x')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO workspace_signal_interpretations "
                "(workspace_id, radar_signal_id, usage_role_snapshot, created_at, updated_at) "
                "VALUES (?, 999, 'invalid', 'x', 'x')", (owner,)
            )
    assert len(rows) == 2 and {row[0] for row in rows} == {owner}
    assert rows[0][1:] == (1, None, "review", "note", 73.0, "market_signal", "reason",
                           "draft", 1, "checked", "type", 81.0, "high", "llm reason", "llm draft")


def test_bridge_isolates_workspaces_and_snapshots_roles(tmp_path: Path) -> None:
    app_db, radar_db, owner, _, catalog = setup(tmp_path)
    other = workspace(app_db, 200)
    outsider = workspace(app_db, 300)
    source = run(catalog.add_source(owner, "https://example.com/feed", "competitor")).source
    run(catalog.add_source(other, "https://example.com/feed", "monitoring"))
    create_radar(radar_db, [raw(source.id, suggested="owner-only", llm_message="owner llm")])
    repo = WorkspaceSignalRepository(app_db, radar_db)
    run(repo.init(None))
    assert run(repo.sync_eligible()) == 2
    assert run(repo.sync_eligible()) == 0
    a = run(repo.list_for_workspace(owner))
    b = run(repo.list_for_workspace(other))
    assert len(a) == len(b) == 1 and run(repo.list_for_workspace(outsider)) == []
    assert a[0].usage_role_snapshot == "competitor"
    assert b[0].usage_role_snapshot == "monitoring"
    assert a[0].suggested_message is None and b[0].suggested_message is None
    assert run(repo.get_for_workspace(owner, b[0].interpretation_id)) is None
    run(catalog.set_usage_role(owner, source.id, "monitoring"))
    assert run(repo.get_for_workspace(owner, a[0].interpretation_id)).usage_role_snapshot == "competitor"


def test_disabled_inactive_and_null_source_are_not_assigned(tmp_path: Path) -> None:
    app_db, radar_db, owner, _, catalog = setup(tmp_path)
    enabled = run(catalog.add_source(owner, "https://example.com/enabled")).source
    disabled = run(catalog.add_source(owner, "https://example.com/disabled")).source
    inactive = run(catalog.add_source(owner, "https://example.com/inactive")).source
    run(catalog.set_enabled(owner, disabled.id, False))
    with sqlite3.connect(app_db) as db:
        db.execute("UPDATE source_catalog SET status='inactive' WHERE id=?", (inactive.id,))
    create_radar(radar_db, [raw(enabled.id), raw(disabled.id, 2), raw(inactive.id, 3), raw(None, 4)])
    repo = WorkspaceSignalRepository(app_db, radar_db)
    run(repo.init(None))
    run(repo.sync_eligible())
    assert [row.radar_signal_id for row in run(repo.list_for_workspace(owner))] == [1]


def test_concurrent_bridge_does_not_duplicate(tmp_path: Path) -> None:
    app_db, radar_db, owner, _, catalog = setup(tmp_path)
    source = run(catalog.add_source(owner, "https://example.com/feed")).source
    create_radar(radar_db, [raw(source.id)])
    repo = WorkspaceSignalRepository(app_db, radar_db)
    run(repo.init(None))
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: run(repo.sync_eligible()), range(2)))
    with sqlite3.connect(app_db) as db:
        assert db.execute("SELECT COUNT(*) FROM workspace_signal_interpretations").fetchone()[0] == 1
