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

    @staticmethod
    async def _row(
        db: aiosqlite.Connection, workspace_id: int, competitor_id: int
    ) -> aiosqlite.Row | None:
        cursor = await db.execute(
            "SELECT * FROM competitors WHERE workspace_id = ? AND id = ?",
            (workspace_id, competitor_id),
        )
        return await cursor.fetchone()


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
