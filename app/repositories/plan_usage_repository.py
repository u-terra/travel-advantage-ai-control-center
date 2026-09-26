"""Persisted logical-action ledger for ORCHESTRAVEL plan quotas.

Deliberately NOT the same table as app.repositories.usage_ledger_repository
(usage_events): that ledger records one row per raw LLM/provider call - a
single user-facing "material" or "competitor analysis" can and does involve
several of those (see app.services.competitor_intelligence, which calls
analyze_source once per fetched public source). Using usage_events as a
business quota would over-count: a competitor analysis over 5 sources would
silently cost 5 units instead of 1. So this module records exactly ONE row
per successful LOGICAL action - decided by the calling code (only after it
has confirmed the action actually succeeded end-to-end, e.g. an Artifact was
actually created, or CompetitorIntelligenceService.analyze() actually
returned intelligence instead of raising) - never a raw call count.

A failed action (LLM call failed, no Artifact created, analyze() raised
CompetitorIntelligenceUnavailable) must never call record() - see
app.services.plan_quota_service, the only caller.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

MATERIAL_CREATED = "material_created"
COMPETITOR_ANALYSIS_COMPLETED = "competitor_analysis_completed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plan_logical_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    action_type TEXT NOT NULL CHECK (
        action_type IN ('material_created', 'competitor_analysis_completed')
    ),
    occurred_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_plan_logical_actions_workspace_action
    ON plan_logical_actions(workspace_id, action_type, occurred_at);
"""


class PlanUsageRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.executescript(_SCHEMA)
            await db.commit()

    async def record(self, workspace_id: int, action_type: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO plan_logical_actions (workspace_id, action_type, occurred_at) "
                "VALUES (?, ?, ?)",
                (workspace_id, action_type, _now()),
            )
            await db.commit()

    async def count_since(
        self, workspace_id: int, action_type: str, since_iso: str,
    ) -> int:
        """Rolling-window count - callers compute `since_iso` themselves
        (now - window_days), so there is no separate cron/reset job: an
        action ages out of the window the moment enough real time has
        passed, purely by this WHERE clause, never by a scheduled deletion."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM plan_logical_actions "
                "WHERE workspace_id = ? AND action_type = ? AND occurred_at >= ?",
                (workspace_id, action_type, since_iso),
            )
            row = await cursor.fetchone()
        return int(row[0]) if row is not None else 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
