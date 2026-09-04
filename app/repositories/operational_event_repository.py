"""operational_events - see app/domain/telemetry.py for scope/safety rules.

Plain aiosqlite, additive schema, same conventions as every other
repository here. Writes are best-effort from the caller's perspective (see
app.services.telemetry.record_event) - this repository itself always
either succeeds or raises, callers decide whether to swallow.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from app.domain.telemetry import ErrorGroup, EventSeverity, OperationalEvent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS operational_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    workspace_id INTEGER,
    telegram_user_id INTEGER,
    web_user_id INTEGER,
    module TEXT NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info'
        CHECK (severity IN ('info', 'warning', 'error', 'critical')),
    success INTEGER NOT NULL CHECK (success IN (0, 1)),
    latency_ms INTEGER,
    request_id TEXT,
    error_code TEXT,
    safe_message TEXT,
    metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_operational_events_occurred_at
    ON operational_events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_operational_events_module_type
    ON operational_events(module, event_type, occurred_at);
CREATE INDEX IF NOT EXISTS idx_operational_events_workspace
    ON operational_events(workspace_id, occurred_at);
"""

# Hard caps - a caller passing something huge (should never happen given
# app.services.telemetry.record_event's own limits) still can't bloat a
# single row without bound.
_MAX_SAFE_MESSAGE_CHARS = 500
_MAX_METADATA_CHARS = 2000


class OperationalEventRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.executescript(_SCHEMA)
            await db.commit()

    async def record(
        self,
        *,
        module: str,
        event_type: str,
        success: bool,
        workspace_id: int | None = None,
        telegram_user_id: int | None = None,
        web_user_id: int | None = None,
        severity: EventSeverity = EventSeverity.INFO,
        latency_ms: int | None = None,
        request_id: str | None = None,
        error_code: str | None = None,
        safe_message: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        metadata_json = None
        if metadata:
            try:
                metadata_json = json.dumps(metadata, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                metadata_json = None
            if metadata_json is not None and len(metadata_json) > _MAX_METADATA_CHARS:
                metadata_json = metadata_json[:_MAX_METADATA_CHARS]
        safe = (safe_message or "")[:_MAX_SAFE_MESSAGE_CHARS] or None

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO operational_events "
                "(occurred_at, workspace_id, telegram_user_id, web_user_id, module, "
                "event_type, severity, success, latency_ms, request_id, error_code, "
                "safe_message, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _now(), workspace_id, telegram_user_id, web_user_id, module,
                    event_type, severity.value, 1 if success else 0, latency_ms,
                    request_id, error_code, safe, metadata_json,
                ),
            )
            await db.commit()

    async def count_since(
        self, since_iso: str, *, module: str | None = None,
        event_type: str | None = None, success: bool | None = None,
    ) -> int:
        clauses = ["occurred_at >= ?"]
        params: list[object] = [since_iso]
        if module is not None:
            clauses.append("module = ?")
            params.append(module)
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        if success is not None:
            clauses.append("success = ?")
            params.append(1 if success else 0)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                f"SELECT COUNT(*) FROM operational_events WHERE {' AND '.join(clauses)}",
                params,
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def distinct_workspace_count_since(
        self, since_iso: str, *, module: str | None = None, event_type: str | None = None,
    ) -> int:
        clauses = ["occurred_at >= ?", "workspace_id IS NOT NULL"]
        params: list[object] = [since_iso]
        if module is not None:
            clauses.append("module = ?")
            params.append(module)
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(DISTINCT workspace_id) FROM operational_events "
                f"WHERE {' AND '.join(clauses)}",
                params,
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def distinct_web_user_count_since(
        self, since_iso: str, *, module: str | None = None, event_type: str | None = None,
    ) -> int:
        clauses = ["occurred_at >= ?", "web_user_id IS NOT NULL"]
        params: list[object] = [since_iso]
        if module is not None:
            clauses.append("module = ?")
            params.append(module)
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(DISTINCT web_user_id) FROM operational_events "
                f"WHERE {' AND '.join(clauses)}",
                params,
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def distinct_web_user_count_with_event_on_a_later_day(self) -> int:
        """Beta Control Center funnel only (app/admin_api.py) - a simple,
        honest "came back" proxy: web users with events recorded on at
        least two distinct calendar days (UTC date prefix of occurred_at).
        Not a real session/visit concept - just what's actually derivable
        from this table."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM ("
                "  SELECT web_user_id FROM operational_events "
                "  WHERE web_user_id IS NOT NULL "
                "  GROUP BY web_user_id "
                "  HAVING COUNT(DISTINCT substr(occurred_at, 1, 10)) >= 2"
                ")"
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def error_summary_since(
        self, since_iso: str, *, module: str | None = None,
        severity: str | None = None, workspace_id: int | None = None, limit: int = 100,
    ) -> list[ErrorGroup]:
        """Grouped "this failed N times across M workspaces" rows for
        /admin/errors - never a raw per-event dump by default."""
        clauses = ["occurred_at >= ?", "success = 0"]
        params: list[object] = [since_iso]
        if module is not None:
            clauses.append("module = ?")
            params.append(module)
        if severity is not None:
            clauses.append("severity = ?")
            params.append(severity)
        if workspace_id is not None:
            clauses.append("workspace_id = ?")
            params.append(workspace_id)
        where = " AND ".join(clauses)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT module, event_type, error_code, severity, "
                "COUNT(*) AS occurrences, "
                "COUNT(DISTINCT workspace_id) AS workspace_count, "
                "MAX(occurred_at) AS last_occurred_at "
                f"FROM operational_events WHERE {where} "
                "GROUP BY module, event_type, error_code, severity "
                "ORDER BY occurrences DESC LIMIT ?",
                (*params, limit),
            )
            rows = await cursor.fetchall()

            groups: list[ErrorGroup] = []
            for row in rows:
                sample_cursor = await db.execute(
                    "SELECT safe_message, request_id FROM operational_events "
                    "WHERE module = ? AND event_type = ? AND success = 0 "
                    "AND (error_code IS ? ) "
                    "ORDER BY occurred_at DESC LIMIT 1",
                    (row["module"], row["event_type"], row["error_code"]),
                )
                sample = await sample_cursor.fetchone()
                groups.append(ErrorGroup(
                    module=row["module"], event_type=row["event_type"],
                    error_code=row["error_code"],
                    severity=EventSeverity(row["severity"]),
                    occurrences=row["occurrences"],
                    workspace_count=row["workspace_count"],
                    last_occurred_at=row["last_occurred_at"],
                    sample_safe_message=sample["safe_message"] if sample else None,
                    sample_request_id=sample["request_id"] if sample else None,
                ))
        # Critical first, then by occurrence count (already SQL-sorted) -
        # stable Python sort keeps the SQL ordering within each severity.
        severity_rank = {"critical": 0, "error": 1, "warning": 2, "info": 3}
        groups.sort(key=lambda g: severity_rank.get(g.severity.value, 9))
        return groups

    async def list_recent_events(
        self, *, since_iso: str | None = None, module: str | None = None,
        success: bool | None = None, workspace_id: int | None = None, limit: int = 50,
    ) -> list[OperationalEvent]:
        clauses: list[str] = []
        params: list[object] = []
        if since_iso is not None:
            clauses.append("occurred_at >= ?")
            params.append(since_iso)
        if module is not None:
            clauses.append("module = ?")
            params.append(module)
        if success is not None:
            clauses.append("success = ?")
            params.append(1 if success else 0)
        if workspace_id is not None:
            clauses.append("workspace_id = ?")
            params.append(workspace_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"SELECT * FROM operational_events {where} "
                "ORDER BY occurred_at DESC LIMIT ?",
                (*params, limit),
            )
            rows = await cursor.fetchall()
        return [_from_row(row) for row in rows]

    async def last_success(self, *, module: str, event_type: str) -> OperationalEvent | None:
        """Backs /admin/health's "last successful X" - never invents a
        healthy state, None means it genuinely never happened (or not yet
        recorded)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM operational_events "
                "WHERE module = ? AND event_type = ? AND success = 1 "
                "ORDER BY occurred_at DESC LIMIT 1",
                (module, event_type),
            )
            row = await cursor.fetchone()
        return _from_row(row) if row is not None else None


def _from_row(row: aiosqlite.Row) -> OperationalEvent:
    return OperationalEvent(
        id=row["id"], occurred_at=row["occurred_at"], workspace_id=row["workspace_id"],
        telegram_user_id=row["telegram_user_id"], web_user_id=row["web_user_id"],
        module=row["module"], event_type=row["event_type"],
        severity=EventSeverity(row["severity"]), success=bool(row["success"]),
        latency_ms=row["latency_ms"], request_id=row["request_id"],
        error_code=row["error_code"], safe_message=row["safe_message"],
        metadata_json=row["metadata_json"],
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
