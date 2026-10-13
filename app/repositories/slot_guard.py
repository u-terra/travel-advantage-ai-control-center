"""Atomic plan-slot guard shared by every write path that connects a
competitor or a monitoring source (web, Telegram, admin CLI, payment-time
default pack, legacy import).

Why it lives in the write transaction: a "read count, then insert" done in
two separate connections lets two parallel requests both see the last free
slot and both insert. Here the repositories open `BEGIN IMMEDIATE` (SQLite's
single-writer lock), call ensure_free_slot() / free_slots() on THE SAME
connection, and insert before committing - so the count and the insert are
one atomic step and the quota cannot be exceeded by concurrency.

The plan is read from workspace_subscriptions on that same connection and
interpreted by app.services.plan_limits.resolve_plan - the same rules
PlanQuotaService uses for its pre-checks:
  - explicit 'beta'              -> unlimited;
  - START / STANDARD / FULL      -> slot limit of the plan;
  - no subscription / unknown    -> blocked with a clear message.

The count definitions are deliberately identical to
CompetitorRepository.count_for_workspace and
SourceCatalogRepository.count_for_workspace (sources: enabled + disabled).
Nothing here ever deletes or disables existing rows: a workspace already
above its limit keeps everything and is simply refused NEW slots.
"""

from __future__ import annotations

import sqlite3

import aiosqlite

from app.services.plan_limits import (
    UNKNOWN_PLAN_MESSAGE,
    competitor_slot_message,
    resolve_plan,
    source_slot_message,
)

SLOT_COMPETITORS = "competitors"
SLOT_SOURCES = "sources"

_COUNT_SQL = {
    SLOT_COMPETITORS: "SELECT COUNT(*) FROM competitors WHERE workspace_id = ?",
    SLOT_SOURCES: (
        "SELECT COUNT(*) FROM workspace_source_subscriptions WHERE workspace_id = ?"
    ),
}


class SlotLimitReached(RuntimeError):
    """A new competitor/source slot is refused. ``message`` is user-facing
    Russian text (plan limit reached, or the plan could not be determined)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


async def _plan_of(db: aiosqlite.Connection, workspace_id: int) -> str | None:
    try:
        cursor = await db.execute(
            "SELECT plan FROM workspace_subscriptions WHERE workspace_id = ?",
            (workspace_id,),
        )
        row = await cursor.fetchone()
    except sqlite3.OperationalError:
        return None  # table not provisioned -> no subscription
    return str(row[0]) if row is not None else None


async def free_slots(
    db: aiosqlite.Connection,
    workspace_id: int,
    kind: str,
    *,
    unprovisioned_is_unlimited: bool = False,
) -> int | None:
    """Free slots of ``kind`` right now, on the caller's connection/
    transaction. None = unlimited (explicit beta). Never negative: a
    workspace above its limit has 0 free slots. Raises SlotLimitReached when
    the plan itself blocks (no subscription / unknown plan).

    ``unprovisioned_is_unlimited`` is for the one-time legacy import only,
    which runs before subscription rows exist on a fresh database."""
    plan = await _plan_of(db, workspace_id)
    if plan is None and unprovisioned_is_unlimited:
        return None
    resolution = resolve_plan(plan)
    if resolution.block_message is not None:
        raise SlotLimitReached(resolution.block_message)
    limits = resolution.limits
    if limits is None:
        return None
    cursor = await db.execute(_COUNT_SQL[kind], (workspace_id,))
    row = await cursor.fetchone()
    used = int(row[0]) if row is not None else 0
    limit = (
        limits.competitor_slot_limit if kind == SLOT_COMPETITORS
        else limits.source_slot_limit
    )
    return max(limit - used, 0)


async def ensure_free_slot(
    db: aiosqlite.Connection, workspace_id: int, kind: str,
) -> None:
    """Raise SlotLimitReached unless one more slot of ``kind`` fits."""
    free = await free_slots(db, workspace_id, kind)
    if free is None or free >= 1:
        return
    limits = resolve_plan(await _plan_of(db, workspace_id)).limits
    if limits is None:  # plan vanished mid-transaction: stay closed
        raise SlotLimitReached(UNKNOWN_PLAN_MESSAGE)
    raise SlotLimitReached(
        competitor_slot_message(limits) if kind == SLOT_COMPETITORS
        else source_slot_message(limits)
    )
