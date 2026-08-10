from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import aiosqlite

from app.domain.sources import WorkspaceSource
from app.services.source_catalog_export import export_radar_projection
from app.services.source_registry import SourceRegistry, load_registry
from app.services.source_registry_store import UnknownSourceError, resolve_address


VISIBILITIES = frozenset({"platform", "private"})
SOURCE_STATUSES = frozenset({"active", "inactive"})
USAGE_ROLES = frozenset({"monitoring", "competitor"})
_MIGRATION_KEY = "source_catalog_v1"


class SourceCatalogMigrationError(RuntimeError):
    """Legacy registry could not be migrated without guessing its owner."""


@dataclass(frozen=True)
class AddSourceResult:
    source: WorkspaceSource
    outcome: Literal["created", "already_enabled", "already_disabled"]


_SCHEMA_STATEMENTS = (
"""CREATE TABLE IF NOT EXISTS source_catalog (
    id TEXT PRIMARY KEY,
    identity_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    platform TEXT NOT NULL,
    url TEXT,
    username TEXT,
    source_type TEXT NOT NULL,
    purpose TEXT NOT NULL,
    priority INTEGER NOT NULL,
    notes TEXT NOT NULL,
    collector_json TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK (visibility IN ('platform', 'private')),
    owner_workspace_id INTEGER,
    status TEXT NOT NULL CHECK (status IN ('active', 'inactive')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (owner_workspace_id) REFERENCES partner_workspaces(id),
    CHECK (visibility != 'private' OR owner_workspace_id IS NOT NULL)
)""",
"""CREATE TABLE IF NOT EXISTS workspace_source_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    usage_role TEXT NOT NULL DEFAULT 'monitoring'
        CHECK (usage_role IN ('monitoring', 'competitor')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES partner_workspaces(id),
    FOREIGN KEY (source_id) REFERENCES source_catalog(id),
    UNIQUE (workspace_id, source_id)
)""",
"""CREATE INDEX IF NOT EXISTS idx_workspace_source_subscriptions_workspace
    ON workspace_source_subscriptions(workspace_id, source_id)""",
"""CREATE TABLE IF NOT EXISTS source_catalog_migrations (
    name TEXT PRIMARY KEY,
    completed_at TEXT NOT NULL
)""",
)


class SourceCatalogRepository:
    def __init__(self, db_path: Path, projection_path: Path) -> None:
        self.db_path = Path(db_path)
        self.projection_path = Path(projection_path)

    async def init(
        self,
        legacy_owner_workspace_id: int | None,
        *,
        legacy_path: Path,
    ) -> None:
        """Create schema and atomically import legacy JSON exactly once."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                for statement in _SCHEMA_STATEMENTS:
                    await db.execute(statement)
                marker = await (await db.execute(
                    "SELECT 1 FROM source_catalog_migrations WHERE name = ?",
                    (_MIGRATION_KEY,),
                )).fetchone()
                if marker is None:
                    if legacy_owner_workspace_id is None:
                        raise SourceCatalogMigrationError(
                            "Для миграции legacy Source Registry не определён owner workspace"
                        )
                    owner = await (await db.execute(
                        "SELECT 1 FROM partner_workspaces WHERE id = ?",
                        (legacy_owner_workspace_id,),
                    )).fetchone()
                    if owner is None:
                        raise SourceCatalogMigrationError(
                            "Owner workspace для миграции Source Registry не существует"
                        )
                    registry = load_registry(legacy_path, use_cache=False)
                    await self._import_legacy(db, registry, legacy_owner_workspace_id)
                    await db.execute(
                        "INSERT INTO source_catalog_migrations(name, completed_at) VALUES (?, ?)",
                        (_MIGRATION_KEY, _now()),
                    )
                violations = await (await db.execute("PRAGMA foreign_key_check")).fetchall()
                if violations:
                    raise SourceCatalogMigrationError(
                        "Source Catalog нарушает внешние ключи"
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        await self.export_projection()

    async def _import_legacy(
        self, db: aiosqlite.Connection, registry: SourceRegistry, workspace_id: int
    ) -> None:
        now = _now()
        for source in registry:
            created_at = source.created_at or now
            updated_at = source.updated_at or created_at
            await db.execute(
                "INSERT INTO source_catalog "
                "(id, identity_key, name, platform, url, username, source_type, "
                "purpose, priority, notes, collector_json, visibility, "
                "owner_workspace_id, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'platform', NULL, "
                "'active', ?, ?)",
                (
                    source.id, source.identity_key, source.name, source.platform,
                    source.url, source.username, source.source_type, source.purpose,
                    source.priority, source.notes,
                    json.dumps(dict(source.collector), ensure_ascii=False, sort_keys=True),
                    created_at, updated_at,
                ),
            )
            await db.execute(
                "INSERT INTO workspace_source_subscriptions "
                "(workspace_id, source_id, enabled, usage_role, created_at, updated_at) "
                "VALUES (?, ?, ?, 'monitoring', ?, ?)",
                (workspace_id, source.id, int(source.enabled), now, now),
            )

    async def list_for_workspace(self, workspace_id: int) -> tuple[WorkspaceSource, ...]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                _WORKSPACE_SELECT
                + " WHERE c.status = 'active' AND (c.visibility = 'platform' "
                  "OR s.workspace_id IS NOT NULL) ORDER BY enabled DESC, c.priority, c.id",
                (workspace_id,),
            )).fetchall()
        return tuple(_workspace_source(row) for row in rows)

    async def get_for_workspace(
        self, workspace_id: int, source_id: str
    ) -> WorkspaceSource | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                _WORKSPACE_SELECT
                + " WHERE c.id = ? AND c.status = 'active' AND "
                  "(c.visibility = 'platform' OR s.workspace_id IS NOT NULL)",
                (workspace_id, source_id),
            )).fetchone()
        return _workspace_source(row) if row is not None else None

    async def add_source(
        self, workspace_id: int, address: str, usage_role: str = "monitoring"
    ) -> AddSourceResult:
        _validate_usage_role(usage_role)
        resolved = resolve_address(address)
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (await db.execute(
                    "SELECT id, status FROM source_catalog WHERE identity_key = ?",
                    (resolved.identity_key,),
                )).fetchone()
                if row is None:
                    source_id = _private_source_id(resolved.identity_key)
                    await db.execute(
                        "INSERT INTO source_catalog "
                        "(id, identity_key, name, platform, url, username, source_type, "
                        "purpose, priority, notes, collector_json, visibility, "
                        "owner_workspace_id, status, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, 'monitored_source', 'mixed', 50, "
                        "'', '{}', 'private', ?, 'active', ?, ?)",
                        (
                            source_id, resolved.identity_key, resolved.default_name,
                            resolved.platform, resolved.url or None,
                            resolved.username or None, workspace_id, now, now,
                        ),
                    )
                else:
                    if row["status"] != "active":
                        raise UnknownSourceError("Источник отключён на уровне платформы")
                    source_id = row["id"]
                subscription = await (await db.execute(
                    "SELECT enabled FROM workspace_source_subscriptions "
                    "WHERE workspace_id = ? AND source_id = ?",
                    (workspace_id, source_id),
                )).fetchone()
                if subscription is None:
                    outcome: Literal[
                        "created", "already_enabled", "already_disabled"
                    ] = "created"
                    await db.execute(
                        "INSERT INTO workspace_source_subscriptions "
                        "(workspace_id, source_id, enabled, usage_role, created_at, updated_at) "
                        "VALUES (?, ?, 1, ?, ?, ?)",
                        (workspace_id, source_id, usage_role, now, now),
                    )
                else:
                    outcome = (
                        "already_enabled" if subscription["enabled"]
                        else "already_disabled"
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        await self.export_projection()
        result = await self.get_for_workspace(workspace_id, source_id)
        if result is None:
            raise RuntimeError("Не удалось добавить источник workspace")
        return AddSourceResult(source=result, outcome=outcome)

    async def set_enabled(
        self, workspace_id: int, source_id: str, enabled: bool
    ) -> WorkspaceSource:
        return await self._mutate_subscription(workspace_id, source_id, enabled=enabled)

    async def toggle(self, workspace_id: int, source_id: str) -> WorkspaceSource:
        return await self._mutate_subscription(workspace_id, source_id, toggle=True)

    async def set_usage_role(
        self, workspace_id: int, source_id: str, usage_role: str
    ) -> WorkspaceSource:
        _validate_usage_role(usage_role)
        return await self._mutate_subscription(
            workspace_id, source_id, usage_role=usage_role
        )

    async def _mutate_subscription(
        self,
        workspace_id: int,
        source_id: str,
        *,
        enabled: bool | None = None,
        toggle: bool = False,
        usage_role: str | None = None,
    ) -> WorkspaceSource:
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                source = await (await db.execute(
                    "SELECT visibility, status FROM source_catalog WHERE id = ?",
                    (source_id,),
                )).fetchone()
                subscription = await (await db.execute(
                    "SELECT enabled, usage_role FROM workspace_source_subscriptions "
                    "WHERE workspace_id = ? AND source_id = ?",
                    (workspace_id, source_id),
                )).fetchone()
                if source is None or source["status"] != "active" or (
                    source["visibility"] == "private" and subscription is None
                ):
                    raise UnknownSourceError(f"источник '{source_id}' недоступен")
                if subscription is None:
                    current_enabled, current_role = 0, "monitoring"
                    await db.execute(
                        "INSERT INTO workspace_source_subscriptions "
                        "(workspace_id, source_id, enabled, usage_role, created_at, updated_at) "
                        "VALUES (?, ?, 0, 'monitoring', ?, ?)",
                        (workspace_id, source_id, now, now),
                    )
                else:
                    current_enabled = subscription["enabled"]
                    current_role = subscription["usage_role"]
                next_enabled = 1 - current_enabled if toggle else (
                    int(enabled) if enabled is not None else current_enabled
                )
                next_role = usage_role if usage_role is not None else current_role
                await db.execute(
                    "UPDATE workspace_source_subscriptions SET enabled = ?, "
                    "usage_role = ?, updated_at = ? WHERE workspace_id = ? AND source_id = ?",
                    (next_enabled, next_role, now, workspace_id, source_id),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        await self.export_projection()
        result = await self.get_for_workspace(workspace_id, source_id)
        if result is None:
            raise UnknownSourceError(f"источник '{source_id}' недоступен")
        return result

    async def physical_collection_targets(self) -> tuple[WorkspaceSource, ...]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                _PHYSICAL_SELECT + " WHERE c.status = 'active' AND EXISTS ("
                "SELECT 1 FROM workspace_source_subscriptions s "
                "WHERE s.source_id = c.id AND s.enabled = 1) ORDER BY c.priority, c.id"
            )).fetchall()
        return tuple(_workspace_source(row) for row in rows)

    async def export_projection(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                rows = await (await db.execute(
                    "SELECT c.*, CASE WHEN c.status = 'active' AND EXISTS ("
                    "SELECT 1 FROM workspace_source_subscriptions s "
                    "WHERE s.source_id = c.id AND s.enabled = 1) THEN 1 ELSE 0 END "
                    "AS radar_enabled FROM source_catalog c ORDER BY c.priority, c.id"
                )).fetchall()
                export_radar_projection(self.projection_path, rows)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise


_WORKSPACE_SELECT = (
    "SELECT c.*, COALESCE(s.enabled, 0) AS enabled, "
    "COALESCE(s.usage_role, 'monitoring') AS usage_role "
    "FROM source_catalog c LEFT JOIN workspace_source_subscriptions s "
    "ON s.source_id = c.id AND s.workspace_id = ?"
)
_PHYSICAL_SELECT = (
    "SELECT c.*, 1 AS enabled, 'monitoring' AS usage_role FROM source_catalog c"
)


def _workspace_source(row: aiosqlite.Row) -> WorkspaceSource:
    return WorkspaceSource(
        id=row["id"], name=row["name"], platform=row["platform"],
        source_type=row["source_type"], purpose=row["purpose"],
        enabled=bool(row["enabled"]), usage_role=row["usage_role"],
        url=row["url"], username=row["username"], priority=row["priority"],
        notes=row["notes"], collector=json.loads(row["collector_json"]),
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


def _private_source_id(identity_key: str) -> str:
    return "private_" + hashlib.sha256(identity_key.encode("utf-8")).hexdigest()[:24]


def _validate_usage_role(role: str) -> None:
    if role not in USAGE_ROLES:
        raise ValueError("Недопустимая роль использования источника")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
