from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from app.domain.partners import PartnerProfile, PartnerWorkspace


_SCHEMA = """
CREATE TABLE IF NOT EXISTS partner_workspaces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS partner_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL UNIQUE,
    telegram_user_id INTEGER NOT NULL UNIQUE,
    partner_name TEXT NOT NULL,
    project_name TEXT NOT NULL,
    business_description TEXT NOT NULL,
    communication_style TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES partner_workspaces(id)
);
"""


class PartnerRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.executescript(_SCHEMA)
            await db.commit()

    async def ensure_owner_workspace(
        self, telegram_user_id: int
    ) -> tuple[PartnerWorkspace, PartnerProfile]:
        """Idempotently bind the owner to the first workspace."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                profile_row = await self._profile_row_by_telegram_id(
                    db, telegram_user_id
                )
                if profile_row is None:
                    workspace_row = await self._first_workspace_row(db)
                    if workspace_row is None:
                        now = _now()
                        cursor = await db.execute(
                            "INSERT INTO partner_workspaces "
                            "(name, slug, status, created_at, updated_at) "
                            "VALUES (?, ?, 'active', ?, ?)",
                            ("Основное рабочее пространство", "owner-workspace", now, now),
                        )
                        workspace_id = cursor.lastrowid
                        workspace_row = await self._workspace_row_by_id(
                            db, workspace_id or 0
                        )

                    if workspace_row is None:
                        raise RuntimeError("Не удалось создать рабочее пространство")

                    now = _now()
                    await db.execute(
                        "INSERT INTO partner_profiles "
                        "(workspace_id, telegram_user_id, partner_name, "
                        "project_name, business_description, communication_style, "
                        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            workspace_row["id"],
                            telegram_user_id,
                            "Владелец",
                            "Travel Advantage AI Ecosystem",
                            "Внутреннее партнёрское рабочее пространство.",
                            "Спокойный и профессиональный",
                            now,
                            now,
                        ),
                    )
                    profile_row = await self._profile_row_by_telegram_id(
                        db, telegram_user_id
                    )
                else:
                    workspace_row = await self._workspace_row_by_id(
                        db, profile_row["workspace_id"]
                    )

                if workspace_row is None or profile_row is None:
                    raise RuntimeError("Не удалось инициализировать профиль владельца")
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

        return _workspace_from_row(workspace_row), _profile_from_row(profile_row)

    async def get_workspace(self, workspace_id: int) -> PartnerWorkspace | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await self._workspace_row_by_id(db, workspace_id)
        return _workspace_from_row(row) if row is not None else None

    async def find_workspace_by_telegram_id(
        self, telegram_user_id: int
    ) -> PartnerWorkspace | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT w.* FROM partner_workspaces AS w "
                "JOIN partner_profiles AS p ON p.workspace_id = w.id "
                "WHERE p.telegram_user_id = ?",
                (telegram_user_id,),
            )
            row = await cursor.fetchone()
        return _workspace_from_row(row) if row is not None else None

    async def get_profile(self, workspace_id: int) -> PartnerProfile | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM partner_profiles WHERE workspace_id = ?",
                (workspace_id,),
            )
            row = await cursor.fetchone()
        return _profile_from_row(row) if row is not None else None

    async def update_profile(
        self,
        workspace_id: int,
        *,
        partner_name: str,
        project_name: str,
        business_description: str,
        communication_style: str,
    ) -> PartnerProfile | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                "UPDATE partner_profiles SET partner_name = ?, project_name = ?, "
                "business_description = ?, communication_style = ?, updated_at = ? "
                "WHERE workspace_id = ?",
                (
                    partner_name,
                    project_name,
                    business_description,
                    communication_style,
                    _now(),
                    workspace_id,
                ),
            )
            await db.commit()
            cursor = await db.execute(
                "SELECT * FROM partner_profiles WHERE workspace_id = ?",
                (workspace_id,),
            )
            row = await cursor.fetchone()
        return _profile_from_row(row) if row is not None else None

    @staticmethod
    async def _workspace_row_by_id(
        db: aiosqlite.Connection, workspace_id: int
    ) -> aiosqlite.Row | None:
        cursor = await db.execute(
            "SELECT * FROM partner_workspaces WHERE id = ?", (workspace_id,)
        )
        return await cursor.fetchone()

    @staticmethod
    async def _first_workspace_row(
        db: aiosqlite.Connection,
    ) -> aiosqlite.Row | None:
        cursor = await db.execute(
            "SELECT * FROM partner_workspaces ORDER BY id ASC LIMIT 1"
        )
        return await cursor.fetchone()

    @staticmethod
    async def _profile_row_by_telegram_id(
        db: aiosqlite.Connection, telegram_user_id: int
    ) -> aiosqlite.Row | None:
        cursor = await db.execute(
            "SELECT * FROM partner_profiles WHERE telegram_user_id = ?",
            (telegram_user_id,),
        )
        return await cursor.fetchone()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _workspace_from_row(row: aiosqlite.Row) -> PartnerWorkspace:
    return PartnerWorkspace(
        id=row["id"],
        name=row["name"],
        slug=row["slug"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _profile_from_row(row: aiosqlite.Row) -> PartnerProfile:
    return PartnerProfile(
        id=row["id"],
        workspace_id=row["workspace_id"],
        telegram_user_id=row["telegram_user_id"],
        partner_name=row["partner_name"],
        project_name=row["project_name"],
        business_description=row["business_description"],
        communication_style=row["communication_style"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
