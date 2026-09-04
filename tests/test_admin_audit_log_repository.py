from __future__ import annotations

import asyncio
from pathlib import Path

from app.repositories.admin_audit_log_repository import AdminAuditLogRepository


def run(coro):
    return asyncio.run(coro)


def test_record_and_list_recent(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    repo = AdminAuditLogRepository(db_path)
    run(repo.init())

    entry = run(repo.record(
        admin_web_user_id=1, admin_email="Owner@Example.com", action="suspend",
        target_workspace_id=42, before={"status": "active"}, after={"status": "suspended"},
    ))

    assert entry.admin_email == "owner@example.com"  # normalized lowercase
    assert entry.action == "suspend"
    assert entry.target_workspace_id == 42
    assert '"status": "active"' in entry.before_json
    assert '"status": "suspended"' in entry.after_json

    entries = run(repo.list_recent())
    assert len(entries) == 1
    assert entries[0].id == entry.id


def test_list_recent_filters_by_target_workspace(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    repo = AdminAuditLogRepository(db_path)
    run(repo.init())
    run(repo.record(admin_web_user_id=1, admin_email="a@x.com", action="suspend", target_workspace_id=1))
    run(repo.record(admin_web_user_id=1, admin_email="a@x.com", action="restore", target_workspace_id=2))

    assert len(run(repo.list_recent(target_workspace_id=1))) == 1
    assert len(run(repo.list_recent(target_workspace_id=2))) == 1
    assert len(run(repo.list_recent())) == 2


def test_list_recent_orders_newest_first(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    repo = AdminAuditLogRepository(db_path)
    run(repo.init())
    first = run(repo.record(admin_web_user_id=1, admin_email="a@x.com", action="suspend", target_workspace_id=1))
    second = run(repo.record(admin_web_user_id=1, admin_email="a@x.com", action="restore", target_workspace_id=1))

    entries = run(repo.list_recent())
    assert entries[0].id == second.id
    assert entries[1].id == first.id


def test_repository_exposes_no_update_or_delete_method(tmp_path: Path):
    """Append-only by construction, not just convention - assert the
    class literally has no way to mutate/remove an existing entry."""
    db_path = tmp_path / "db.sqlite3"
    repo = AdminAuditLogRepository(db_path)
    public_methods = {name for name in dir(repo) if not name.startswith("_")}
    assert public_methods == {"init", "record", "list_recent", "db_path"}


def test_no_secret_like_field_exists_on_the_domain_entry(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    repo = AdminAuditLogRepository(db_path)
    run(repo.init())
    entry = run(repo.record(admin_web_user_id=1, admin_email="a@x.com", action="suspend"))
    fields = set(entry.__dataclass_fields__)
    for forbidden in ("password", "secret", "token", "signature", "csrf"):
        assert not any(forbidden in f.lower() for f in fields)
