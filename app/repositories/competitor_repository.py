"""Workspace-scoped хранилище ссылок на конкурентов.

Только хранение и чтение того, что владелец workspace сам счёл конкурентом.
Никакого сбора, обхода сайтов или анализа здесь нет — это база для будущего
конкурентного анализа, не сам анализ.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path

import aiosqlite

from app.domain.competitor_discovery import (
    CandidateClassification,
    CandidateConfidence,
    CandidateStatus,
    CompetitorCandidate,
    canonical_domain,
)
from app.domain.competitors import Competitor
from app.domain.competitor_intelligence import (
    CompetitorIntelligence,
    CompetitorSourceEvidence,
    ContentOpportunity,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS competitors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    url TEXT NOT NULL,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES partner_workspaces(id),
    CHECK (length(trim(url)) > 0),
    CHECK (length(trim(label)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_competitors_workspace
    ON competitors(workspace_id, id DESC);

CREATE TABLE IF NOT EXISTS competitor_intelligence_snapshots (
    competitor_id INTEGER PRIMARY KEY,
    workspace_id INTEGER NOT NULL,
    analyzed_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    FOREIGN KEY (competitor_id) REFERENCES competitors(id)
);

-- Competitor Discovery Radar: candidates found from public market signals,
-- before an owner promotes one into `competitors` (add_competitor below).
-- UNIQUE(workspace_id, canonical_domain) is the dedup: nl.trip.com /
-- www.trip.com / trip.com all upsert into the SAME row instead of spawning
-- one candidate per locale/URL variant.
CREATE TABLE IF NOT EXISTS competitor_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    canonical_domain TEXT NOT NULL,
    discovered_url TEXT NOT NULL,
    source_title TEXT NOT NULL,
    source_url TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    description TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    confidence TEXT NOT NULL,
    classification TEXT NOT NULL,
    why_it_matters TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    FOREIGN KEY (workspace_id) REFERENCES partner_workspaces(id),
    UNIQUE (workspace_id, canonical_domain),
    CHECK (length(trim(name)) > 0),
    CHECK (length(trim(canonical_domain)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_competitor_candidates_workspace
    ON competitor_candidates(workspace_id, id DESC);
"""

_MAX_URL_LENGTH = 500
_MAX_LABEL_LENGTH = 100


class CompetitorAddressError(ValueError):
    """Ссылка на конкурента пуста, слишком длинная или без схемы http(s)."""


class CompetitorLabelError(ValueError):
    """Название конкурента пусто или слишком длинное."""


class CompetitorRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.executescript(_SCHEMA)
            await db.commit()

    async def add_competitor(
        self, workspace_id: int, url: str, label: str | None = None,
    ) -> Competitor:
        """Stage 3.2: ``label`` is optional and backward compatible - when
        omitted or blank, behavior is unchanged from before (label = url).
        Passing a non-blank label stores that human-readable name instead;
        existing rows (label == url) are never touched by this method."""
        address = _validate_address(url)
        resolved_label = _validate_label(label) if label and label.strip() else address
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            cursor = await db.execute(
                "INSERT INTO competitors (workspace_id, url, label, created_at) "
                "VALUES (?, ?, ?, ?)",
                (workspace_id, address, resolved_label, now),
            )
            await db.commit()
            row = await self._row(db, workspace_id, cursor.lastrowid or 0)
        if row is None:
            raise RuntimeError("Не удалось сохранить конкурента")
        return _from_row(row)

    async def update_label(
        self, workspace_id: int, competitor_id: int, label: str,
    ) -> Competitor | None:
        """Renames an already-saved competitor. Workspace isolation is
        enforced by the WHERE clause itself (not a separate check): a
        competitor_id belonging to a different workspace matches zero rows
        and this returns None, exactly like "not found" - it never leaks
        whether the id exists in someone else's workspace."""
        resolved_label = _validate_label(label)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute(
                "UPDATE competitors SET label = ? WHERE workspace_id = ? AND id = ?",
                (resolved_label, workspace_id, competitor_id),
            )
            await db.commit()
            row = await self._row(db, workspace_id, competitor_id)
        return _from_row(row) if row is not None else None

    async def list_for_workspace(
        self, workspace_id: int, limit: int = 20
    ) -> list[Competitor]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM competitors WHERE workspace_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (workspace_id, _limit(limit)),
            )
            rows = await cursor.fetchall()
        return [_from_row(row) for row in rows]

    async def get_for_workspace(
        self, workspace_id: int, competitor_id: int,
    ) -> Competitor | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await self._row(db, workspace_id, competitor_id)
        return _from_row(row) if row is not None else None

    async def save_intelligence(
        self, workspace_id: int, intelligence: CompetitorIntelligence,
    ) -> None:
        payload = json.dumps(asdict(intelligence), ensure_ascii=False)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            cursor = await db.execute(
                "UPDATE competitor_intelligence_snapshots SET analyzed_at=?, payload_json=? "
                "WHERE workspace_id=? AND competitor_id=?",
                (intelligence.analyzed_at, payload, workspace_id, intelligence.competitor_id),
            )
            if cursor.rowcount == 0:
                await db.execute(
                    "INSERT INTO competitor_intelligence_snapshots "
                    "(competitor_id, workspace_id, analyzed_at, payload_json) "
                    "SELECT id, workspace_id, ?, ? FROM competitors "
                    "WHERE id=? AND workspace_id=?",
                    (intelligence.analyzed_at, payload, intelligence.competitor_id, workspace_id),
                )
            await db.commit()

    async def get_intelligence(
        self, workspace_id: int, competitor_id: int,
    ) -> CompetitorIntelligence | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT payload_json FROM competitor_intelligence_snapshots "
                "WHERE workspace_id=? AND competitor_id=?",
                (workspace_id, competitor_id),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return _intelligence_from_json(row["payload_json"])

    async def count_intelligence_analyses_since(self, since_iso: str) -> int:
        """Beta Control Center dashboard only (app/admin_api.py) - global
        (cross-tenant) count of analyses (a re-analysis updates the same
        row in place - see save_intelligence - so this counts "analyzed_at
        touched since X", not a full history of every past run)."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM competitor_intelligence_snapshots "
                "WHERE analyzed_at >= ?",
                (since_iso,),
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def list_intelligence_dates_for_workspace(
        self, workspace_id: int,
    ) -> dict[int, str]:
        """competitor_id -> most recent analyzed_at for competitors that
        already have a saved Competitor Intelligence snapshot. Cheap listing
        lookup (no payload_json parsing) for UI cards - see get_intelligence()
        for the full snapshot.

        competitor_id is currently a single-column PRIMARY KEY on
        competitor_intelligence_snapshots, so save_intelligence() can only
        ever leave one row per competitor - but the query still aggregates
        with MAX(analyzed_at) GROUP BY competitor_id rather than trusting
        that invariant, so the result stays correct (and independent of
        SQLite's row order) even if that constraint is ever relaxed."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT competitor_id, MAX(analyzed_at) FROM "
                "competitor_intelligence_snapshots WHERE workspace_id = ? "
                "GROUP BY competitor_id",
                (workspace_id,),
            )
            rows = await cursor.fetchall()
        return {row[0]: row[1] for row in rows}

    @staticmethod
    async def _row(
        db: aiosqlite.Connection, workspace_id: int, competitor_id: int
    ) -> aiosqlite.Row | None:
        cursor = await db.execute(
            "SELECT * FROM competitors WHERE workspace_id = ? AND id = ?",
            (workspace_id, competitor_id),
        )
        return await cursor.fetchone()

    async def known_domains_for_workspace(self, workspace_id: int) -> set[str]:
        """Canonical domains of already-saved competitors - Discovery uses
        this to skip a candidate that is already a known competitor (e.g.
        Trip.com must not be re-proposed just because its URL string
        differs from what was saved)."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT url FROM competitors WHERE workspace_id = ?", (workspace_id,),
            )
            rows = await cursor.fetchall()
        return {canonical_domain(row[0]) for row in rows}

    async def upsert_candidate(
        self, workspace_id: int, *, name: str, discovered_url: str,
        source_title: str, source_url: str, description: str,
        evidence: tuple[str, ...], confidence: CandidateConfidence,
        classification: CandidateClassification, why_it_matters: str,
    ) -> CompetitorCandidate:
        """Insert a newly found candidate, or - if this canonical domain was
        already seen for this workspace - refresh its evidence/last_seen
        without creating a duplicate row and without resetting a status the
        owner already set (reviewed/added/ignored stays as-is)."""
        domain = canonical_domain(discovered_url)
        now = _now()
        evidence_json = json.dumps(list(evidence), ensure_ascii=False)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute(
                "INSERT INTO competitor_candidates "
                "(workspace_id, name, canonical_domain, discovered_url, source_title, "
                "source_url, discovered_at, last_seen_at, description, evidence_json, "
                "confidence, classification, why_it_matters, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new') "
                "ON CONFLICT(workspace_id, canonical_domain) DO UPDATE SET "
                "name=excluded.name, discovered_url=excluded.discovered_url, "
                "source_title=excluded.source_title, source_url=excluded.source_url, "
                "last_seen_at=excluded.last_seen_at, description=excluded.description, "
                "evidence_json=excluded.evidence_json, confidence=excluded.confidence, "
                "classification=excluded.classification, "
                "why_it_matters=excluded.why_it_matters",
                (
                    workspace_id, name, domain, discovered_url, source_title, source_url,
                    now, now, description, evidence_json,
                    confidence.value, classification.value, why_it_matters,
                ),
            )
            await db.commit()
            cursor = await db.execute(
                "SELECT * FROM competitor_candidates "
                "WHERE workspace_id = ? AND canonical_domain = ?",
                (workspace_id, domain),
            )
            row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("Не удалось сохранить candidate")
        return _candidate_from_row(row)

    async def list_candidates_for_workspace(
        self, workspace_id: int, *, status: str | None = None, limit: int = 20,
    ) -> list[CompetitorCandidate]:
        query = "SELECT * FROM competitor_candidates WHERE workspace_id = ?"
        params: list[object] = [workspace_id]
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(_limit(limit))
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()
        return [_candidate_from_row(row) for row in rows]

    async def get_candidate_for_workspace(
        self, workspace_id: int, candidate_id: int,
    ) -> CompetitorCandidate | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM competitor_candidates WHERE workspace_id = ? AND id = ?",
                (workspace_id, candidate_id),
            )
            row = await cursor.fetchone()
        return _candidate_from_row(row) if row is not None else None

    async def update_candidate_status(
        self, workspace_id: int, candidate_id: int, status: CandidateStatus,
    ) -> CompetitorCandidate | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                "UPDATE competitor_candidates SET status = ? "
                "WHERE workspace_id = ? AND id = ?",
                (status.value, workspace_id, candidate_id),
            )
            await db.commit()
            cursor = await db.execute(
                "SELECT * FROM competitor_candidates WHERE workspace_id = ? AND id = ?",
                (workspace_id, candidate_id),
            )
            row = await cursor.fetchone()
        return _candidate_from_row(row) if row is not None else None


def _validate_address(url: str) -> str:
    address = (url or "").strip()
    if not address:
        raise CompetitorAddressError("ссылка не должна быть пустой")
    if not (address.startswith("http://") or address.startswith("https://")):
        raise CompetitorAddressError(
            "ссылка должна начинаться с http:// или https://"
        )
    if len(address) > _MAX_URL_LENGTH:
        raise CompetitorAddressError("ссылка слишком длинная")
    if any(char.isspace() for char in address):
        raise CompetitorAddressError("ссылка не должна содержать пробелы")
    return address


def _validate_label(label: str) -> str:
    normalized = " ".join((label or "").strip().split())
    if not normalized:
        raise CompetitorLabelError("название не должно быть пустым")
    if len(normalized) > _MAX_LABEL_LENGTH:
        raise CompetitorLabelError("название слишком длинное")
    return normalized


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _limit(value: int) -> int:
    if value <= 0:
        raise ValueError("limit должен быть положительным")
    return value


def _from_row(row: aiosqlite.Row) -> Competitor:
    return Competitor(
        id=row["id"],
        workspace_id=row["workspace_id"],
        url=row["url"],
        label=row["label"],
        created_at=row["created_at"],
    )


def _candidate_from_row(row: aiosqlite.Row) -> CompetitorCandidate:
    return CompetitorCandidate(
        candidate_id=row["id"],
        workspace_id=row["workspace_id"],
        name=row["name"],
        canonical_domain=row["canonical_domain"],
        discovered_url=row["discovered_url"],
        source_title=row["source_title"],
        source_url=row["source_url"],
        discovered_at=row["discovered_at"],
        description=row["description"],
        evidence=tuple(json.loads(row["evidence_json"])),
        confidence=CandidateConfidence(row["confidence"]),
        classification=CandidateClassification(row["classification"]),
        why_it_matters=row["why_it_matters"],
        status=CandidateStatus(row["status"]),
    )


def _intelligence_from_json(raw: str) -> CompetitorIntelligence:
    data = json.loads(raw)
    data["sources"] = tuple(CompetitorSourceEvidence(**item) for item in data["sources"])
    data["opportunities"] = tuple(ContentOpportunity(**item) for item in data["opportunities"])
    for key in (
        "positioning", "products", "destinations_and_categories", "promotions",
        "loyalty_mechanics", "service_and_ux", "strengths",
        "travel_advantage_comparison", "fresh_signals",
    ):
        data[key] = tuple(data[key])
    return CompetitorIntelligence(**data)
