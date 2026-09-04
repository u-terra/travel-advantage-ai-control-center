"""Append-only record of every platform-admin mutation - see
app/repositories/admin_audit_log_repository.py. before_json/after_json are
safe, pre-summarized snapshots (e.g. {"status": "active", "paid_until":
"..."}) the caller builds itself - never raw secrets, never a full DB row
dump."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AdminAuditEntry:
    id: int
    occurred_at: str
    admin_web_user_id: int
    admin_email: str
    action: str
    target_workspace_id: int | None
    before_json: str | None
    after_json: str | None
    request_id: str | None
