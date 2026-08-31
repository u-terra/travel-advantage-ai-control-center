"""Persisted AI usage ledger - one row per LLM/provider call.

Follows the same repository conventions as app/repositories/
competitor_repository.py: own table in the shared journal DB, plain
aiosqlite, no ORM. Recording a call must never be able to break the
caller's actual flow - see record(), which is best-effort by contract at
every call site that uses it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from app.domain.usage import (
    ModuleUsageBreakdown,
    UsageEvent,
    UsageStatus,
    WorkspaceUsageSummary,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    workspace_id INTEGER NOT NULL,
    telegram_user_id INTEGER,
    module TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    estimated_cost_usd REAL,
    status TEXT NOT NULL CHECK (status IN ('success', 'failure')),
    FOREIGN KEY (workspace_id) REFERENCES partner_workspaces(id)
);

CREATE INDEX IF NOT EXISTS idx_usage_events_workspace
    ON usage_events(workspace_id, occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_usage_events_module
    ON usage_events(module, occurred_at DESC);
"""


class UsageLedgerRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.executescript(_SCHEMA)
            await db.commit()

    async def record(
        self, *, workspace_id: int, telegram_user_id: int | None, module: str,
        provider: str, model: str | None,
        input_tokens: int | None, output_tokens: int | None, total_tokens: int | None,
        estimated_cost_usd: float | None, status: UsageStatus,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute(
                "INSERT INTO usage_events (occurred_at, workspace_id, telegram_user_id, "
                "module, provider, model, input_tokens, output_tokens, total_tokens, "
                "estimated_cost_usd, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _now(), workspace_id, telegram_user_id, module, provider, model,
                    input_tokens, output_tokens, total_tokens, estimated_cost_usd,
                    status.value,
                ),
            )
            await db.commit()

    async def list_for_workspace(
        self, workspace_id: int, *, since: str | None = None, limit: int = 1000,
    ) -> list[UsageEvent]:
        query = "SELECT * FROM usage_events WHERE workspace_id = ?"
        params: list[object] = [workspace_id]
        if since is not None:
            query += " AND occurred_at >= ?"
            params.append(since)
        query += " ORDER BY occurred_at DESC LIMIT ?"
        params.append(limit)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()
        return [_event_from_row(row) for row in rows]

    async def summary_for_workspace(
        self, workspace_id: int, *, since: str | None = None,
    ) -> WorkspaceUsageSummary:
        events = await self.list_for_workspace(workspace_id, since=since, limit=100_000)
        return _summarize(workspace_id, events)

    async def known_workspace_ids(self, *, since: str | None = None) -> list[int]:
        """Workspaces that have at least one recorded usage event - the
        starting point for a per-workspace report across all test users."""
        query = "SELECT DISTINCT workspace_id FROM usage_events"
        params: list[object] = []
        if since is not None:
            query += " WHERE occurred_at >= ?"
            params.append(since)
        query += " ORDER BY workspace_id"
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()
        return [row[0] for row in rows]


def _summarize(workspace_id: int, events: list[UsageEvent]) -> WorkspaceUsageSummary:
    successful = sum(1 for e in events if e.status is UsageStatus.SUCCESS)
    failed = sum(1 for e in events if e.status is UsageStatus.FAILURE)
    with_tokens = [e for e in events if e.total_tokens is not None]
    total_tokens = sum(e.total_tokens for e in with_tokens) if with_tokens else None
    with_cost = [e for e in events if e.estimated_cost_usd is not None]
    total_cost = sum(e.estimated_cost_usd for e in with_cost) if with_cost else None

    by_module: dict[str, list[UsageEvent]] = {}
    for event in events:
        by_module.setdefault(event.module, []).append(event)
    breakdown = tuple(
        sorted(
            (
                ModuleUsageBreakdown(
                    module=module,
                    calls=len(module_events),
                    total_tokens=(
                        sum(e.total_tokens for e in module_events if e.total_tokens is not None)
                        if any(e.total_tokens is not None for e in module_events) else None
                    ),
                    estimated_cost_usd=(
                        sum(
                            e.estimated_cost_usd for e in module_events
                            if e.estimated_cost_usd is not None
                        )
                        if any(e.estimated_cost_usd is not None for e in module_events) else None
                    ),
                )
                for module, module_events in by_module.items()
            ),
            key=lambda b: b.calls, reverse=True,
        )
    )
    return WorkspaceUsageSummary(
        workspace_id=workspace_id,
        total_calls=len(events),
        successful_calls=successful,
        failed_calls=failed,
        calls_with_token_data=len(with_tokens),
        total_tokens=total_tokens,
        estimated_cost_usd=total_cost,
        by_module=breakdown,
    )


def _event_from_row(row: aiosqlite.Row) -> UsageEvent:
    return UsageEvent(
        id=row["id"], occurred_at=row["occurred_at"], workspace_id=row["workspace_id"],
        telegram_user_id=row["telegram_user_id"], module=row["module"],
        provider=row["provider"], model=row["model"],
        input_tokens=row["input_tokens"], output_tokens=row["output_tokens"],
        total_tokens=row["total_tokens"], estimated_cost_usd=row["estimated_cost_usd"],
        status=UsageStatus(row["status"]),
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
