from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite


_SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    task_text TEXT NOT NULL,
    primary_module TEXT NOT NULL,
    secondary_modules TEXT NOT NULL,
    safety_level TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    note TEXT NOT NULL DEFAULT ''
);
"""


@dataclass(frozen=True)
class JournalEntry:
    id: int
    created_at: str
    task_text: str
    primary_module: str
    secondary_modules: str
    safety_level: str
    status: str
    note: str


class Journal:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()

    async def add(
        self,
        task_text: str,
        primary_module: str,
        secondary_modules: tuple[str, ...],
        safety_level: str,
        status: str = "new",
        note: str = "",
    ) -> int:
        created_at = datetime.now(timezone.utc).isoformat()
        secondary_str = ", ".join(secondary_modules)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "INSERT INTO journal "
                "(created_at, task_text, primary_module, secondary_modules, "
                "safety_level, status, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (created_at, task_text, primary_module, secondary_str,
                 safety_level, status, note),
            )
            await db.commit()
            return cursor.lastrowid or 0

    async def last(self) -> Optional[JournalEntry]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT id, created_at, task_text, primary_module, "
                "secondary_modules, safety_level, status, note "
                "FROM journal ORDER BY id DESC LIMIT 1"
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return JournalEntry(
                id=row["id"],
                created_at=row["created_at"],
                task_text=row["task_text"],
                primary_module=row["primary_module"],
                secondary_modules=row["secondary_modules"],
                safety_level=row["safety_level"],
                status=row["status"],
                note=row["note"],
            )
