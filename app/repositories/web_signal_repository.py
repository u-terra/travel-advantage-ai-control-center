"""Stage 2 (ORCHESTRAVEL): internal storage for signals collected from a
workspace's own ``platform="web"`` source_catalog subscriptions.

Deliberately a SEPARATE table from ``workspace_signal_interpretations``
(see app.repositories.workspace_signal_repository): that table only ever
PROJECTS rows that already exist in the external Travel Lead Radar
``leads.db`` (opened strictly read-only) - it has no columns for the actual
title/summary/url content because that content lives in Radar's own
``lead_signals`` table. Web signals have no such external system: this repo
fetches and analyzes the page itself, so the full content has to be stored
somewhere WE own. Reusing workspace_signal_interpretations would mean either
writing to leads.db (explicitly forbidden - it stays a read-only legacy
boundary) or faking a radar_signal_id for a row that was never produced by
Radar. A small dedicated table avoids both.

Lives in the same Journal DB as source_catalog / workspace_source_subscriptions
(``settings.journal_db_path``) - never in ``leads.db``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import aiosqlite

_SCHEMA_STATEMENTS = (
"""CREATE TABLE IF NOT EXISTS web_source_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    item_url TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES partner_workspaces(id),
    FOREIGN KEY (source_id) REFERENCES source_catalog(id),
    UNIQUE (workspace_id, dedupe_key)
)""",
"""CREATE INDEX IF NOT EXISTS idx_web_source_signals_workspace
    ON web_source_signals(workspace_id, id)""",
)


@dataclass(frozen=True)
class WebSignalRecord:
    """One collected web signal. ``id`` is unset (``None``) for a record
    built by the collector before it has been written; ``save_many()``
    ignores it on write and ``list_for_workspace()`` always returns it set -
    the same "draft vs stored" convention read-only call sites can rely on.
    """

    workspace_id: int
    source_id: str
    source_name: str
    source_url: str
    item_url: str
    title: str
    summary: str
    fetched_at: str
    id: int | None = None
    created_at: str = ""
    updated_at: str = ""


class WebSignalRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            for statement in _SCHEMA_STATEMENTS:
                await db.execute(statement)
            await db.commit()

    async def save_many(self, records: Iterable[WebSignalRecord]) -> int:
        """Idempotent upsert keyed by (workspace_id, source_id + item_url).

        A repeat on-demand run for the same source refreshes title/summary/
        fetched_at in place instead of accumulating duplicate rows -
        ``created_at`` is preserved from the first insert (never touched by
        the ON CONFLICT branch).
        """
        records = list(records)
        if not records:
            return 0
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                for record in records:
                    dedupe_key = _dedupe_key(record.source_id, record.item_url)
                    await db.execute(
                        "INSERT INTO web_source_signals "
                        "(workspace_id, source_id, dedupe_key, source_name, source_url, "
                        "item_url, title, summary, fetched_at, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(workspace_id, dedupe_key) DO UPDATE SET "
                        "source_name = excluded.source_name, title = excluded.title, "
                        "summary = excluded.summary, item_url = excluded.item_url, "
                        "fetched_at = excluded.fetched_at, updated_at = excluded.updated_at",
                        (
                            record.workspace_id, record.source_id, dedupe_key,
                            record.source_name, record.source_url, record.item_url,
                            record.title, record.summary, record.fetched_at, now, now,
                        ),
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return len(records)

    async def list_for_workspace(
        self, workspace_id: int, *, limit: int = 200
    ) -> list[WebSignalRecord]:
        """Signals currently visible to ``workspace_id``.

        A stored row is hidden the moment its source is disabled (workspace
        subscription) or deactivated (source_catalog) - the same
        fail-closed "visible now, not visible at collection time" rule
        WorkspaceSignalRepository.list_for_workspace already uses for legacy
        Radar signals.
        """
        if limit < 1:
            raise ValueError("limit должен быть положительным")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT w.* FROM web_source_signals w "
                "JOIN source_catalog c ON c.id = w.source_id "
                "JOIN workspace_source_subscriptions s "
                "ON s.source_id = c.id AND s.workspace_id = w.workspace_id "
                "WHERE w.workspace_id = ? AND c.status = 'active' AND s.enabled = 1 "
                "ORDER BY w.id DESC LIMIT ?",
                (workspace_id, limit),
            )).fetchall()
        return [_record(row) for row in rows]


def _record(row: aiosqlite.Row) -> WebSignalRecord:
    return WebSignalRecord(
        id=row["id"], workspace_id=row["workspace_id"], source_id=row["source_id"],
        source_name=row["source_name"], source_url=row["source_url"],
        item_url=row["item_url"], title=row["title"], summary=row["summary"],
        fetched_at=row["fetched_at"], created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _dedupe_key(source_id: str, item_url: str) -> str:
    raw = f"{source_id}|{item_url}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
