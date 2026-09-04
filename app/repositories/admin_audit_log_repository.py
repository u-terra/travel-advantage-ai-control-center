"""admin_audit_log - see app/domain/admin_audit.py. Deliberately append-only
at the code level: this class exposes record() and list_recent() only, no
update/delete method exists anywhere, so there is no code path that could
ever alter or remove an entry once written.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from app.domain.admin_audit import AdminAuditEntry

_SCHEMA = """
CREATE TABLE IF NOT EXISTS admin_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    admin_web_user_id INTEGER NOT NULL,
    admin_email TEXT NOT NULL,
    action TEXT NOT NULL,
    target_workspace_id INTEGER,
    before_json TEXT,
    after_json TEXT,
    request_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_admin_audit_log_occurred_at
    ON admin_audit_log(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_admin_audit_log_workspace
    ON admin_audit_log(target_workspace_id, occurred_at DESC);
"""


class AdminAuditLogRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.executescript(_SCHEMA)
            await db.commit()

    async def record(
        self, *, admin_web_user_id: int, admin_email: str, action: str,
        target_workspace_id: int | None = None,
        before: dict[str, object] | None = None,
        after: dict[str, object] | None = None,
        request_id: str | None = None,
    ) -> AdminAuditEntry:
        now = _now()
        before_json = json.dumps(before, ensure_ascii=False, default=str) if before else None
        after_json = json.dumps(after, ensure_ascii=False, default=str) if after else None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "INSERT INTO admin_audit_log "
                "(occurred_at, admin_web_user_id, admin_email, action, "
                "target_workspace_id, before_json, after_json, request_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (now, admin_web_user_id, admin_email.strip().lower(), action,
                 target_workspace_id, before_json, after_json, request_id),
            )
            await db.commit()
            row = await self._row_by_id(db, cursor.lastrowid)
        if row is None:
            raise RuntimeError("Не удалось записать audit log")
        return _from_row(row)

    async def list_recent(
        self, *, target_workspace_id: int | None = None, limit: int = 200,
    ) -> list[AdminAuditEntry]:
        clauses: list[str] = []
        params: list[object] = []
        if target_workspace_id is not None:
            clauses.append("target_workspace_id = ?")
            params.append(target_workspace_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"SELECT * FROM admin_audit_log {where} ORDER BY id DESC LIMIT ?",
                (*params, limit),
            )
            rows = await cursor.fetchall()
        return [_from_row(row) for row in rows]

    @staticmethod
    async def _row_by_id(db: aiosqlite.Connection, entry_id: int):
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM admin_audit_log WHERE id = ?", (entry_id,),
        )
        return await cursor.fetchone()


def _from_row(row: aiosqlite.Row) -> AdminAuditEntry:
    return AdminAuditEntry(
        id=row["id"], occurred_at=row["occurred_at"],
        admin_web_user_id=row["admin_web_user_id"], admin_email=row["admin_email"],
        action=row["action"], target_workspace_id=row["target_workspace_id"],
        before_json=row["before_json"], after_json=row["after_json"],
        request_id=row["request_id"],
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
