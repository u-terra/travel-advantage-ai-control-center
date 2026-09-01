"""Persisted workspace-level memory - one short summary row per workspace.

Own table in the shared journal DB, plain aiosqlite, no ORM - same
conventions as app/repositories/usage_ledger_repository.py. Deliberately
separate from conversation_state_repository.py (per-session dialogue),
BusinessProfile (app/repositories/partner_repository.py) and
workspace_user_preferences/personal_style (also partner_repository.py):
this table stores nothing but a manually curated working summary of the
project, one row per workspace.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from app.domain.workspace_memory import WorkspaceMemoryRecord

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL UNIQUE,
    summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES partner_workspaces(id)
);
"""


class WorkspaceMemoryRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.executescript(_SCHEMA)
            await db.commit()

    async def get(self, workspace_id: int) -> WorkspaceMemoryRecord | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                "SELECT * FROM workspace_memory WHERE workspace_id = ?",
                (workspace_id,),
            )).fetchone()
        return _record_from_row(row) if row is not None else None

    async def set_summary(
        self, workspace_id: int, summary: str
    ) -> WorkspaceMemoryRecord:
        cleaned = summary.strip()
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute(
                "INSERT INTO workspace_memory "
                "(workspace_id, summary, created_at, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(workspace_id) DO UPDATE SET "
                "summary = excluded.summary, updated_at = excluded.updated_at",
                (workspace_id, cleaned, now, now),
            )
            await db.commit()
            row = await (await db.execute(
                "SELECT * FROM workspace_memory WHERE workspace_id = ?",
                (workspace_id,),
            )).fetchone()
        if row is None:
            raise RuntimeError("Не удалось сохранить workspace memory")
        return _record_from_row(row)


def _record_from_row(row: aiosqlite.Row) -> WorkspaceMemoryRecord:
    return WorkspaceMemoryRecord(
        workspace_id=row["workspace_id"],
        summary=row["summary"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
