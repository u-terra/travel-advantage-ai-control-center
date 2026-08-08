from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from app.domain.content import SourceAnalysis
from app.services.llm.models import SourceAnalysisPayload


_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL UNIQUE,
    workspace_id INTEGER NOT NULL,
    summary TEXT NOT NULL,
    key_facts TEXT NOT NULL,
    disputed_claims TEXT NOT NULL,
    audience_value TEXT NOT NULL,
    target_audiences TEXT NOT NULL,
    content_angles TEXT NOT NULL,
    recommended_formats TEXT NOT NULL,
    warnings TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES sources(id),
    FOREIGN KEY (workspace_id) REFERENCES partner_workspaces(id)
);
CREATE INDEX IF NOT EXISTS idx_source_analyses_workspace_source
ON source_analyses(workspace_id, source_id);
"""


class SourceAnalysisRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.executescript(_SCHEMA)
            await db.commit()

    async def save_successful_analysis(
        self, workspace_id: int, source_id: int, payload: SourceAnalysisPayload
    ) -> SourceAnalysis:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                source = await (await db.execute(
                    "SELECT id FROM sources WHERE id = ? AND workspace_id = ?",
                    (source_id, workspace_id),
                )).fetchone()
                if source is None:
                    raise ValueError("Source не принадлежит workspace")
                existing = await (await db.execute(
                    "SELECT id FROM source_analyses WHERE source_id = ?", (source_id,)
                )).fetchone()
                if existing is not None:
                    raise ValueError("Анализ для Source уже существует")
                now = datetime.now(timezone.utc).isoformat()
                values = [json.dumps(getattr(payload, field), ensure_ascii=False) for field in (
                    "key_facts", "disputed_claims", "target_audiences",
                    "content_angles", "recommended_formats", "warnings",
                )]
                cursor = await db.execute(
                    "INSERT INTO source_analyses (source_id, workspace_id, summary, key_facts, "
                    "disputed_claims, audience_value, target_audiences, content_angles, "
                    "recommended_formats, warnings, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (source_id, workspace_id, payload.summary, values[0], values[1],
                     payload.audience_value, values[2], values[3], values[4], values[5], now),
                )
                await db.execute(
                    "UPDATE sources SET status = 'analyzed', updated_at = ? WHERE id = ? AND workspace_id = ?",
                    (now, source_id, workspace_id),
                )
                row = await (await db.execute(
                    "SELECT * FROM source_analyses WHERE id = ? AND workspace_id = ?",
                    (cursor.lastrowid, workspace_id),
                )).fetchone()
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        if row is None:
            raise RuntimeError("Не удалось сохранить анализ")
        return _from_row(row)

    async def get_by_source_id(self, workspace_id: int, source_id: int) -> SourceAnalysis | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                "SELECT * FROM source_analyses WHERE workspace_id = ? AND source_id = ?",
                (workspace_id, source_id),
            )).fetchone()
        return _from_row(row) if row else None


def _from_row(row: aiosqlite.Row) -> SourceAnalysis:
    def string_tuple(field: str) -> tuple[str, ...]:
        value = json.loads(row[field])
        if type(value) is not list or any(type(item) is not str for item in value):
            raise ValueError(f"Повреждено JSON-поле SourceAnalysis: {field}")
        return tuple(value)

    return SourceAnalysis(
        id=row["id"], source_id=row["source_id"], workspace_id=row["workspace_id"],
        summary=row["summary"], key_facts=string_tuple("key_facts"),
        disputed_claims=string_tuple("disputed_claims"),
        audience_value=row["audience_value"],
        target_audiences=string_tuple("target_audiences"),
        content_angles=string_tuple("content_angles"),
        recommended_formats=string_tuple("recommended_formats"),
        warnings=string_tuple("warnings"), created_at=row["created_at"],
    )
