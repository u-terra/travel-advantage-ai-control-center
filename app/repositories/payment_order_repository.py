"""Payment orders (RoboKassa InvId ledger) - see app/domain/billing.py for
why this is separate from workspace_subscriptions. Same conventions as
every other repository here: plain aiosqlite, additive schema, workspace
isolation via WHERE-clause scoping.

Never stores a RoboKassa password, signature, or any other secret - only
what's needed to correlate a callback back to a workspace/plan/amount and
record whether it was ever confirmed paid.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from app.domain.billing import PaymentOrder, PaymentOrderStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace_payment_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    plan TEXT NOT NULL CHECK (plan IN ('standard', 'start', 'full')),
    amount TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'RUB',
    provider TEXT NOT NULL DEFAULT 'robokassa',
    status TEXT NOT NULL DEFAULT 'created' CHECK (status IN ('created', 'paid')),
    created_at TEXT NOT NULL,
    paid_at TEXT,
    duration_days INTEGER NOT NULL DEFAULT 30,
    owner_notified_at TEXT,
    FOREIGN KEY (workspace_id) REFERENCES partner_workspaces(id)
);
CREATE INDEX IF NOT EXISTS idx_workspace_payment_orders_workspace
    ON workspace_payment_orders(workspace_id, id DESC);
"""


class PaymentOrderRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.executescript(_SCHEMA)
            await db.commit()
            await self._migrate_schema(db)
            await db.commit()

    @staticmethod
    async def _migrate_schema(db: aiosqlite.Connection) -> None:
        """Additive migration for a workspace_payment_orders table created
        before 'start'/'full' plan values or duration_days existed.
        CREATE TABLE IF NOT EXISTS in _SCHEMA is a no-op against an
        already-existing table (same situation as every other repository
        here), so a stored CHECK that only allows plan='standard' needs a
        rebuild (SQLite can't ALTER a CHECK in place); duration_days alone
        is a plain additive ADD COLUMN. A no-op on every later startup once
        the table matches the current schema."""
        cursor = await db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='workspace_payment_orders'"
        )
        row = await cursor.fetchone()
        needs_rebuild = row is not None and "'start'" not in (row[0] or "")

        if needs_rebuild:
            legacy_columns_cursor = await db.execute(
                "PRAGMA table_info(workspace_payment_orders)"
            )
            legacy_columns = {r[1] for r in await legacy_columns_cursor.fetchall()}
            copyable = [
                c for c in (
                    "id", "workspace_id", "plan", "amount", "currency", "provider",
                    "status", "created_at", "paid_at", "duration_days", "owner_notified_at",
                ) if c in legacy_columns
            ]
            column_list = ", ".join(copyable)
            await db.execute(
                "ALTER TABLE workspace_payment_orders "
                "RENAME TO workspace_payment_orders_legacy"
            )
            await db.execute(
                "CREATE TABLE workspace_payment_orders (\n"
                "    id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
                "    workspace_id INTEGER NOT NULL,\n"
                "    plan TEXT NOT NULL CHECK (plan IN ('standard', 'start', 'full')),\n"
                "    amount TEXT NOT NULL,\n"
                "    currency TEXT NOT NULL DEFAULT 'RUB',\n"
                "    provider TEXT NOT NULL DEFAULT 'robokassa',\n"
                "    status TEXT NOT NULL DEFAULT 'created' CHECK (status IN ('created', 'paid')),\n"
                "    created_at TEXT NOT NULL,\n"
                "    paid_at TEXT,\n"
                "    duration_days INTEGER NOT NULL DEFAULT 30,\n"
                "    owner_notified_at TEXT,\n"
                "    FOREIGN KEY (workspace_id) REFERENCES partner_workspaces(id)\n"
                ")"
            )
            await db.execute(
                f"INSERT INTO workspace_payment_orders ({column_list}) "
                f"SELECT {column_list} FROM workspace_payment_orders_legacy"
            )
            await db.execute("DROP TABLE workspace_payment_orders_legacy")
            await db.commit()

        cursor = await db.execute("PRAGMA table_info(workspace_payment_orders)")
        columns = {r[1] for r in await cursor.fetchall()}
        if "duration_days" not in columns:
            await db.execute(
                "ALTER TABLE workspace_payment_orders "
                "ADD COLUMN duration_days INTEGER NOT NULL DEFAULT 30"
            )
        if "owner_notified_at" not in columns:
            # Owner payment notification dedup marker (see
            # mark_owner_notified/app.services.owner_payment_notifications) -
            # plain nullable ADD COLUMN, same additive-migration convention
            # as duration_days above. NULL for every pre-existing row -
            # never backfilled/guessed, matching this codebase's rule that a
            # missing value here means "not available", never a fabricated
            # one.
            await db.execute(
                "ALTER TABLE workspace_payment_orders "
                "ADD COLUMN owner_notified_at TEXT"
            )

    async def create_order(
        self, *, workspace_id: int, plan: str, amount: str, currency: str = "RUB",
        provider: str = "robokassa", duration_days: int = 30,
    ) -> PaymentOrder:
        """id (the AUTOINCREMENT PK) IS the RoboKassa InvId - callers send
        the returned order.id straight to RoboKassa as InvId, never a
        separately-generated value. duration_days is frozen onto the order
        at creation time (see app.services.plans.PLAN_CATALOG) so a later
        catalog change never retroactively changes what an
        already-created order is worth."""
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            cursor = await db.execute(
                "INSERT INTO workspace_payment_orders "
                "(workspace_id, plan, amount, currency, provider, status, created_at, duration_days) "
                "VALUES (?, ?, ?, ?, ?, 'created', ?, ?)",
                (workspace_id, plan, amount, currency, provider, now, duration_days),
            )
            await db.commit()
            order_id = cursor.lastrowid
        row = await self.get_order(order_id)
        if row is None:
            raise RuntimeError("Не удалось создать payment order")
        return row

    async def count_since(self, since_iso: str, *, status: str | None = None) -> int:
        """Beta Control Center dashboard only (app/admin_api.py) - global
        (cross-tenant) count."""
        clause = "created_at >= ?"
        params: list[object] = [since_iso]
        if status is not None:
            clause += " AND status = ?"
            params.append(status)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                f"SELECT COUNT(*) FROM workspace_payment_orders WHERE {clause}", params,
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def list_for_workspace(
        self, workspace_id: int, *, limit: int = 50,
    ) -> list[PaymentOrder]:
        """Workspace card in the Beta Control Center (app/admin_api.py) -
        every order for one workspace, newest first."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM workspace_payment_orders WHERE workspace_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (workspace_id, limit),
            )
            rows = await cursor.fetchall()
        return [_from_row(row) for row in rows]

    async def get_order(self, order_id: int) -> PaymentOrder | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM workspace_payment_orders WHERE id = ?",
                (order_id,),
            )
            row = await cursor.fetchone()
        return _from_row(row) if row is not None else None

    async def mark_paid(self, order_id: int) -> tuple[PaymentOrder | None, bool]:
        """Atomic, idempotent status transition: created -> paid.

        Returns (order, transitioned_now). transitioned_now is True only
        when THIS call actually flipped the row from 'created' to 'paid' -
        the caller (see app.services.billing_service.BillingService) must
        extend the subscription ONLY when transitioned_now is True. A
        replayed RoboKassa notification for an order that's already 'paid'
        hits the WHERE status != 'paid' guard, updates zero rows, and gets
        transitioned_now=False - so a repeat ResultURL can never extend the
        subscription a second time. order is None only if order_id doesn't
        exist at all.
        """
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "UPDATE workspace_payment_orders SET status='paid', paid_at=? "
                "WHERE id=? AND status != 'paid'",
                (now, order_id),
            )
            transitioned = cursor.rowcount > 0
            await db.commit()
        order = await self.get_order(order_id)
        return order, transitioned

    async def mark_owner_notified(self, order_id: int) -> None:
        """Records that the owner payment notification (see
        app.services.owner_payment_notifications) was actually delivered -
        called ONLY after a successful Telegram send, never before and
        never on failure, so a failed send leaves this NULL and observably
        retryable later (e.g. by ops tooling querying status='paid' AND
        owner_notified_at IS NULL) without touching payment/subscription
        state at all. Not itself the anti-duplicate guard - that guarantee
        already comes from mark_paid()'s atomic created->paid transition
        (see BillingService.process_result_callback, the only caller of
        both): this column exists for auditability and safe manual retry,
        not for concurrency safety."""
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE workspace_payment_orders SET owner_notified_at=? WHERE id=?",
                (now, order_id),
            )
            await db.commit()


def _from_row(row: aiosqlite.Row) -> PaymentOrder:
    return PaymentOrder(
        id=row["id"],
        workspace_id=row["workspace_id"],
        plan=row["plan"],
        amount=row["amount"],
        currency=row["currency"],
        provider=row["provider"],
        status=PaymentOrderStatus(row["status"]),
        created_at=row["created_at"],
        paid_at=row["paid_at"],
        duration_days=row["duration_days"],
        owner_notified_at=row["owner_notified_at"],
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
