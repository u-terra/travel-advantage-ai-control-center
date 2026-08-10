from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.repositories.partner_repository import PartnerRepository
from app.repositories.source_catalog_repository import (
    SourceCatalogMigrationError,
    SourceCatalogRepository,
)
from app.services.source_registry_store import UnknownSourceError


def run(value):
    return asyncio.run(value)


def legacy_file(path: Path, sources: list[dict] | None = None) -> Path:
    payload = {"schema_version": 2, "sources": sources or []}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def legacy_source(source_id="legacy", *, enabled=True, platform="telegram"):
    address = (
        {"username": "public_channel", "url": "https://t.me/public_channel"}
        if platform == "telegram"
        else {"url": "https://example.com/feed"}
    )
    return {
        "id": source_id,
        "name": "Legacy name",
        "platform": platform,
        "source_type": "monitored_source",
        "purpose": "market",
        "enabled": enabled,
        "priority": 17,
        "notes": "legacy notes",
        "collector": {"fetch_limit": 12, "drop_promo": True},
        "created_at": "2025-01-01T00:00:00+00:00",
        "updated_at": "2025-02-01T00:00:00+00:00",
        **address,
    }


def setup(tmp_path: Path, sources: list[dict] | None = None):
    db_path = tmp_path / "app.sqlite3"
    projection = tmp_path / "sources.json"
    legacy = legacy_file(tmp_path / "legacy.json", sources)
    partner = PartnerRepository(db_path)
    run(partner.init())
    owner = run(partner.bootstrap_owner_membership(100))
    assert owner is not None
    repo = SourceCatalogRepository(db_path, projection)
    run(repo.init(owner.workspace_id, legacy_path=legacy))
    return db_path, projection, partner, owner.workspace_id, repo


def new_workspace(db_path: Path, slug: str) -> int:
    with sqlite3.connect(db_path) as db:
        cursor = db.execute(
            "INSERT INTO partner_workspaces(name, slug, status, created_at, updated_at) "
            "VALUES (?, ?, 'active', 'now', 'now')", (slug, slug),
        )
        return int(cursor.lastrowid)


def test_schema_constraints_foreign_keys_and_legacy_metadata(tmp_path: Path) -> None:
    db, _, _, owner, _ = setup(
        tmp_path, [legacy_source("enabled"), legacy_source("disabled", enabled=False, platform="web")]
    )
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        catalog = conn.execute(
            "SELECT id, priority, notes, collector_json, visibility, status, "
            "created_at, updated_at FROM source_catalog ORDER BY id"
        ).fetchall()
        subscriptions = conn.execute(
            "SELECT source_id, enabled, usage_role FROM workspace_source_subscriptions "
            "WHERE workspace_id=? ORDER BY source_id", (owner,),
        ).fetchall()
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert {row[1] for row in conn.execute("PRAGMA index_list(source_catalog)")} >= {
            "sqlite_autoindex_source_catalog_1", "sqlite_autoindex_source_catalog_2"
        }
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO workspace_source_subscriptions "
                "(workspace_id, source_id, enabled, usage_role, created_at, updated_at) "
                "VALUES (9999, 'enabled', 1, 'monitoring', 'x', 'x')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO workspace_source_subscriptions "
                "(workspace_id, source_id, enabled, usage_role, created_at, updated_at) "
                "VALUES (?, 'enabled', 2, 'monitoring', 'x', 'x')", (owner,)
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO workspace_source_subscriptions "
                "(workspace_id, source_id, enabled, usage_role, created_at, updated_at) "
                "VALUES (?, 'enabled', 1, 'ranking', 'x', 'x')", (owner,)
            )
    assert catalog[0][1:] == (
        17, "legacy notes", '{"drop_promo": true, "fetch_limit": 12}',
        "platform", "active", "2025-01-01T00:00:00+00:00",
        "2025-02-01T00:00:00+00:00",
    )
    assert subscriptions == [("disabled", 0, "monitoring"), ("enabled", 1, "monitoring")]


def test_catalog_checks_visibility_private_owner_and_identity(tmp_path: Path) -> None:
    db, _, _, owner, _ = setup(tmp_path)
    values = (
        "id", "key", "name", "web", "https://example.com", None,
        "monitored_source", "mixed", 50, "", "{}", "active", "x", "x",
    )
    sql = (
        "INSERT INTO source_catalog(id, identity_key, name, platform, url, username, "
        "source_type, purpose, priority, notes, collector_json, visibility, "
        "owner_workspace_id, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, values[:11] + ("shared", None) + values[11:])
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, values[:11] + ("private", None) + values[11:])
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, values[:11] + ("private", 9999) + values[11:])
        conn.execute(sql, values[:11] + ("private", owner) + values[11:])
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, ("id2",) + values[1:11] + ("private", owner) + values[11:])


def test_shared_physical_source_has_isolated_subscription_state_and_role(tmp_path: Path) -> None:
    db, _, _, owner, repo = setup(tmp_path)
    other = new_workspace(db, "other")
    a_result = run(repo.add_source(owner, "https://example.com/competitor", "competitor"))
    b_result = run(repo.add_source(other, "https://example.com/competitor", "monitoring"))
    a, b = a_result.source, b_result.source
    assert a.id == b.id
    assert a.usage_role == "competitor"
    assert b.usage_role == "monitoring"

    run(repo.set_enabled(owner, a.id, False))
    assert run(repo.get_for_workspace(owner, a.id)).enabled is False
    assert run(repo.get_for_workspace(other, b.id)).enabled is True
    run(repo.set_usage_role(owner, a.id, "monitoring"))
    assert run(repo.get_for_workspace(other, b.id)).usage_role == "monitoring"
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM source_catalog WHERE identity_key='web:https://example.com/competitor'"
        ).fetchone()[0] == 1


def test_private_source_is_not_discoverable_until_exact_address_is_added(tmp_path: Path) -> None:
    db, _, _, owner, repo = setup(tmp_path)
    other = new_workspace(db, "other")
    created = run(repo.add_source(owner, "https://example.com/private", "competitor")).source
    assert run(repo.get_for_workspace(other, created.id)) is None
    assert created.id not in {item.id for item in run(repo.list_for_workspace(other))}

    reused_result = run(repo.add_source(other, "https://example.com/private", "monitoring"))
    reused = reused_result.source
    assert reused_result.outcome == "created"
    assert reused.id == created.id
    assert reused.name == "example.com"
    assert reused.notes == ""
    assert reused.usage_role == "monitoring"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_catalog").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM workspace_source_subscriptions WHERE source_id=?",
            (created.id,),
        ).fetchone()[0] == 2


def test_duplicate_add_reports_enabled_or_disabled_without_reenabling(tmp_path: Path) -> None:
    _, _, _, owner, repo = setup(tmp_path)
    created = run(repo.add_source(owner, "https://example.com/duplicate"))
    assert created.outcome == "created"
    enabled = run(repo.add_source(owner, "https://example.com/duplicate"))
    assert enabled.outcome == "already_enabled"

    run(repo.set_enabled(owner, created.source.id, False))
    disabled = run(repo.add_source(owner, "https://example.com/duplicate"))
    assert disabled.outcome == "already_disabled"
    assert disabled.source.enabled is False
    assert run(repo.get_for_workspace(owner, created.source.id)).enabled is False


def test_foreign_private_callback_and_invalid_role_fail_closed(tmp_path: Path) -> None:
    db, _, _, owner, repo = setup(tmp_path)
    other = new_workspace(db, "other")
    created = run(repo.add_source(owner, "https://example.com/private")).source
    with pytest.raises(UnknownSourceError):
        run(repo.toggle(other, created.id))
    with pytest.raises(ValueError):
        run(repo.add_source(other, "https://example.com/other", "invalid"))
    with pytest.raises(ValueError):
        run(repo.set_usage_role(owner, created.id, "invalid"))


def test_inactive_physical_source_cannot_gain_enabled_subscription(tmp_path: Path) -> None:
    db, _, _, owner, repo = setup(tmp_path)
    created = run(repo.add_source(owner, "https://example.com/inactive")).source
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE source_catalog SET status='inactive' WHERE id=?", (created.id,))
        conn.execute(
            "DELETE FROM workspace_source_subscriptions WHERE workspace_id=? AND source_id=?",
            (owner, created.id),
        )
    with pytest.raises(UnknownSourceError):
        run(repo.add_source(owner, "https://example.com/inactive"))
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM workspace_source_subscriptions "
            "WHERE workspace_id=? AND source_id=?", (owner, created.id),
        ).fetchone()[0] == 0


def test_web_competitor_is_stored_and_projection_contains_no_tenant_metadata(tmp_path: Path) -> None:
    _, projection, _, owner, repo = setup(tmp_path)
    created = run(repo.add_source(owner, "https://example.com/tours", "competitor")).source
    assert created.platform == "web" and created.usage_role == "competitor"
    payload = json.loads(projection.read_text(encoding="utf-8"))
    record = next(item for item in payload["sources"] if item["id"] == created.id)
    assert record["enabled"] is True
    assert set(record).isdisjoint(
        {"workspace_id", "owner_workspace_id", "usage_role", "competitor"}
    )
    assert record["platform"] == "web"


def test_projection_enabled_is_aggregate_and_source_exported_once(tmp_path: Path) -> None:
    db, projection, _, owner, repo = setup(tmp_path)
    other = new_workspace(db, "other")
    item = run(repo.add_source(owner, "https://example.com/shared")).source
    run(repo.add_source(other, "https://example.com/shared"))
    run(repo.set_enabled(owner, item.id, False))
    payload = json.loads(projection.read_text(encoding="utf-8"))
    rows = [row for row in payload["sources"] if row["id"] == item.id]
    assert len(rows) == 1 and rows[0]["enabled"] is True
    run(repo.set_enabled(other, item.id, False))
    payload = json.loads(projection.read_text(encoding="utf-8"))
    assert next(row for row in payload["sources"] if row["id"] == item.id)["enabled"] is False


def test_migration_is_idempotent_and_does_not_reset_workspace_decisions(tmp_path: Path) -> None:
    _, _, _, owner, repo = setup(tmp_path, [legacy_source()])
    run(repo.set_enabled(owner, "legacy", False))
    run(repo.set_usage_role(owner, "legacy", "competitor"))
    changed_seed = legacy_file(tmp_path / "changed.json", [legacy_source(enabled=True)])
    run(repo.init(None, legacy_path=changed_seed))
    item = run(repo.get_for_workspace(owner, "legacy"))
    assert item is not None and item.enabled is False and item.usage_role == "competitor"


def test_current_seed_ids_are_imported_without_changes(tmp_path: Path) -> None:
    from app.services.source_registry import SEED_REGISTRY_PATH, load_registry

    db = tmp_path / "db.sqlite3"
    partner = PartnerRepository(db)
    run(partner.init())
    owner = run(partner.bootstrap_owner_membership(100))
    assert owner is not None
    repo = SourceCatalogRepository(db, tmp_path / "projection.json")
    run(repo.init(owner.workspace_id, legacy_path=SEED_REGISTRY_PATH))
    expected = {source.id for source in load_registry(SEED_REGISTRY_PATH, use_cache=False)}
    with sqlite3.connect(db) as conn:
        actual = {row[0] for row in conn.execute("SELECT id FROM source_catalog")}
    assert actual == expected


def test_projection_failure_does_not_rollback_sqlite_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, _, owner, repo = setup(tmp_path)
    monkeypatch.setattr(
        "app.repositories.source_catalog_repository.export_radar_projection",
        lambda *_args: (_ for _ in ()).throw(OSError("disk unavailable")),
    )
    with pytest.raises(OSError, match="disk unavailable"):
        run(repo.add_source(owner, "https://example.com/committed"))
    items = run(repo.list_for_workspace(owner))
    assert any(item.target == "https://example.com/committed" for item in items)


def test_missing_owner_leaves_no_partial_catalog_state(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite3"
    run(PartnerRepository(db).init())
    unrelated = new_workspace(db, "must-not-be-selected")
    legacy = legacy_file(tmp_path / "legacy.json", [legacy_source()])
    repo = SourceCatalogRepository(db, tmp_path / "projection.json")
    with pytest.raises(SourceCatalogMigrationError):
        run(repo.init(None, legacy_path=legacy))
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='source_catalog'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM partner_workspaces WHERE id=?", (unrelated,)
        ).fetchone()[0] == 1


def test_migration_conflict_rolls_back_every_catalog_row(tmp_path: Path) -> None:
    first = legacy_source("first", enabled=True)
    duplicate = dict(first, id="duplicate", enabled=False)
    db = tmp_path / "db.sqlite3"
    partner = PartnerRepository(db)
    run(partner.init())
    owner = run(partner.bootstrap_owner_membership(100))
    assert owner is not None
    legacy = legacy_file(tmp_path / "legacy.json", [first, duplicate])
    repo = SourceCatalogRepository(db, tmp_path / "projection.json")
    with pytest.raises(sqlite3.IntegrityError):
        run(repo.init(owner.workspace_id, legacy_path=legacy))
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='source_catalog'"
        ).fetchone()[0] == 0


def test_parallel_workspace_add_and_toggle_are_isolated(tmp_path: Path) -> None:
    db, _, _, owner, repo = setup(tmp_path)
    other = new_workspace(db, "other")

    def add(workspace_id):
        return run(repo.add_source(workspace_id, "https://example.com/race")).source

    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(add, (owner, other)))
    assert rows[0].id == rows[1].id

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda ws: run(repo.toggle(ws, rows[0].id)), (owner, other)))
    assert run(repo.get_for_workspace(owner, rows[0].id)).enabled is False
    assert run(repo.get_for_workspace(other, rows[0].id)).enabled is False
    json.loads((tmp_path / "sources.json").read_text(encoding="utf-8"))


def test_older_export_cannot_overwrite_newer_committed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, projection, _, owner, repo = setup(tmp_path)
    other = new_workspace(db, "other")
    from app.repositories import source_catalog_repository as module

    real_export = module.export_radar_projection
    first_export_locked = threading.Event()
    release_first_export = threading.Event()
    call_lock = threading.Lock()
    calls = 0

    def controlled_export(path, rows):
        nonlocal calls
        with call_lock:
            calls += 1
            call_number = calls
        if call_number == 1:
            first_export_locked.set()
            assert release_first_export.wait(timeout=5)
        real_export(path, rows)

    monkeypatch.setattr(module, "export_radar_projection", controlled_export)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            lambda: run(repo.add_source(owner, "https://example.com/first"))
        )
        assert first_export_locked.wait(timeout=5)
        second = pool.submit(
            lambda: run(repo.add_source(other, "https://example.com/second"))
        )
        time.sleep(0.1)
        release_first_export.set()
        first.result(timeout=5)
        second.result(timeout=5)

    payload = json.loads(projection.read_text(encoding="utf-8"))
    targets = {row.get("url") for row in payload["sources"]}
    assert "https://example.com/first" in targets
    assert "https://example.com/second" in targets


def test_concurrent_exporters_are_serialized_by_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, projection, _, owner, repo = setup(tmp_path)
    run(repo.add_source(owner, "https://example.com/current"))
    from app.repositories import source_catalog_repository as module

    real_export = module.export_radar_projection
    state_lock = threading.Lock()
    active = 0
    maximum_active = 0

    def observed_export(path, rows):
        nonlocal active, maximum_active
        with state_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.1)
        real_export(path, rows)
        with state_lock:
            active -= 1

    monkeypatch.setattr(module, "export_radar_projection", observed_export)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(lambda: run(repo.export_projection())) for _ in range(2)]
        for future in futures:
            future.result(timeout=5)

    assert maximum_active == 1
    payload = json.loads(projection.read_text(encoding="utf-8"))
    assert any(row.get("url") == "https://example.com/current" for row in payload["sources"])
