from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from app.domain.content import Artifact, ArtifactVersion, Source


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    source_type TEXT NOT NULL,
    original_url TEXT,
    original_text TEXT,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES partner_workspaces(id),
    CHECK (length(trim(title)) > 0),
    CHECK (
        (original_url IS NOT NULL AND length(trim(original_url)) > 0)
        OR (original_text IS NOT NULL AND length(trim(original_text)) > 0)
    )
);

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    source_id INTEGER,
    artifact_type TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    current_version_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES partner_workspaces(id),
    FOREIGN KEY (source_id) REFERENCES sources(id),
    FOREIGN KEY (current_version_id) REFERENCES artifact_versions(id),
    CHECK (length(trim(title)) > 0)
);

CREATE TABLE IF NOT EXISTS artifact_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id INTEGER NOT NULL,
    version_number INTEGER NOT NULL,
    content TEXT NOT NULL,
    generation_note TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (artifact_id) REFERENCES artifacts(id),
    CHECK (length(trim(content)) > 0),
    UNIQUE (artifact_id, version_number)
);

CREATE INDEX IF NOT EXISTS idx_sources_workspace
    ON sources(workspace_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_artifacts_workspace_status
    ON artifacts(workspace_id, status, id DESC);
"""


class ArtifactRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.executescript(_SCHEMA)
            await db.commit()

    async def create_source(
        self,
        workspace_id: int,
        *,
        source_type: str,
        title: str,
        original_url: str | None = None,
        original_text: str | None = None,
        status: str = "new",
    ) -> Source:
        _require_value(title, "title")
        if not _has_value(original_url) and not _has_value(original_text):
            raise ValueError("Source должен содержать URL или текст")
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            cursor = await db.execute(
                "INSERT INTO sources "
                "(workspace_id, source_type, original_url, original_text, title, "
                "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    workspace_id,
                    source_type,
                    original_url,
                    original_text,
                    title,
                    status,
                    now,
                    now,
                ),
            )
            await db.commit()
            row = await self._source_row(db, workspace_id, cursor.lastrowid or 0)
        if row is None:
            raise RuntimeError("Не удалось создать источник")
        return _source_from_row(row)

    async def get_source(self, workspace_id: int, source_id: int) -> Source | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await self._source_row(db, workspace_id, source_id)
        return _source_from_row(row) if row is not None else None

    async def list_sources(
        self, workspace_id: int, limit: int = 50
    ) -> list[Source]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM sources WHERE workspace_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (workspace_id, _limit(limit)),
            )
            rows = await cursor.fetchall()
        return [_source_from_row(row) for row in rows]

    async def create_artifact(
        self,
        workspace_id: int,
        *,
        artifact_type: str,
        title: str,
        source_id: int | None = None,
        status: str = "draft",
    ) -> Artifact:
        _require_value(title, "title")
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                if source_id is not None:
                    source_row = await self._source_row(db, workspace_id, source_id)
                    if source_row is None:
                        raise ValueError("Source не принадлежит workspace")
                cursor = await db.execute(
                    "INSERT INTO artifacts "
                    "(workspace_id, source_id, artifact_type, title, status, "
                    "current_version_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
                    (workspace_id, source_id, artifact_type, title, status, now, now),
                )
                row = await self._artifact_row(
                    db, workspace_id, cursor.lastrowid or 0
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        if row is None:
            raise RuntimeError("Не удалось создать материал")
        return _artifact_from_row(row)

    async def create_artifact_with_initial_version(
        self,
        workspace_id: int,
        *,
        artifact_type: str,
        title: str,
        content: str,
        source_id: int | None = None,
        status: str = "draft",
        generation_note: str | None = None,
    ) -> tuple[Artifact, ArtifactVersion]:
        """Atomically create an artifact, version 1, and current-version link."""
        _require_value(title, "title")
        _require_value(content, "content")
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                if source_id is not None and await self._source_row(
                    db, workspace_id, source_id
                ) is None:
                    raise ValueError("Source не принадлежит workspace")
                artifact_cursor = await db.execute(
                    "INSERT INTO artifacts "
                    "(workspace_id, source_id, artifact_type, title, status, "
                    "current_version_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
                    (workspace_id, source_id, artifact_type, title, status, now, now),
                )
                artifact_id = artifact_cursor.lastrowid or 0
                version_cursor = await db.execute(
                    "INSERT INTO artifact_versions "
                    "(artifact_id, version_number, content, generation_note, created_at) "
                    "VALUES (?, 1, ?, ?, ?)",
                    (artifact_id, content, generation_note, now),
                )
                version_id = version_cursor.lastrowid or 0
                await db.execute(
                    "UPDATE artifacts SET current_version_id = ?, updated_at = ? "
                    "WHERE workspace_id = ? AND id = ?",
                    (version_id, now, workspace_id, artifact_id),
                )
                artifact_row = await self._artifact_row(db, workspace_id, artifact_id)
                version_row = await self._version_row(db, workspace_id, artifact_id, 1)
                if artifact_row is None or version_row is None:
                    raise RuntimeError("Не удалось создать материал и первую версию")
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return _artifact_from_row(artifact_row), _version_from_row(version_row)

    async def get_artifact(
        self, workspace_id: int, artifact_id: int
    ) -> Artifact | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await self._artifact_row(db, workspace_id, artifact_id)
        return _artifact_from_row(row) if row is not None else None

    async def list_artifacts(
        self, workspace_id: int, limit: int = 50, status: str | None = None
    ) -> list[Artifact]:
        params: list[object] = [workspace_id]
        status_sql = ""
        if status is not None:
            status_sql = " AND status = ?"
            params.append(status)
        params.append(_limit(limit))
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM artifacts WHERE workspace_id = ?"
                + status_sql
                + " ORDER BY id DESC LIMIT ?",
                params,
            )
            rows = await cursor.fetchall()
        return [_artifact_from_row(row) for row in rows]

    async def update_artifact_status(
        self, workspace_id: int, artifact_id: int, status: str
    ) -> Artifact | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute(
                "UPDATE artifacts SET status = ?, updated_at = ? "
                "WHERE workspace_id = ? AND id = ?",
                (status, _now(), workspace_id, artifact_id),
            )
            await db.commit()
            row = await self._artifact_row(db, workspace_id, artifact_id)
        return _artifact_from_row(row) if row is not None else None

    async def add_artifact_version(
        self,
        workspace_id: int,
        artifact_id: int,
        content: str,
        generation_note: str | None = None,
    ) -> ArtifactVersion | None:
        _require_value(content, "content")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                artifact_row = await self._artifact_row(db, workspace_id, artifact_id)
                if artifact_row is None:
                    await db.rollback()
                    return None
                cursor = await db.execute(
                    "SELECT COALESCE(MAX(version_number), 0) + 1 "
                    "FROM artifact_versions WHERE artifact_id = ?",
                    (artifact_id,),
                )
                version_number = (await cursor.fetchone())[0]
                now = _now()
                cursor = await db.execute(
                    "INSERT INTO artifact_versions "
                    "(artifact_id, version_number, content, generation_note, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (artifact_id, version_number, content, generation_note, now),
                )
                version_id = cursor.lastrowid or 0
                await db.execute(
                    "UPDATE artifacts SET current_version_id = ?, updated_at = ? "
                    "WHERE workspace_id = ? AND id = ?",
                    (version_id, now, workspace_id, artifact_id),
                )
                version_row = await self._version_row(
                    db, workspace_id, artifact_id, version_number
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        if version_row is None:
            raise RuntimeError("Не удалось создать версию материала")
        return _version_from_row(version_row)

    async def add_artifact_version_if_current(
        self,
        workspace_id: int,
        artifact_id: int,
        expected_current_version_id: int,
        content: str,
        generation_note: str | None = None,
    ) -> ArtifactVersion | None:
        """Atomically append a version only while the reviewed version is current."""
        _require_value(content, "content")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                artifact_row = await self._artifact_row(db, workspace_id, artifact_id)
                if (
                    artifact_row is None
                    or artifact_row["current_version_id"] != expected_current_version_id
                ):
                    await db.rollback()
                    return None
                cursor = await db.execute(
                    "SELECT COALESCE(MAX(version_number), 0) + 1 "
                    "FROM artifact_versions WHERE artifact_id = ?",
                    (artifact_id,),
                )
                version_number = (await cursor.fetchone())[0]
                now = _now()
                cursor = await db.execute(
                    "INSERT INTO artifact_versions "
                    "(artifact_id, version_number, content, generation_note, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (artifact_id, version_number, content, generation_note, now),
                )
                version_id = cursor.lastrowid or 0
                update = await db.execute(
                    "UPDATE artifacts SET current_version_id = ?, updated_at = ? "
                    "WHERE workspace_id = ? AND id = ? AND current_version_id = ?",
                    (
                        version_id, now, workspace_id, artifact_id,
                        expected_current_version_id,
                    ),
                )
                if update.rowcount != 1:
                    await db.rollback()
                    return None
                version_row = await self._version_row(
                    db, workspace_id, artifact_id, version_number
                )
                if version_row is None:
                    raise RuntimeError("Не удалось создать версию материала")
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return _version_from_row(version_row)

    async def get_artifact_version(
        self, workspace_id: int, artifact_id: int, version_number: int
    ) -> ArtifactVersion | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await self._version_row(
                db, workspace_id, artifact_id, version_number
            )
        return _version_from_row(row) if row is not None else None

    async def list_artifact_versions(
        self, workspace_id: int, artifact_id: int
    ) -> list[ArtifactVersion]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT v.* FROM artifact_versions AS v "
                "JOIN artifacts AS a ON a.id = v.artifact_id "
                "WHERE a.workspace_id = ? AND a.id = ? "
                "ORDER BY v.version_number ASC",
                (workspace_id, artifact_id),
            )
            rows = await cursor.fetchall()
        return [_version_from_row(row) for row in rows]

    async def get_current_artifact_version(
        self, workspace_id: int, artifact_id: int
    ) -> ArtifactVersion | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT v.* FROM artifact_versions AS v "
                "JOIN artifacts AS a ON a.current_version_id = v.id "
                "WHERE a.workspace_id = ? AND a.id = ? "
                "AND v.artifact_id = a.id",
                (workspace_id, artifact_id),
            )
            row = await cursor.fetchone()
        return _version_from_row(row) if row is not None else None

    @staticmethod
    async def _source_row(
        db: aiosqlite.Connection, workspace_id: int, source_id: int
    ) -> aiosqlite.Row | None:
        cursor = await db.execute(
            "SELECT * FROM sources WHERE workspace_id = ? AND id = ?",
            (workspace_id, source_id),
        )
        return await cursor.fetchone()

    @staticmethod
    async def _artifact_row(
        db: aiosqlite.Connection, workspace_id: int, artifact_id: int
    ) -> aiosqlite.Row | None:
        cursor = await db.execute(
            "SELECT * FROM artifacts WHERE workspace_id = ? AND id = ?",
            (workspace_id, artifact_id),
        )
        return await cursor.fetchone()

    @staticmethod
    async def _version_row(
        db: aiosqlite.Connection,
        workspace_id: int,
        artifact_id: int,
        version_number: int,
    ) -> aiosqlite.Row | None:
        cursor = await db.execute(
            "SELECT v.* FROM artifact_versions AS v "
            "JOIN artifacts AS a ON a.id = v.artifact_id "
            "WHERE a.workspace_id = ? AND a.id = ? AND v.version_number = ?",
            (workspace_id, artifact_id, version_number),
        )
        return await cursor.fetchone()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _has_value(value: str | None) -> bool:
    return value is not None and bool(value.strip())


def _limit(value: int) -> int:
    if value <= 0:
        raise ValueError("limit должен быть положительным")
    return value


def _require_value(value: str, field: str) -> None:
    if not value.strip():
        raise ValueError(f"{field} не должен быть пустым")


def _source_from_row(row: aiosqlite.Row) -> Source:
    return Source(
        id=row["id"],
        workspace_id=row["workspace_id"],
        source_type=row["source_type"],
        original_url=row["original_url"],
        original_text=row["original_text"],
        title=row["title"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _artifact_from_row(row: aiosqlite.Row) -> Artifact:
    return Artifact(
        id=row["id"],
        workspace_id=row["workspace_id"],
        source_id=row["source_id"],
        artifact_type=row["artifact_type"],
        title=row["title"],
        status=row["status"],
        current_version_id=row["current_version_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _version_from_row(row: aiosqlite.Row) -> ArtifactVersion:
    return ArtifactVersion(
        id=row["id"],
        artifact_id=row["artifact_id"],
        version_number=row["version_number"],
        content=row["content"],
        generation_note=row["generation_note"],
        created_at=row["created_at"],
    )
