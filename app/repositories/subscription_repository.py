"""Subscription state per workspace - trial/beta/active/past_due/expired/
suspended. THE single source of truth for workspace access: both
AccessStateMiddleware (Telegram, app/access_state_gate.py) and the Web
subscription gate (app/web_api.py) resolve access through
resolve_access_state() below - never through partner_workspaces.access_status
anymore (that column is deprecated, see app/domain/partners.py; it is
still read once by init()'s migration backfill and nowhere else).

init() backfills every already-provisioned workspace that doesn't have a
row yet, carrying over any real (non-default) legacy access_status/
access_expires_at state so nobody's access silently changes the moment
this migration runs - see _seed_from_legacy_access_status(). A workspace
whose legacy state was never touched (the column default - which is every
real workspace today, since nothing in this codebase has ever written a
non-default access_status) grandfathers in as 'beta', exactly as before
this table became the live gate.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from app.domain.subscription import Subscription, SubscriptionPlan, SubscriptionStatus
from app.services.access_state import EXPIRED, compute_access_state

_TABLE_COLUMNS_SQL = """(
    workspace_id INTEGER PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'beta'
        CHECK (status IN ('trial', 'beta', 'active', 'past_due', 'expired', 'suspended')),
    plan TEXT NOT NULL DEFAULT 'beta'
        CHECK (plan IN ('beta', 'standard')),
    started_at TEXT NOT NULL,
    trial_until TEXT,
    paid_until TEXT,
    external_payment_id TEXT,
    payment_provider TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES partner_workspaces(id)
)"""

_SCHEMA = f"CREATE TABLE IF NOT EXISTS workspace_subscriptions {_TABLE_COLUMNS_SQL};"


class SubscriptionRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.executescript(_SCHEMA)
            await self._migrate_schema(db)
            await db.commit()
            await self._backfill_missing_rows(db)
            await db.commit()

    @staticmethod
    async def _migrate_schema(db: aiosqlite.Connection) -> None:
        """Additive migration for a workspace_subscriptions table created by
        an earlier version of this repository (4-value status CHECK, no
        plan/trial_until columns). CREATE TABLE IF NOT EXISTS in _SCHEMA is
        a no-op against an already-existing table, so it won't pick up the
        wider status CHECK or the new columns on its own - same situation
        as WebAuthRepository._migrate_onboarding_column. Rebuilds the table
        only when the stored CHECK constraint doesn't already allow
        'trial'/'suspended' (SQLite can't ALTER a CHECK constraint in
        place); a no-op on every later startup once the table matches the
        current schema.
        """
        cursor = await db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='workspace_subscriptions'"
        )
        row = await cursor.fetchone()
        needs_rebuild = row is not None and "'trial'" not in (row[0] or "")

        if needs_rebuild:
            await db.execute(
                "ALTER TABLE workspace_subscriptions "
                "RENAME TO workspace_subscriptions_legacy"
            )
            await db.execute(
                f"CREATE TABLE workspace_subscriptions {_TABLE_COLUMNS_SQL}"
            )
            await db.execute(
                "INSERT INTO workspace_subscriptions "
                "(workspace_id, status, started_at, paid_until, "
                "external_payment_id, payment_provider, updated_at) "
                "SELECT workspace_id, status, started_at, paid_until, "
                "external_payment_id, payment_provider, updated_at "
                "FROM workspace_subscriptions_legacy"
            )
            await db.execute("DROP TABLE workspace_subscriptions_legacy")

        cursor = await db.execute("PRAGMA table_info(workspace_subscriptions)")
        columns = {r[1] for r in await cursor.fetchall()}
        if "plan" not in columns:
            await db.execute(
                "ALTER TABLE workspace_subscriptions "
                "ADD COLUMN plan TEXT NOT NULL DEFAULT 'beta'"
            )
        if "trial_until" not in columns:
            await db.execute(
                "ALTER TABLE workspace_subscriptions ADD COLUMN trial_until TEXT"
            )

    @staticmethod
    async def _backfill_missing_rows(db: aiosqlite.Connection) -> None:
        """Called on every startup - only ever inserts a row for a
        workspace that doesn't have one yet (idempotent: a pre-existing
        row is never touched here, matching ensure_beta()'s own contract).
        """
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, access_status, access_expires_at FROM partner_workspaces "
            "WHERE id NOT IN (SELECT workspace_id FROM workspace_subscriptions)"
        )
        missing = await cursor.fetchall()
        now = _now()
        for row in missing:
            status, trial_until, paid_until = _seed_from_legacy_access_status(
                row["access_status"], row["access_expires_at"],
            )
            await db.execute(
                "INSERT INTO workspace_subscriptions "
                "(workspace_id, status, plan, started_at, trial_until, "
                "paid_until, updated_at) "
                "VALUES (?, ?, 'beta', ?, ?, ?, ?) "
                "ON CONFLICT(workspace_id) DO NOTHING",
                (row["id"], status.value, now, trial_until, paid_until, now),
            )

    async def ensure_beta(self, workspace_id: int) -> Subscription:
        """Called for a newly-provisioned workspace (or lazily, by
        resolve_access_state(), for one created after the last startup
        backfill) so it always has a subscription row - idempotent, a
        pre-existing row is never overwritten."""
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute(
                "INSERT INTO workspace_subscriptions "
                "(workspace_id, status, plan, started_at, updated_at) "
                "VALUES (?, 'beta', 'beta', ?, ?) "
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

    async def resolve_access_state(self, workspace_id: int) -> str:
        """THE single call site both Telegram (AccessStateMiddleware) and
        Web (app/web_api.py's subscription gate) use - guarantees they
        always compute the same access_state for the same workspace,
        because they're calling the exact same function, not two
        independent implementations of the same rule. Lazily provisions a
        missing row via ensure_beta() (a workspace created after the last
        startup backfill would otherwise have none yet). Any failure to
        read/construct the row fails closed as EXPIRED - never silently
        grants access on a broken read, same rule web_api.py's own
        get_current_principal already applies to membership checks.
        """
        try:
            subscription = await self.get_for_workspace(workspace_id)
            if subscription is None:
                subscription = await self.ensure_beta(workspace_id)
        except Exception:
            return EXPIRED
        return compute_access_state(
            subscription.status, subscription.trial_until, subscription.paid_until,
        )

    async def mark_paid(
        self, workspace_id: int, *, external_payment_id: str, payment_provider: str,
        paid_until: str, plan: SubscriptionPlan = SubscriptionPlan.STANDARD,
    ) -> Subscription | None:
        """RoboKassa integration point: call this from the verified-payment
        callback handler (see app.services.billing_service.BillingService)
        once the signature/amount check has passed. Never call this from an
        unverified request. plan defaults to STANDARD - the only paid plan
        this product has right now - and is written on every call
        (including a repeat/renewal), not just the first one, so a renewal
        can't leave a stale plan behind.

        Upsert, not a plain UPDATE: a workspace created after the last
        startup backfill (see init()) may not have a subscription row yet
        at the moment a payment webhook fires for it - a plain UPDATE
        would silently affect zero rows and the payment would be lost.
        """
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute(
                "INSERT INTO workspace_subscriptions "
                "(workspace_id, status, plan, started_at, paid_until, "
                "external_payment_id, payment_provider, updated_at) "
                "VALUES (?, 'active', ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(workspace_id) DO UPDATE SET "
                "status='active', plan=excluded.plan, "
                "paid_until=excluded.paid_until, "
                "external_payment_id=excluded.external_payment_id, "
                "payment_provider=excluded.payment_provider, "
                "updated_at=excluded.updated_at",
                (workspace_id, plan.value, now, paid_until, external_payment_id,
                 payment_provider, now),
            )
            await db.commit()
        return await self.get_for_workspace(workspace_id)

    async def start_trial(self, workspace_id: int, trial_until: str) -> Subscription | None:
        """Starts (or restarts) a time-boxed trial. Not wired to any real
        flow yet (no purchase UI - see the task notes); exists so the
        access model can represent 'trial' end-to-end and be exercised by
        tests/future admin tooling without another schema change. Upsert
        for the same reason as mark_paid()."""
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute(
                "INSERT INTO workspace_subscriptions "
                "(workspace_id, status, plan, started_at, trial_until, updated_at) "
                "VALUES (?, 'trial', 'beta', ?, ?, ?) "
                "ON CONFLICT(workspace_id) DO UPDATE SET "
                "status='trial', trial_until=excluded.trial_until, "
                "updated_at=excluded.updated_at",
                (workspace_id, now, trial_until, now),
            )
            await db.commit()
        return await self.get_for_workspace(workspace_id)

    async def mark_past_due(self, workspace_id: int) -> Subscription | None:
        return await self._set_status(workspace_id, SubscriptionStatus.PAST_DUE)

    async def mark_expired(self, workspace_id: int) -> Subscription | None:
        return await self._set_status(workspace_id, SubscriptionStatus.EXPIRED)

    async def mark_suspended(self, workspace_id: int) -> Subscription | None:
        """Administrative override (moderation/abuse), independent of
        billing - replaces the suspended state partner_workspaces.access_status
        used to represent. Wired to the Beta Control Center's "suspend"
        admin action (app/admin_api.py)."""
        return await self._set_status(workspace_id, SubscriptionStatus.SUSPENDED)

    async def mark_active(self, workspace_id: int) -> Subscription | None:
        """Administrative "restore" - the counterpart to mark_suspended().
        Only flips status back to 'active'; deliberately does not touch
        plan/paid_until (a restore is not a new grant of paid time - see
        mark_paid() for that), so restoring a suspended workspace brings
        back exactly the access it had before suspension, no more."""
        return await self._set_status(workspace_id, SubscriptionStatus.ACTIVE)

    async def _set_status(
        self, workspace_id: int, status: SubscriptionStatus,
    ) -> Subscription | None:
        """Upsert for the same reason as mark_paid() - a workspace can have
        no row yet at the moment an admin/ops action targets it."""
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute(
                "INSERT INTO workspace_subscriptions "
                "(workspace_id, status, plan, started_at, updated_at) "
                "VALUES (?, ?, 'beta', ?, ?) "
                "ON CONFLICT(workspace_id) DO UPDATE SET "
                "status=excluded.status, updated_at=excluded.updated_at",
                (workspace_id, status.value, now, now),
            )
            await db.commit()
        return await self.get_for_workspace(workspace_id)


def _seed_from_legacy_access_status(
    access_status: str, access_expires_at: str | None,
) -> tuple[SubscriptionStatus, str | None, str | None]:
    """One-time backfill mapping from the deprecated
    partner_workspaces.access_status/access_expires_at gate to an initial
    workspace_subscriptions row - only used for a workspace that doesn't
    have a subscription row yet (see _backfill_missing_rows/ensure_beta).
    A workspace whose legacy state was never customized (access_status
    default 'active', access_expires_at NULL - indistinguishable from
    "nobody ever touched this") grandfathers in as 'beta', same as this
    table's original backfill before it became the live gate. A workspace
    with a real non-default legacy state (manually suspended, mid-trial,
    or given a real expiry date) keeps that exact state, so access doesn't
    silently change the moment this migration runs. Returns
    (status, trial_until, paid_until).
    """
    if access_status == "suspended":
        return SubscriptionStatus.SUSPENDED, None, None
    if access_status == "trial_active":
        return SubscriptionStatus.TRIAL, access_expires_at, None
    if access_status == "expired":
        return SubscriptionStatus.EXPIRED, None, access_expires_at
    if access_status == "active" and access_expires_at is not None:
        return SubscriptionStatus.ACTIVE, None, access_expires_at
    return SubscriptionStatus.BETA, None, None


def _from_row(row: aiosqlite.Row) -> Subscription:
    return Subscription(
        workspace_id=row["workspace_id"],
        status=SubscriptionStatus(row["status"]),
        plan=SubscriptionPlan(row["plan"]),
        started_at=row["started_at"],
        trial_until=row["trial_until"],
        paid_until=row["paid_until"],
        external_payment_id=row["external_payment_id"],
        payment_provider=row["payment_provider"],
        updated_at=row["updated_at"],
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
