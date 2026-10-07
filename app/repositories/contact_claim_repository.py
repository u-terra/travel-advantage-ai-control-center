"""contact_claims - minimal cross-tenant outreach-claim guard (2026-10-07)
for Partner Discovery / Contact Discovery (app/handlers/partner_discovery.py).

Scope, deliberately narrow - see the task this was built for:
- A claim is created ONLY when a workspace actually STARTS outreach to a
  PERSON (the "✉️ Подготовить первое сообщение" button or its
  natural-language equivalent / the enrollment-link invitation) - never by
  Partner Discovery showing a prospect card, never by Contact Discovery
  alone finding a person.
- No permanent UNIQUE constraint: a claim expires CLAIM_COOLDOWN_DAYS after
  it was made. After that, the same identity can be claimed by a different
  workspace again - this is a cooldown, not a lifetime reservation.
- The SAME workspace that already holds an active claim may keep working
  with that contact (claim() is idempotent for the holder - no new row,
  no extended expiry, same semantics every call).
- NOT a CRM: no statuses, no lead pipeline, no analytics - one table, one
  read-then-maybe-write method.

Plain aiosqlite, additive schema, same conventions as every other
repository here (see app.repositories.operational_event_repository).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS contact_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_identity_key TEXT NOT NULL,
    workspace_id INTEGER NOT NULL,
    claimed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contact_claims_identity_expires
    ON contact_claims(contact_identity_key, expires_at);
CREATE INDEX IF NOT EXISTS idx_contact_claims_workspace
    ON contact_claims(workspace_id, expires_at);
"""

CLAIM_COOLDOWN_DAYS = 30


@dataclass(frozen=True)
class ClaimResult:
    # True = this workspace may proceed with outreach right now (either it
    # already held the active claim, or none existed and one was just
    # created).
    allowed: bool
    # True ONLY when a DIFFERENT workspace currently holds an active claim -
    # callers (app/handlers/partner_discovery.py) use this to show a
    # neutral message and must NEVER surface which workspace holds it.
    held_by_other: bool = False


class ContactClaimRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.executescript(_SCHEMA)
            await db.commit()

    async def claim(self, contact_identity_key: str, workspace_id: int) -> ClaimResult:
        """Starts (or continues) outreach to this identity for this
        workspace - see ClaimResult for the exact semantics. Safe to call
        every time a workspace begins/continues outreach to the same
        contact; never creates a duplicate active claim for the same
        (identity, workspace) pair."""
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                existing = await (await db.execute(
                    "SELECT workspace_id FROM contact_claims "
                    "WHERE contact_identity_key = ? AND expires_at > ? "
                    "ORDER BY claimed_at DESC LIMIT 1",
                    (contact_identity_key, now),
                )).fetchone()
                if existing is not None and existing["workspace_id"] != workspace_id:
                    await db.rollback()
                    return ClaimResult(allowed=False, held_by_other=True)
                if existing is not None and existing["workspace_id"] == workspace_id:
                    # Already claimed by this same workspace - nothing new
                    # to persist, this workspace may keep working with it.
                    await db.rollback()
                    return ClaimResult(allowed=True)
                await db.execute(
                    "INSERT INTO contact_claims "
                    "(contact_identity_key, workspace_id, claimed_at, expires_at) "
                    "VALUES (?, ?, ?, ?)",
                    (contact_identity_key, workspace_id, now, _now_plus_days(CLAIM_COOLDOWN_DAYS)),
                )
                await db.commit()
                return ClaimResult(allowed=True)
            except BaseException:
                await db.rollback()
                raise


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_plus_days(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
