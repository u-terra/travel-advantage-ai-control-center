from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite


_MIGRATION_KEY = "legacy_radar_owner_v1"

_SCHEMA = (
"""CREATE TABLE IF NOT EXISTS workspace_signal_interpretations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    radar_signal_id INTEGER NOT NULL,
    source_id TEXT,
    usage_role_snapshot TEXT
        CHECK (usage_role_snapshot IN ('monitoring', 'competitor')
               OR usage_role_snapshot IS NULL),
    status TEXT NOT NULL DEFAULT 'new',
    notes TEXT NOT NULL DEFAULT '',
    ai_score REAL,
    ai_category TEXT,
    ai_reason TEXT,
    suggested_message TEXT,
    llm_checked INTEGER NOT NULL DEFAULT 0,
    llm_checked_at TEXT,
    llm_signal_type TEXT,
    llm_score REAL,
    llm_relevance TEXT,
    llm_reason TEXT,
    llm_suggested_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES partner_workspaces(id),
    FOREIGN KEY (source_id) REFERENCES source_catalog(id),
    UNIQUE (workspace_id, radar_signal_id)
)""",
"""CREATE INDEX IF NOT EXISTS idx_workspace_signal_interpretations_workspace
    ON workspace_signal_interpretations(workspace_id, id)""",
"""CREATE TABLE IF NOT EXISTS workspace_signal_migrations (
    name TEXT PRIMARY KEY,
    completed_at TEXT NOT NULL
)""",
)


@dataclass(frozen=True)
class WorkspaceSignalRecord:
    interpretation_id: int
    workspace_id: int
    radar_signal_id: int
    source_id: str | None
    usage_role_snapshot: str | None
    status: str
    notes: str
    ai_score: float | None
    ai_category: str | None
    ai_reason: str | None
    suggested_message: str | None
    created_at: str
    raw_created_at: str
    source_type: str
    origin_type: str
    item_title: str
    item_summary: str
    item_url: str


class WorkspaceSignalRepository:
    def __init__(self, db_path: Path, radar_db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.radar_db_path = Path(radar_db_path)

    async def init(self, legacy_owner_workspace_id: int | None) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            for statement in _SCHEMA:
                await db.execute(statement)
            await db.commit()
        if legacy_owner_workspace_id is not None:
            await self._backfill_legacy_owner(legacy_owner_workspace_id)

    async def _backfill_legacy_owner(self, workspace_id: int) -> None:
        if not self.radar_db_path.is_file():
            return
        raw_rows = await self._read_raw_rows(include_null_source_id=True)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                owner = await (await db.execute(
                    "SELECT 1 FROM workspace_memberships "
                    "WHERE workspace_id = ? AND role = 'owner' AND status = 'active'",
                    (workspace_id,),
                )).fetchone()
                if owner is None:
                    await db.rollback()
                    return
                marker = await (await db.execute(
                    "SELECT 1 FROM workspace_signal_migrations WHERE name = ?",
                    (_MIGRATION_KEY,),
                )).fetchone()
                if marker is not None:
                    await db.rollback()
                    return
                catalog_ids = {
                    row[0] for row in await (await db.execute(
                        "SELECT id FROM source_catalog"
                    )).fetchall()
                }
                now = _now()
                for raw in raw_rows:
                    source_id = raw["source_id"] if raw["source_id"] in catalog_ids else None
                    await db.execute(
                        "INSERT INTO workspace_signal_interpretations "
                        "(workspace_id, radar_signal_id, source_id, usage_role_snapshot, "
                        "status, notes, ai_score, ai_category, ai_reason, suggested_message, "
                        "llm_checked, llm_checked_at, llm_signal_type, llm_score, "
                        "llm_relevance, llm_reason, llm_suggested_message, created_at, updated_at) "
                        "VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(workspace_id, radar_signal_id) DO NOTHING",
                        (
                            workspace_id, raw["id"], source_id,
                            raw["status"] or "new", raw["notes"] or "",
                            raw["ai_score"], raw["ai_category"], raw["ai_reason"],
                            raw["suggested_message"], int(raw["llm_checked"] or 0),
                            raw["llm_checked_at"], raw["llm_signal_type"],
                            raw["llm_score"], raw["llm_relevance"], raw["llm_reason"],
                            raw["llm_suggested_message"], now, now,
                        ),
                    )
                await db.execute(
                    "INSERT INTO workspace_signal_migrations(name, completed_at) VALUES (?, ?)",
                    (_MIGRATION_KEY, now),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def sync_eligible(self) -> int:
        raw_rows = await self._read_raw_rows(include_null_source_id=False)
        created = 0
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                now = _now()
                for raw in raw_rows:
                    subscriptions = await (await db.execute(
                        "SELECT s.workspace_id, s.usage_role FROM source_catalog c "
                        "JOIN workspace_source_subscriptions s ON s.source_id = c.id "
                        "WHERE c.id = ? AND c.status = 'active' AND s.enabled = 1",
                        (raw["source_id"],),
                    )).fetchall()
                    for subscription in subscriptions:
                        cursor = await db.execute(
                            "INSERT INTO workspace_signal_interpretations "
                            "(workspace_id, radar_signal_id, source_id, usage_role_snapshot, "
                            "status, notes, ai_score, ai_category, ai_reason, "
                            "created_at, updated_at) "
                            "VALUES (?, ?, ?, ?, 'new', '', ?, ?, ?, ?, ?) "
                            "ON CONFLICT(workspace_id, radar_signal_id) DO NOTHING",
                            (
                                subscription["workspace_id"], raw["id"], raw["source_id"],
                                subscription["usage_role"], raw["ai_score"],
                                raw["ai_category"], raw["ai_reason"], now, now,
                            ),
                        )
                        created += max(cursor.rowcount, 0)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return created

    async def list_for_workspace(
        self, workspace_id: int, *, limit: int = 200
    ) -> list[WorkspaceSignalRecord]:
        if limit < 1:
            raise ValueError("limit должен быть положительным")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT * FROM workspace_signal_interpretations "
                "WHERE workspace_id = ? ORDER BY id DESC LIMIT ?",
                (workspace_id, limit),
            )).fetchall()
        return await self._attach_raw(rows)

    async def get_for_workspace(
        self, workspace_id: int, interpretation_id: int
    ) -> WorkspaceSignalRecord | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                "SELECT * FROM workspace_signal_interpretations "
                "WHERE workspace_id = ? AND id = ?",
                (workspace_id, interpretation_id),
            )).fetchone()
        if row is None:
            return None
        records = await self._attach_raw([row])
        return records[0] if records else None

    async def _attach_raw(self, interpretations) -> list[WorkspaceSignalRecord]:
        if not interpretations:
            return []
        raw_ids = [row["radar_signal_id"] for row in interpretations]
        placeholders = ",".join("?" for _ in raw_ids)
        async with aiosqlite.connect(
            f"file:{self.radar_db_path.resolve().as_posix()}?mode=ro", uri=True
        ) as radar:
            radar.row_factory = aiosqlite.Row
            raw_rows = await (await radar.execute(
                f"SELECT id, created_at, source_type, origin_type, item_title, "
                f"item_summary, item_url FROM lead_signals WHERE id IN ({placeholders})",
                raw_ids,
            )).fetchall()
        raw_by_id = {row["id"]: row for row in raw_rows}
        result = []
        for row in interpretations:
            raw = raw_by_id.get(row["radar_signal_id"])
            if raw is not None:
                result.append(_record(row, raw))
        return result

    async def _read_raw_rows(self, *, include_null_source_id: bool):
        if not self.radar_db_path.is_file():
            return []
        condition = "" if include_null_source_id else " WHERE source_id IS NOT NULL"
        async with aiosqlite.connect(
            f"file:{self.radar_db_path.resolve().as_posix()}?mode=ro", uri=True
        ) as db:
            db.row_factory = aiosqlite.Row
            return await (await db.execute(
                "SELECT id, source_id, status, notes, ai_score, ai_category, ai_reason, "
                "suggested_message, llm_checked, llm_checked_at, llm_signal_type, "
                "llm_score, llm_relevance, llm_reason, llm_suggested_message "
                "FROM lead_signals" + condition
            )).fetchall()


def _record(row, raw) -> WorkspaceSignalRecord:
    return WorkspaceSignalRecord(
        interpretation_id=row["id"], workspace_id=row["workspace_id"],
        radar_signal_id=row["radar_signal_id"], source_id=row["source_id"],
        usage_role_snapshot=row["usage_role_snapshot"], status=row["status"],
        notes=row["notes"], ai_score=row["ai_score"], ai_category=row["ai_category"],
        ai_reason=row["ai_reason"], suggested_message=row["suggested_message"],
        created_at=row["created_at"], raw_created_at=raw["created_at"] or "",
        source_type=raw["source_type"] or "", origin_type=raw["origin_type"] or "",
        item_title=raw["item_title"] or "", item_summary=raw["item_summary"] or "",
        item_url=raw["item_url"] or "",
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
