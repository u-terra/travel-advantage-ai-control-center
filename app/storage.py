from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite


_TABLE = "journal"
_MIGRATION_TABLE = "journal_tenant_migration"
_INDEX = "idx_journal_workspace_id"
_COLUMNS = (
    "id",
    "workspace_id",
    "created_at",
    "task_text",
    "primary_module",
    "secondary_modules",
    "safety_level",
    "status",
    "note",
)
_LEGACY_COLUMNS = tuple(column for column in _COLUMNS if column != "workspace_id")


def _create_table_sql(table: str) -> str:
    return f"""
CREATE TABLE {table} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    task_text TEXT NOT NULL,
    primary_module TEXT NOT NULL,
    secondary_modules TEXT NOT NULL,
    safety_level TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    note TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (workspace_id) REFERENCES partner_workspaces(id)
)
"""


_CREATE_INDEX_SQL = f"""
CREATE INDEX IF NOT EXISTS {_INDEX}
    ON {_TABLE}(workspace_id, id)
"""


@dataclass(frozen=True)
class JournalEntry:
    id: int
    workspace_id: int
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

    async def init(self, legacy_owner_workspace_id: int | None) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                columns = await _table_columns(db, _TABLE)
                if not columns:
                    await db.execute(_create_table_sql(_TABLE))
                    await db.execute(_CREATE_INDEX_SQL)
                elif tuple(columns) == _LEGACY_COLUMNS:
                    await self._migrate_legacy(db, legacy_owner_workspace_id)
                else:
                    await _validate_tenant_schema(db, columns)
                    await db.execute(_CREATE_INDEX_SQL)

                await _validate_index(db)
                violations = await (
                    await db.execute("PRAGMA foreign_key_check")
                ).fetchall()
                if violations:
                    raise RuntimeError("Нарушена ссылочная целостность SQLite")
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def _migrate_legacy(
        self, db: aiosqlite.Connection, legacy_owner_workspace_id: int | None
    ) -> None:
        if legacy_owner_workspace_id is None:
            raise RuntimeError(
                "Для миграции legacy Journal требуется workspace владельца"
            )
        workspace = await (
            await db.execute(
                "SELECT id FROM partner_workspaces WHERE id = ?",
                (legacy_owner_workspace_id,),
            )
        ).fetchone()
        if workspace is None:
            raise ValueError("Workspace для миграции Journal не существует")

        await db.execute(_create_table_sql(_MIGRATION_TABLE))
        source_count = (
            await (await db.execute("SELECT COUNT(*) FROM journal")).fetchone()
        )[0]
        await db.execute(
            f"INSERT INTO {_MIGRATION_TABLE} "
            "(id, workspace_id, created_at, task_text, primary_module, "
            "secondary_modules, safety_level, status, note) "
            "SELECT id, ?, created_at, task_text, primary_module, "
            "secondary_modules, safety_level, status, note FROM journal",
            (legacy_owner_workspace_id,),
        )
        target_count = (
            await (
                await db.execute(f"SELECT COUNT(*) FROM {_MIGRATION_TABLE}")
            ).fetchone()
        )[0]
        if source_count != target_count:
            raise RuntimeError("Не все записи Journal перенесены")

        await db.execute("DROP TABLE journal")
        await db.execute(f"ALTER TABLE {_MIGRATION_TABLE} RENAME TO journal")
        await db.execute(_CREATE_INDEX_SQL)

    async def add(
        self,
        workspace_id: int,
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
            await db.execute("PRAGMA foreign_keys = ON")
            cursor = await db.execute(
                "INSERT INTO journal "
                "(workspace_id, created_at, task_text, primary_module, "
                "secondary_modules, safety_level, status, note) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    workspace_id,
                    created_at,
                    task_text,
                    primary_module,
                    secondary_str,
                    safety_level,
                    status,
                    note,
                ),
            )
            await db.commit()
            return cursor.lastrowid or 0

    async def last(self, workspace_id: int) -> Optional[JournalEntry]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT id, workspace_id, created_at, task_text, primary_module, "
                "secondary_modules, safety_level, status, note "
                "FROM journal WHERE workspace_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (workspace_id,),
            )
            row = await cursor.fetchone()
        return _entry_from_row(row) if row is not None else None


async def _table_columns(
    db: aiosqlite.Connection, table: str
) -> dict[str, aiosqlite.Row]:
    rows = await (await db.execute(f"PRAGMA table_info({table})")).fetchall()
    return {row["name"]: row for row in rows}


async def _validate_tenant_schema(
    db: aiosqlite.Connection, columns: dict[str, aiosqlite.Row]
) -> None:
    if tuple(columns) != _COLUMNS:
        raise RuntimeError("Неожиданная схема таблицы journal")
    if columns["id"]["pk"] != 1:
        raise RuntimeError("journal.id не является primary key")
    expected_types = {
        "id": "INTEGER",
        "workspace_id": "INTEGER",
        "created_at": "TEXT",
        "task_text": "TEXT",
        "primary_module": "TEXT",
        "secondary_modules": "TEXT",
        "safety_level": "TEXT",
        "status": "TEXT",
        "note": "TEXT",
    }
    for name, expected_type in expected_types.items():
        if columns[name]["type"].upper() != expected_type:
            raise RuntimeError(f"Неожиданный тип journal.{name}")
    for name in _COLUMNS[1:]:
        if columns[name]["notnull"] != 1:
            raise RuntimeError(f"journal.{name} должен быть NOT NULL")
    if columns["status"]["dflt_value"] != "'new'":
        raise RuntimeError("Неожиданный default journal.status")
    if columns["note"]["dflt_value"] != "''":
        raise RuntimeError("Неожиданный default journal.note")
    foreign_keys = await (
        await db.execute("PRAGMA foreign_key_list(journal)")
    ).fetchall()
    expected_fk = any(
        row["table"] == "partner_workspaces"
        and row["from"] == "workspace_id"
        and row["to"] == "id"
        for row in foreign_keys
    )
    if not expected_fk:
        raise RuntimeError("У journal отсутствует FK на partner_workspaces")
    null_count = (
        await (
            await db.execute(
                "SELECT COUNT(*) FROM journal WHERE workspace_id IS NULL"
            )
        ).fetchone()
    )[0]
    if null_count:
        raise RuntimeError("В journal обнаружен NULL workspace_id")


async def _validate_index(db: aiosqlite.Connection) -> None:
    indexes = await (await db.execute("PRAGMA index_list(journal)")).fetchall()
    if not any(row["name"] == _INDEX for row in indexes):
        raise RuntimeError("У journal отсутствует workspace index")
    columns = await (
        await db.execute(f"PRAGMA index_info({_INDEX})")
    ).fetchall()
    if [row["name"] for row in columns] != ["workspace_id", "id"]:
        raise RuntimeError("Некорректный workspace index таблицы journal")


def _entry_from_row(row: aiosqlite.Row) -> JournalEntry:
    return JournalEntry(
        id=row["id"],
        workspace_id=row["workspace_id"],
        created_at=row["created_at"],
        task_text=row["task_text"],
        primary_module=row["primary_module"],
        secondary_modules=row["secondary_modules"],
        safety_level=row["safety_level"],
        status=row["status"],
        note=row["note"],
    )
