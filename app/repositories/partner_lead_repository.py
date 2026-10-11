"""partner_leads - minimal persistent lead tracking for Partner Discovery
2.0 (see app/domain/partner_leads.py for the deliberately-small status
set and allowed-transition table).

Deliberately NOT a CRM: one table, upsert-on-rediscovery (never creates
duplicates for the same workspace+identity, never resets an already-
advanced status back to "new"), a small fixed status set, no analytics,
no outreach history beyond last_contact_at/next_action. Discovery/
qualification themselves remain fully stateless - this table only
persists a candidate once the user has actually acted on it (saved/
approved/skipped/drafted), per app/handlers/partner_discovery.py and
app/web_api.py's own wiring.

Plain aiosqlite, additive schema, same conventions as every other
repository here (see app.repositories.contact_claim_repository).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from app.domain.partner_leads import ALLOWED_TRANSITIONS, PartnerLead, PartnerLeadStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS partner_leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    identity_key TEXT NOT NULL,
    name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    score INTEGER,
    confidence REAL,
    why_fit_json TEXT NOT NULL DEFAULT '[]',
    hook TEXT NOT NULL DEFAULT '',
    contact_type TEXT,
    contact_value TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_contact_at TEXT,
    next_action TEXT,
    notes TEXT,
    UNIQUE(workspace_id, identity_key)
);
CREATE INDEX IF NOT EXISTS idx_partner_leads_workspace_status
    ON partner_leads(workspace_id, status, updated_at);
"""


@dataclass(frozen=True)
class StatusChangeResult:
    ok: bool
    # Populated only when ok is False - a human-readable reason, never
    # exposed as an exception: callers (Telegram/Web) render it directly.
    reason: str | None = None
    lead: PartnerLead | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_lead(row: aiosqlite.Row) -> PartnerLead:
    return PartnerLead(
        lead_id=row["id"],
        workspace_id=row["workspace_id"],
        identity_key=row["identity_key"],
        name=row["name"],
        source_url=row["source_url"],
        score=row["score"],
        confidence=row["confidence"],
        why_fit=tuple(json.loads(row["why_fit_json"] or "[]")),
        hook=row["hook"] or "",
        contact_type=row["contact_type"],
        contact_value=row["contact_value"],
        status=PartnerLeadStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        last_contact_at=datetime.fromisoformat(row["last_contact_at"]) if row["last_contact_at"] else None,
        next_action=row["next_action"],
        notes=row["notes"],
    )


class PartnerLeadRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.executescript(_SCHEMA)
            await db.commit()

    async def upsert_from_candidate(
        self, workspace_id: int, identity_key: str, candidate: dict,
    ) -> PartnerLead:
        """Creates a new lead as PartnerLeadStatus.NEW, or - if this
        workspace already saved this same identity before - refreshes
        enrichment/score/confidence/why_fit/hook/contact fields WITHOUT
        touching status/created_at (re-discovering an already-worked
        candidate must never reset progress back to "new")."""
        now = _now()
        why_fit_json = json.dumps(list(candidate.get("why_fit") or []), ensure_ascii=False)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            existing = await (await db.execute(
                "SELECT * FROM partner_leads WHERE workspace_id = ? AND identity_key = ?",
                (workspace_id, identity_key),
            )).fetchone()
            if existing is not None:
                await db.execute(
                    "UPDATE partner_leads SET name = ?, source_url = ?, score = ?, "
                    "confidence = ?, why_fit_json = ?, hook = ?, contact_type = ?, "
                    "contact_value = ?, updated_at = ? WHERE id = ?",
                    (
                        candidate.get("display_name") or candidate.get("name") or "",
                        candidate.get("url") or "",
                        candidate.get("total_score"),
                        candidate.get("confidence"),
                        why_fit_json,
                        candidate.get("hook") or "",
                        candidate.get("contact_type"),
                        candidate.get("contact_value"),
                        now,
                        existing["id"],
                    ),
                )
                await db.commit()
                row = await (await db.execute(
                    "SELECT * FROM partner_leads WHERE id = ?", (existing["id"],),
                )).fetchone()
                return _row_to_lead(row)

            await db.execute(
                "INSERT INTO partner_leads (workspace_id, identity_key, name, source_url, "
                "score, confidence, why_fit_json, hook, contact_type, contact_value, "
                "status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    workspace_id, identity_key,
                    candidate.get("display_name") or candidate.get("name") or "",
                    candidate.get("url") or "",
                    candidate.get("total_score"),
                    candidate.get("confidence"),
                    why_fit_json,
                    candidate.get("hook") or "",
                    candidate.get("contact_type"),
                    candidate.get("contact_value"),
                    PartnerLeadStatus.NEW.value, now, now,
                ),
            )
            await db.commit()
            lead_id = (await (await db.execute("SELECT last_insert_rowid()")).fetchone())[0]
            row = await (await db.execute(
                "SELECT * FROM partner_leads WHERE id = ?", (lead_id,),
            )).fetchone()
            return _row_to_lead(row)

    async def get(self, lead_id: int, workspace_id: int) -> PartnerLead | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                "SELECT * FROM partner_leads WHERE id = ? AND workspace_id = ?",
                (lead_id, workspace_id),
            )).fetchone()
            return _row_to_lead(row) if row is not None else None

    async def list_for_workspace(
        self, workspace_id: int, *, statuses: list[PartnerLeadStatus] | None = None,
        limit: int = 20,
    ) -> list[PartnerLead]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if statuses:
                placeholders = ",".join("?" for _ in statuses)
                query = (
                    f"SELECT * FROM partner_leads WHERE workspace_id = ? "
                    f"AND status IN ({placeholders}) ORDER BY updated_at DESC LIMIT ?"
                )
                params = (workspace_id, *[s.value for s in statuses], limit)
            else:
                query = (
                    "SELECT * FROM partner_leads WHERE workspace_id = ? "
                    "ORDER BY updated_at DESC LIMIT ?"
                )
                params = (workspace_id, limit)
            rows = await (await db.execute(query, params)).fetchall()
            return [_row_to_lead(row) for row in rows]

    async def set_status(
        self, lead_id: int, workspace_id: int, new_status: PartnerLeadStatus,
        *, next_action: str | None = None, mark_contacted: bool = False,
    ) -> StatusChangeResult:
        """Refuses any transition not explicitly listed in
        ALLOWED_TRANSITIONS - e.g. INTERESTED can never be moved back to
        NEW by this method, no matter what caller passes. mark_contacted
        stamps last_contact_at (only meaningful for -> CONTACTED)."""
        lead = await self.get(lead_id, workspace_id)
        if lead is None:
            return StatusChangeResult(ok=False, reason="lead_not_found")
        if new_status not in ALLOWED_TRANSITIONS.get(lead.status, frozenset()):
            return StatusChangeResult(
                ok=False, reason=f"transition_not_allowed:{lead.status.value}->{new_status.value}",
                lead=lead,
            )
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if mark_contacted:
                await db.execute(
                    "UPDATE partner_leads SET status = ?, updated_at = ?, "
                    "last_contact_at = ?, next_action = ? WHERE id = ? AND workspace_id = ?",
                    (new_status.value, now, now, next_action, lead_id, workspace_id),
                )
            else:
                await db.execute(
                    "UPDATE partner_leads SET status = ?, updated_at = ?, "
                    "next_action = COALESCE(?, next_action) WHERE id = ? AND workspace_id = ?",
                    (new_status.value, now, next_action, lead_id, workspace_id),
                )
            await db.commit()
            row = await (await db.execute(
                "SELECT * FROM partner_leads WHERE id = ?", (lead_id,),
            )).fetchone()
            return StatusChangeResult(ok=True, lead=_row_to_lead(row))
