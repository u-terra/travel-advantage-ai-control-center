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
    -- Stage 3: only ever set from a reliably-found signal - never guessed/
    -- estimated. NULL (unknown) is the honest default, not "".
    published_at TEXT,
    -- Stage 3: 1 for the landing-page-only fallback (no specific article
    -- could be discovered for this source this run), 0 for a real
    -- discovered article. Ranked below real articles - see
    -- list_for_workspace()'s ORDER BY.
    is_fallback INTEGER NOT NULL DEFAULT 0 CHECK (is_fallback IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES partner_workspaces(id),
    FOREIGN KEY (source_id) REFERENCES source_catalog(id),
    UNIQUE (workspace_id, dedupe_key)
)""",
"""CREATE INDEX IF NOT EXISTS idx_web_source_signals_workspace
    ON web_source_signals(workspace_id, id)""",
)

# Stage 3 added published_at/is_fallback to a table Stage 2 already shipped
# (and which already has live rows on production) - same additive-migration
# convention as app.repositories.partner_repository/subscription_repository/
# payment_order_repository: PRAGMA table_info() first, ALTER TABLE ADD
# COLUMN only for whichever of the two is actually missing, so a fresh
# install (single CREATE TABLE, already has both) and an upgrade (Stage-2-
# only table on disk) both end up with the identical final schema.
_STAGE3_COLUMNS = (
    ("published_at", "ALTER TABLE web_source_signals ADD COLUMN published_at TEXT"),
    (
        "is_fallback",
        "ALTER TABLE web_source_signals ADD COLUMN is_fallback INTEGER "
        "NOT NULL DEFAULT 0 CHECK (is_fallback IN (0, 1))",
    ),
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
    # Only ever a reliably-found date - see the schema comment above.
    published_at: str | None = None
    is_fallback: bool = False
    created_at: str = ""
    updated_at: str = ""


class WebSignalRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            for statement in _SCHEMA_STATEMENTS:
                await db.execute(statement)
            columns = {
                row["name"]
                for row in await (await db.execute(
                    "PRAGMA table_info(web_source_signals)"
                )).fetchall()
            }
            for name, ddl in _STAGE3_COLUMNS:
                if name not in columns:
                    await db.execute(ddl)
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
                        "item_url, title, summary, fetched_at, published_at, is_fallback, "
                        "created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(workspace_id, dedupe_key) DO UPDATE SET "
                        "source_name = excluded.source_name, title = excluded.title, "
                        "summary = excluded.summary, item_url = excluded.item_url, "
                        "fetched_at = excluded.fetched_at, published_at = excluded.published_at, "
                        "is_fallback = excluded.is_fallback, updated_at = excluded.updated_at",
                        (
                            record.workspace_id, record.source_id, dedupe_key,
                            record.source_name, record.source_url, record.item_url,
                            record.title, record.summary, record.fetched_at,
                            record.published_at, int(record.is_fallback), now, now,
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

        Ordering: real discovered articles always rank ahead of a landing-
        page fallback (``is_fallback`` ASC first - see Stage 3's schema
        comment), newest first within each group. This holds regardless of
        which order a single collection run happened to insert/update rows
        in - it is not just an accident of insertion order.
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
                "ORDER BY w.is_fallback ASC, w.id DESC LIMIT ?",
                (workspace_id, limit),
            )).fetchall()
        return [_record(row) for row in rows]


def _record(row: aiosqlite.Row) -> WebSignalRecord:
    return WebSignalRecord(
        id=row["id"], workspace_id=row["workspace_id"], source_id=row["source_id"],
        source_name=row["source_name"], source_url=row["source_url"],
        item_url=row["item_url"], title=row["title"], summary=row["summary"],
        fetched_at=row["fetched_at"], published_at=row["published_at"],
        is_fallback=bool(row["is_fallback"]), created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _dedupe_key(source_id: str, item_url: str) -> str:
    raw = f"{source_id}|{item_url}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
