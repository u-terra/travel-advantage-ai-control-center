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
    plan TEXT NOT NULL CHECK (plan IN ('standard')),
    amount TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'RUB',
    provider TEXT NOT NULL DEFAULT 'robokassa',
    status TEXT NOT NULL DEFAULT 'created' CHECK (status IN ('created', 'paid')),
    created_at TEXT NOT NULL,
    paid_at TEXT,
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

    async def create_order(
        self, *, workspace_id: int, plan: str, amount: str, currency: str = "RUB",
        provider: str = "robokassa",
    ) -> PaymentOrder:
        """id (the AUTOINCREMENT PK) IS the RoboKassa InvId - callers send
        the returned order.id straight to RoboKassa as InvId, never a
        separately-generated value."""
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            cursor = await db.execute(
                "INSERT INTO workspace_payment_orders "
                "(workspace_id, plan, amount, currency, provider, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'created', ?)",
                (workspace_id, plan, amount, currency, provider, now),
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
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
