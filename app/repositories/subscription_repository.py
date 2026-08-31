"""Subscription state per workspace - beta/active/past_due/expired.

This is deliberately a SEPARATE table from partner_workspaces.access_status
(the existing Stage 3A trial/active/expired/suspended gate) - not a
replacement, not wired into AccessStateMiddleware. init() backfills every
already-provisioned workspace as 'beta' exactly once, so no existing
workspace loses access or changes behavior by this table's mere existence
- see the module docstring in app/domain/subscription.py for how (and
when) this is meant to connect to the live access gate later.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from app.domain.subscription import Subscription, SubscriptionStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace_subscriptions (
    workspace_id INTEGER PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'beta'
        CHECK (status IN ('beta', 'active', 'past_due', 'expired')),
    started_at TEXT NOT NULL,
    paid_until TEXT,
    external_payment_id TEXT,
    payment_provider TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES partner_workspaces(id)
);
"""


class SubscriptionRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.executescript(_SCHEMA)
            await db.commit()
            # Backfill: every workspace that existed before this table did
            # (all current test users) starts as 'beta', never re-touched
            # once a row exists - safe to call on every startup.
            now = _now()
            await db.execute(
                "INSERT INTO workspace_subscriptions "
                "(workspace_id, status, started_at, updated_at) "
                "SELECT id, 'beta', ?, ? FROM partner_workspaces "
                "WHERE id NOT IN (SELECT workspace_id FROM workspace_subscriptions)",
                (now, now),
            )
            await db.commit()

    async def ensure_beta(self, workspace_id: int) -> Subscription:
        """Called for a newly-provisioned workspace so it always has a
        subscription row from day one (idempotent - a pre-existing row is
        never overwritten, matching init()'s backfill semantics)."""
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute(
                "INSERT INTO workspace_subscriptions "
                "(workspace_id, status, started_at, updated_at) "
                "VALUES (?, 'beta', ?, ?) "
                "ON CONFLICT(workspace_id) DO NOTHING",
                (workspace_id, now, now),
            )
            await db.commit()
        row = await self.get_for_workspace(workspace_id)
        if row is None:
            raise RuntimeError("Не удалось создать subscription")
        return row

    async def get_for_workspace(self, workspace_id: int) -> Subscription | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM workspace_subscriptions WHERE workspace_id = ?",
                (workspace_id,),
            )
            row = await cursor.fetchone()
        return _from_row(row) if row is not None else None

    async def list_all(self) -> list[Subscription]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM workspace_subscriptions ORDER BY workspace_id"
            )
            rows = await cursor.fetchall()
        return [_from_row(row) for row in rows]

    async def mark_paid(
        self, workspace_id: int, *, external_payment_id: str, payment_provider: str,
        paid_until: str,
    ) -> Subscription | None:
        """RoboKassa integration point: call this from the verified-payment
        callback handler once the signature/amount check has passed - see
        the report for exactly where that handler would live. Never call
        this from an unverified request."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE workspace_subscriptions SET status='active', "
                "external_payment_id=?, payment_provider=?, paid_until=?, updated_at=? "
                "WHERE workspace_id=?",
                (external_payment_id, payment_provider, paid_until, _now(), workspace_id),
            )
            await db.commit()
        return await self.get_for_workspace(workspace_id)

    async def mark_past_due(self, workspace_id: int) -> Subscription | None:
        return await self._set_status(workspace_id, SubscriptionStatus.PAST_DUE)

    async def mark_expired(self, workspace_id: int) -> Subscription | None:
        return await self._set_status(workspace_id, SubscriptionStatus.EXPIRED)

    async def _set_status(
        self, workspace_id: int, status: SubscriptionStatus,
    ) -> Subscription | None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE workspace_subscriptions SET status=?, updated_at=? "
                "WHERE workspace_id=?",
                (status.value, _now(), workspace_id),
            )
            await db.commit()
        return await self.get_for_workspace(workspace_id)


def _from_row(row: aiosqlite.Row) -> Subscription:
    return Subscription(
        workspace_id=row["workspace_id"], status=SubscriptionStatus(row["status"]),
        started_at=row["started_at"], paid_until=row["paid_until"],
        external_payment_id=row["external_payment_id"],
        payment_provider=row["payment_provider"], updated_at=row["updated_at"],
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
