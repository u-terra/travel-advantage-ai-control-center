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
    DEFAULT_SOURCE_PACK_IDS,
    SourceCatalogMigrationError,
    SourceCatalogRepository,
    SourceRequestAuthorizationError,
    SourceRequestConflictError,
)
from app.services.source_registry_store import UnknownSourceError


def run(value):
    return asyncio.run(value)


def legacy_file(path: Path, sources: list[dict] | None = None) -> Path:
    payload = {"schema_version": 2, "sources": sources or []}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def web_source(source_id: str, *, url: str | None = None, enabled: bool = True) -> dict:
    return {
        "id": source_id,
        "name": source_id,
        "platform": "web",
        "source_type": "monitored_source",
        "purpose": "content",
        "enabled": enabled,
        "priority": 40,
        "notes": "",
        "url": url or f"https://example.com/{source_id}",
    }


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


def add_member(db_path: Path, workspace_id: int, telegram_user_id: int) -> None:
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO workspace_memberships "
            "(workspace_id, telegram_user_id, role, status, created_at, updated_at) "
            "VALUES (?, ?, 'owner', 'active', 'now', 'now')",
            (workspace_id, telegram_user_id),
        )


def submit(repo: SourceCatalogRepository, workspace_id: int, address: str, user=100):
    return run(repo.submit_source_request(workspace_id, user, address))


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


def test_source_request_schema_constraints_and_init_upgrade_are_idempotent(
    tmp_path: Path,
) -> None:
    db, _, _, owner, repo = setup(tmp_path)
    request = submit(repo, owner, "https://example.com/request").request
    assert request is not None
    run(repo.init(None, legacy_path=tmp_path / "unused.json"))
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO source_requests "
                "(workspace_id, source_id, submitted_by_telegram_user_id, status, "
                "created_at, updated_at) VALUES (?, ?, 100, 'pending', 'x', 'x')",
                (owner, request.source_id),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO source_requests "
                "(workspace_id, source_id, submitted_by_telegram_user_id, status, "
                "created_at, updated_at) VALUES (9999, ?, 100, 'pending', 'x', 'x')",
                (request.source_id,),
            )


def test_submit_is_pending_without_subscription_or_projection_change(tmp_path: Path) -> None:
    db, projection, _, owner, repo = setup(tmp_path)
    before = projection.read_text(encoding="utf-8")
    result = submit(repo, owner, "https://example.com/pending")
    assert result.outcome == "pending"
    assert result.request is not None and result.request.status == "pending"
    assert projection.read_text(encoding="utf-8") == before
    assert run(repo.physical_collection_targets()) == ()
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM workspace_source_subscriptions"
        ).fetchone()[0] == 0


def test_submit_duplicate_cross_workspace_and_reopen_semantics(tmp_path: Path) -> None:
    db, _, _, owner, repo = setup(tmp_path)
    other = new_workspace(db, "request-other")
    add_member(db, other, 200)
    first = submit(repo, owner, "https://example.com/shared-request")
    duplicate = submit(repo, owner, "https://example.com/shared-request")
    foreign = submit(repo, other, "https://example.com/shared-request", 200)
    assert duplicate.outcome == "already_pending"
    assert duplicate.request.id == first.request.id
    assert foreign.request.source_id == first.request.source_id
    assert foreign.request.id != first.request.id
    run(repo.reject_source_request(first.request.id, owner, "not yet"))
    reopened = submit(repo, owner, "https://example.com/shared-request")
    assert reopened.outcome == "reopened"
    assert reopened.request.id == first.request.id
    assert reopened.request.status == "pending"
    assert reopened.request.reason is None and reopened.request.reviewed_at is None


def test_submit_requires_active_membership_and_legacy_subscription_wins(tmp_path: Path) -> None:
    db, _, _, owner, repo = setup(tmp_path)
    with pytest.raises(SourceRequestAuthorizationError):
        run(repo.submit_source_request(owner, 999, "https://example.com/foreign"))
    connected = run(repo.add_source(owner, "https://example.com/legacy-connected"))
    result = submit(repo, owner, connected.source.target)
    assert result.outcome == "already_connected" and result.request is None
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_requests").fetchone()[0] == 0


def test_toggle_without_subscription_and_pending_request_fail_closed(tmp_path: Path) -> None:
    db, _, _, owner, repo = setup(tmp_path, [legacy_source("platform-source")])
    other = new_workspace(db, "toggle-other")
    add_member(db, other, 200)
    with pytest.raises(UnknownSourceError):
        run(repo.toggle(other, "platform-source"))
    pending = submit(repo, other, "https://example.com/pending-toggle", 200).request
    with pytest.raises(UnknownSourceError):
        run(repo.toggle(other, pending.source_id))
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM workspace_source_subscriptions WHERE workspace_id=?",
            (other,),
        ).fetchone()[0] == 0


def test_approve_is_tenant_scoped_idempotent_and_persistent(tmp_path: Path) -> None:
    db, _, _, owner, repo = setup(tmp_path)
    other = new_workspace(db, "approve-other")
    add_member(db, other, 200)
    request = submit(repo, owner, "https://example.com/approve").request
    with pytest.raises(SourceRequestAuthorizationError):
        run(repo.approve_source_request(request.id, other))
    approved = run(repo.approve_source_request(request.id, owner))
    repeated = run(repo.approve_source_request(request.id, owner))
    assert approved.status == repeated.status == "approved"
    assert run(repo.get_for_workspace(owner, request.source_id)).enabled is True
    assert run(repo.get_for_workspace(other, request.source_id)) is None
    reopened_repo = SourceCatalogRepository(db, tmp_path / "reopened.json")
    assert run(reopened_repo.get_source_request(request.id)).status == "approved"


def test_approve_projection_failure_rolls_back_request_and_subscription(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, _, _, owner, repo = setup(tmp_path)
    request = submit(repo, owner, "https://example.com/rollback").request
    monkeypatch.setattr(
        "app.repositories.source_catalog_repository.export_radar_projection",
        lambda *_args: (_ for _ in ()).throw(OSError("projection failed")),
    )
    with pytest.raises(OSError, match="projection failed"):
        run(repo.approve_source_request(request.id, owner))
    assert run(repo.get_source_request(request.id)).status == "pending"
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM workspace_source_subscriptions WHERE workspace_id=?",
            (owner,),
        ).fetchone()[0] == 0


def test_reject_is_idempotent_and_approved_is_fail_closed(tmp_path: Path) -> None:
    _, _, _, owner, repo = setup(tmp_path)
    rejected_request = submit(repo, owner, "https://example.com/reject").request
    rejected = run(repo.reject_source_request(rejected_request.id, owner, "unsupported"))
    repeated = run(repo.reject_source_request(rejected_request.id, owner, "changed"))
    assert rejected.status == repeated.status == "rejected"
    assert repeated.reason == "unsupported"
    assert run(repo.get_for_workspace(owner, rejected.source_id)) is None
    with pytest.raises(SourceRequestConflictError):
        run(repo.approve_source_request(rejected.id, owner))

    approved_request = submit(repo, owner, "https://example.com/approved").request
    run(repo.approve_source_request(approved_request.id, owner))
    with pytest.raises(SourceRequestConflictError):
        run(repo.reject_source_request(approved_request.id, owner))
    assert run(repo.get_for_workspace(owner, approved_request.source_id)).enabled is True


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


# ── assign_default_sources: ORCHESTRAVEL default source pack (stage 1) ─────
#
# A small, self-contained 3-source fixture pack ("pack_a/b/c"), independent
# of the real 9-source DEFAULT_SOURCE_PACK_IDS/config/sources.json content -
# these tests exercise the MECHANISM (idempotency, isolation, per-workspace
# toggling), not the specific production source list, which is instead
# covered by the two dedicated tests at the end of this section.

PACK_IDS = ("pack_a", "pack_b", "pack_c")


def pack_seed(tmp_path: Path) -> Path:
    return legacy_file(tmp_path / "pack_seed.json", [web_source(pid) for pid in PACK_IDS])


def test_assign_default_sources_creates_catalog_rows_and_subscriptions(tmp_path: Path) -> None:
    db, _, _, owner, repo = setup(tmp_path)
    new_id = new_workspace(db, "paid-partner")
    seed = pack_seed(tmp_path)

    run(repo.assign_default_sources(new_id, PACK_IDS, seed_path=seed))

    sources = run(repo.list_for_workspace(new_id))
    assert {s.id for s in sources} == set(PACK_IDS)
    assert all(s.enabled for s in sources)
    assert all(s.platform == "web" for s in sources)
    assert all(s.usage_role == "monitoring" for s in sources)
    # The pre-existing (legacy owner) workspace never gets these - default
    # pack assignment is scoped to exactly the workspace it's called for.
    assert {s.id for s in run(repo.list_for_workspace(owner))}.isdisjoint(PACK_IDS)


def test_assign_default_sources_is_idempotent_on_repeat_call(tmp_path: Path) -> None:
    """Requirement: a replayed payment webhook (or any repeat call) must
    not create duplicate subscription rows."""
    db, _, _, owner, repo = setup(tmp_path)
    new_id = new_workspace(db, "paid-partner-2")
    seed = pack_seed(tmp_path)

    run(repo.assign_default_sources(new_id, PACK_IDS, seed_path=seed))
    run(repo.assign_default_sources(new_id, PACK_IDS, seed_path=seed))

    with sqlite3.connect(db) as conn:
        subscription_count = conn.execute(
            "SELECT COUNT(*) FROM workspace_source_subscriptions WHERE workspace_id = ?",
            (new_id,),
        ).fetchone()[0]
        catalog_count = conn.execute(
            "SELECT COUNT(*) FROM source_catalog WHERE id IN (?, ?, ?)", PACK_IDS,
        ).fetchone()[0]
    assert subscription_count == len(PACK_IDS)
    assert catalog_count == len(PACK_IDS)


def test_assign_default_sources_does_not_reset_a_workspace_own_customization(tmp_path: Path) -> None:
    """A second call (e.g. a replayed payment webhook, or being called
    again for a workspace that already has some of these ids from
    elsewhere) must never override settings the workspace already changed
    for itself."""
    db, _, _, owner, repo = setup(tmp_path)
    new_id = new_workspace(db, "paid-partner-3")
    seed = pack_seed(tmp_path)
    run(repo.assign_default_sources(new_id, PACK_IDS, seed_path=seed))

    run(repo.set_enabled(new_id, "pack_a", False))
    run(repo.set_usage_role(new_id, "pack_b", "competitor"))

    run(repo.assign_default_sources(new_id, PACK_IDS, seed_path=seed))

    sources = {s.id: s for s in run(repo.list_for_workspace(new_id))}
    assert sources["pack_a"].enabled is False
    assert sources["pack_b"].usage_role == "competitor"
    assert sources["pack_c"].enabled is True


def test_assign_default_sources_toggle_still_works_per_workspace(tmp_path: Path) -> None:
    db, _, _, owner, repo = setup(tmp_path)
    new_id = new_workspace(db, "paid-partner-4")
    seed = pack_seed(tmp_path)
    run(repo.assign_default_sources(new_id, PACK_IDS, seed_path=seed))

    updated = run(repo.toggle(new_id, "pack_a"))

    assert updated.enabled is False
    sources = {s.id: s for s in run(repo.list_for_workspace(new_id))}
    assert sources["pack_a"].enabled is False
    assert sources["pack_b"].enabled is True


def test_assign_default_sources_keeps_private_sources_workspace_isolated(tmp_path: Path) -> None:
    """A private source another workspace adds for itself must never become
    visible to the workspace that received the default pack."""
    db, _, _, owner, repo = setup(tmp_path)
    new_id = new_workspace(db, "paid-partner-5")
    seed = pack_seed(tmp_path)
    run(repo.assign_default_sources(new_id, PACK_IDS, seed_path=seed))

    private_result = run(repo.add_source(owner, "https://example.com/owner-private"))

    new_workspace_source_ids = {s.id for s in run(repo.list_for_workspace(new_id))}
    assert private_result.source.id not in new_workspace_source_ids
    owner_source_ids = {s.id for s in run(repo.list_for_workspace(owner))}
    assert owner_source_ids.isdisjoint(PACK_IDS)


def test_assign_default_sources_skips_unknown_id_without_raising(tmp_path: Path) -> None:
    db, _, _, owner, repo = setup(tmp_path)
    new_id = new_workspace(db, "paid-partner-6")
    seed = pack_seed(tmp_path)

    run(repo.assign_default_sources(new_id, (*PACK_IDS, "does_not_exist"), seed_path=seed))

    sources = {s.id for s in run(repo.list_for_workspace(new_id))}
    assert sources == set(PACK_IDS)


def test_assign_default_sources_empty_pack_is_a_no_op(tmp_path: Path) -> None:
    db, _, _, owner, repo = setup(tmp_path)
    new_id = new_workspace(db, "paid-partner-7")

    run(repo.assign_default_sources(new_id, ()))

    assert run(repo.list_for_workspace(new_id)) == ()


def test_assign_default_sources_supports_a_future_disabled_optional_pack(tmp_path: Path) -> None:
    """Architectural hook for a later, separate 'рынок / турагенты и
    эксперты' pack (purpose=market_experts) - not added in this stage, but
    the enabled= parameter already lets a future call opt a second pack in
    as disabled by default, with no further change to this method."""
    db, _, _, owner, repo = setup(tmp_path)
    new_id = new_workspace(db, "paid-partner-8")
    seed = pack_seed(tmp_path)

    run(repo.assign_default_sources(new_id, PACK_IDS, seed_path=seed, enabled=False))

    sources = run(repo.list_for_workspace(new_id))
    assert {s.id for s in sources} == set(PACK_IDS)
    assert all(not s.enabled for s in sources)


def test_default_source_pack_ids_are_exactly_the_nine_stage_one_sources() -> None:
    assert set(DEFAULT_SOURCE_PACK_IDS) == {
        "trip_com", "aviasales_psgr", "yandex_web_journal", "tj_travel",
        "onetwotrip_blog", "tutu_guide", "skyscanner_travel_tips",
        "lonely_planet_advice", "traveloka_explore",
    }
    assert len(DEFAULT_SOURCE_PACK_IDS) == 9


def test_default_source_pack_ids_resolve_in_the_real_seed_registry() -> None:
    """Regression guard tying the code constant to config/sources.json - a
    future edit to either one going out of sync would otherwise only show
    up as a silent 'unknown source id skipped' warning in production."""
    from app.services.source_registry import SEED_REGISTRY_PATH, load_registry

    registry = load_registry(SEED_REGISTRY_PATH, use_cache=False)
    for source_id in DEFAULT_SOURCE_PACK_IDS:
        source = registry.get(source_id)
        assert source is not None, f"missing from config/sources.json: {source_id}"
        assert source.platform == "web"
        assert source.enabled is True
        assert source.url and source.url.startswith("https://")


def test_assign_default_sources_with_real_pack_assigns_all_nine(tmp_path: Path) -> None:
    """End-to-end with the REAL default pack (still against a throwaway
    DB/projection - only config/sources.json itself is the real, shared
    file) - the actual scenario a new paid workspace hits."""
    db, _, _, owner, repo = setup(tmp_path)
    new_id = new_workspace(db, "paid-partner-9")

    run(repo.assign_default_sources(new_id))  # default source_ids, default seed_path

    sources = run(repo.list_for_workspace(new_id))
    assert {s.id for s in sources} == set(DEFAULT_SOURCE_PACK_IDS)
    assert all(s.enabled for s in sources)
