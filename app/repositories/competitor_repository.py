"""Workspace-scoped хранилище ссылок на конкурентов.

Только хранение и чтение того, что владелец workspace сам счёл конкурентом.
Никакого сбора, обхода сайтов или анализа здесь нет — это база для будущего
конкурентного анализа, не сам анализ.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from app.domain.competitors import Competitor

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
"""

_MAX_URL_LENGTH = 500


class CompetitorAddressError(ValueError):
    """Ссылка на конкурента пуста, слишком длинная или без схемы http(s)."""


class CompetitorRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.executescript(_SCHEMA)
            await db.commit()

    async def add_competitor(self, workspace_id: int, url: str) -> Competitor:
        address = _validate_address(url)
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            cursor = await db.execute(
                "INSERT INTO competitors (workspace_id, url, label, created_at) "
                "VALUES (?, ?, ?, ?)",
                (workspace_id, address, address, now),
            )
            await db.commit()
            row = await self._row(db, workspace_id, cursor.lastrowid or 0)
        if row is None:
            raise RuntimeError("Не удалось сохранить конкурента")
        return _from_row(row)

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
