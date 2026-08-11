from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import aiosqlite

from app.domain.partners import (
    PartnerProfile,
    PartnerWorkspace,
    WorkspaceContext,
    WorkspaceMembership,
)
from app.domain.business_profiles import (
    BUSINESS_PROFILE_SCHEMA_VERSION,
    BUSINESS_TYPES,
    PROFILE_STATUSES,
    BusinessClaim,
    BusinessContext,
    BusinessProfile,
    BusinessProfileValidationError,
    StaleBusinessProfileError,
)


WORKSPACE_ROLES = frozenset({"owner", "admin", "member"})
MEMBERSHIP_STATUSES = frozenset({"active", "inactive"})


class AmbiguousWorkspaceError(RuntimeError):
    """A user has more than one active workspace and none was selected."""


class OwnerMembershipConflictError(RuntimeError):
    """The owner's existing membership is not an active owner membership."""


class PartnerProvisioningConflictError(RuntimeError):
    """Existing tenant state is incompatible with an admin operation."""


class PartnerMembershipNotFoundError(RuntimeError):
    """No unambiguous membership exists for an admin status operation."""


@dataclass(frozen=True)
class ProvisionedPartner:
    workspace: PartnerWorkspace
    membership: WorkspaceMembership
    profile: BusinessProfile
    created: bool


class BusinessProfileSchemaError(RuntimeError):
    """partner_profiles has neither the known legacy nor the Stage-5 schema."""


_BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS partner_workspaces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspace_memberships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    telegram_user_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'member')),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'inactive')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES partner_workspaces(id),
    UNIQUE (workspace_id, telegram_user_id)
);

CREATE INDEX IF NOT EXISTS idx_workspace_memberships_user_status
    ON workspace_memberships(telegram_user_id, status);
"""

_PROFILE_SCHEMA = """CREATE TABLE {table_name} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL UNIQUE,
    telegram_user_id INTEGER UNIQUE,
    partner_name TEXT NOT NULL,
    project_name TEXT NOT NULL,
    business_description TEXT NOT NULL,
    communication_style TEXT NOT NULL,
    business_name TEXT NOT NULL,
    business_type TEXT NOT NULL CHECK (
        business_type IN ('independent_agent', 'club_partner', 'agency', 'travel_company', 'other')
    ),
    short_description TEXT NOT NULL,
    profile_status TEXT NOT NULL CHECK (profile_status IN ('incomplete', 'usable')),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    context_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES partner_workspaces(id)
)"""

_CONTEXT_KEYS = frozenset({
    "specializations", "destinations", "audiences", "markets", "positioning",
    "communication", "goals", "content_preferences", "public_contacts", "claims",
})
_POSITIONING_KEYS = frozenset({"statement", "value_proposition", "differentiators"})
_COMMUNICATION_KEYS = frozenset({
    "tone", "formality", "emoji_preference", "preferred_terms",
    "banned_formulations", "cta_preference",
})
_CONTENT_KEYS = frozenset({
    "formats", "channels", "preferred_topics", "prohibited_topics",
})
_CONTACT_KEYS = frozenset({
    "website", "phone", "email", "telegram", "vk", "booking_url",
})
_CLAIM_KEYS = frozenset({
    "text", "verification_status", "evidence_reference", "updated_at", "verified_at",
})
_LEGACY_PROFILE_COLUMNS = frozenset({
    "id", "workspace_id", "telegram_user_id", "partner_name", "project_name",
    "business_description", "communication_style", "created_at", "updated_at",
})
_STAGE5_PROFILE_COLUMNS = _LEGACY_PROFILE_COLUMNS | frozenset({
    "business_name", "business_type", "short_description", "profile_status",
    "schema_version", "revision", "context_json",
})


class PartnerRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.executescript(_BASE_SCHEMA)
            await db.commit()
            await self._init_profiles(db)

    async def _init_profiles(self, db: aiosqlite.Connection) -> None:
        db.row_factory = aiosqlite.Row
        exists = await (await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='partner_profiles'"
        )).fetchone()
        if exists is None:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(_PROFILE_SCHEMA.format(table_name="partner_profiles"))
                await _assert_foreign_keys(db)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
            return

        columns = {
            row["name"]: row
            for row in await (await db.execute("PRAGMA table_info(partner_profiles)")).fetchall()
        }
        column_names = frozenset(columns)
        if column_names == _STAGE5_PROFILE_COLUMNS:
            await _assert_stage5_profile_schema(db, columns)
            await _assert_foreign_keys(db)
            return
        if column_names != _LEGACY_PROFILE_COLUMNS:
            raise BusinessProfileSchemaError(
                "partner_profiles имеет несовместимую частично мигрированную схему"
            )
        await _assert_legacy_profile_schema(db, columns)

        await db.execute("BEGIN IMMEDIATE")
        try:
            legacy_rows = await (await db.execute(
                "SELECT * FROM partner_profiles ORDER BY id"
            )).fetchall()
            await db.execute(_PROFILE_SCHEMA.format(table_name="partner_profiles_new"))
            for row in legacy_rows:
                context = empty_business_context()
                context["communication"]["tone"] = row["communication_style"]
                await db.execute(
                    "INSERT INTO partner_profiles_new "
                    "(id, workspace_id, telegram_user_id, partner_name, project_name, "
                    "business_description, communication_style, business_name, business_type, "
                    "short_description, profile_status, schema_version, revision, context_json, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?)",
                    (
                        row["id"], row["workspace_id"], row["telegram_user_id"],
                        row["partner_name"], row["project_name"],
                        row["business_description"], row["communication_style"],
                        row["project_name"], _legacy_business_type(row),
                        row["business_description"],
                        "incomplete", _encode_context(context), row["created_at"], row["updated_at"],
                    ),
                )
            copied = await (await db.execute(
                "SELECT COUNT(*) FROM partner_profiles_new"
            )).fetchone()
            if copied[0] != len(legacy_rows):
                raise RuntimeError("Не удалось сохранить все legacy business profiles")
            await db.execute("DROP TABLE partner_profiles")
            await db.execute("ALTER TABLE partner_profiles_new RENAME TO partner_profiles")
            await _assert_foreign_keys(db)
            await db.commit()
        except BaseException:
            await db.rollback()
            raise

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
                        "business_name, business_type, short_description, profile_status, "
                        "schema_version, revision, context_json, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 'club_partner', ?, 'incomplete', 1, 1, ?, ?, ?)",
                        (
                            workspace_row["id"],
                            telegram_user_id,
                            "Владелец",
                            "Travel Advantage AI Ecosystem",
                            "Внутреннее партнёрское рабочее пространство.",
                            "Спокойный и профессиональный",
                            "Travel Advantage AI Ecosystem",
                            "Внутреннее партнёрское рабочее пространство.",
                            _encode_context(_legacy_context("Спокойный и профессиональный")),
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

    async def provision_partner(
        self,
        telegram_user_id: int,
        workspace_name: str,
        workspace_slug: str,
        *,
        business_name: str,
        business_type: str,
        short_description: str,
        context: Mapping[str, Any],
        schema_version: int = BUSINESS_PROFILE_SCHEMA_VERSION,
    ) -> ProvisionedPartner:
        if type(telegram_user_id) is not int or telegram_user_id < 1:
            raise ValueError("telegram_user_id должен быть положительным целым числом")
        if not isinstance(workspace_name, str) or not workspace_name.strip():
            raise ValueError("workspace_name обязателен")
        if not isinstance(workspace_slug, str) or not workspace_slug.strip():
            raise ValueError("workspace_slug обязателен")
        normalized = validate_business_profile_input(
            business_name, business_type, short_description, context, schema_version
        )
        name = workspace_name.strip()
        slug = workspace_slug.strip()
        business_name = business_name.strip()
        short_description = short_description.strip()
        profile_status = _profile_status(
            business_name, short_description, normalized
        )
        tone = normalized["communication"]["tone"]

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                workspace_row = await (await db.execute(
                    "SELECT * FROM partner_workspaces WHERE slug = ?", (slug,)
                )).fetchone()
                memberships = await (await db.execute(
                    "SELECT * FROM workspace_memberships "
                    "WHERE telegram_user_id = ? ORDER BY workspace_id",
                    (telegram_user_id,),
                )).fetchall()

                if workspace_row is not None:
                    result = await self._existing_provisioned_partner(
                        db, workspace_row, memberships,
                        telegram_user_id=telegram_user_id,
                        workspace_name=name,
                        business_name=business_name,
                        business_type=business_type,
                        short_description=short_description,
                        normalized_context=normalized,
                        input_context=context,
                    )
                    await db.commit()
                    return result

                if memberships:
                    raise PartnerProvisioningConflictError(
                        "Telegram user уже связан с другим workspace"
                    )

                now = _now()
                cursor = await db.execute(
                    "INSERT INTO partner_workspaces "
                    "(name, slug, status, created_at, updated_at) "
                    "VALUES (?, ?, 'active', ?, ?)",
                    (name, slug, now, now),
                )
                workspace_id = cursor.lastrowid or 0
                membership_cursor = await db.execute(
                    "INSERT INTO workspace_memberships "
                    "(workspace_id, telegram_user_id, role, status, created_at, updated_at) "
                    "VALUES (?, ?, 'owner', 'active', ?, ?)",
                    (workspace_id, telegram_user_id, now, now),
                )
                profile_cursor = await db.execute(
                    "INSERT INTO partner_profiles "
                    "(workspace_id, telegram_user_id, partner_name, project_name, "
                    "business_description, communication_style, business_name, business_type, "
                    "short_description, profile_status, schema_version, revision, context_json, "
                    "created_at, updated_at) "
                    "VALUES (?, NULL, '', ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                    (
                        workspace_id, business_name, short_description, tone,
                        business_name, business_type, short_description, profile_status,
                        schema_version, _encode_context(normalized), now, now,
                    ),
                )
                workspace_row = await self._workspace_row_by_id(db, workspace_id)
                membership_row = await self._membership_row_by_id(
                    db, membership_cursor.lastrowid or 0
                )
                profile_row = await (await db.execute(
                    "SELECT * FROM partner_profiles WHERE id = ?",
                    (profile_cursor.lastrowid or 0,),
                )).fetchone()
                if workspace_row is None or membership_row is None or profile_row is None:
                    raise RuntimeError("Не удалось создать полный tenant")
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return ProvisionedPartner(
            _workspace_from_row(workspace_row),
            _membership_from_row(membership_row),
            _business_profile_from_row(profile_row),
            True,
        )

    async def set_partner_membership_status(
        self, telegram_user_id: int, status: str
    ) -> WorkspaceMembership:
        if type(telegram_user_id) is not int or telegram_user_id < 1:
            raise ValueError("telegram_user_id должен быть положительным целым числом")
        if status not in MEMBERSHIP_STATUSES:
            raise ValueError("Недопустимый membership status")
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                rows = await (await db.execute(
                    "SELECT * FROM workspace_memberships "
                    "WHERE telegram_user_id = ? ORDER BY workspace_id",
                    (telegram_user_id,),
                )).fetchall()
                if len(rows) != 1:
                    raise PartnerMembershipNotFoundError(
                        "Ожидалась ровно одна membership для Telegram user"
                    )
                row = rows[0]
                if status == "active":
                    workspace = await self._workspace_row_by_id(db, row["workspace_id"])
                    profile = await (await db.execute(
                        "SELECT 1 FROM partner_profiles WHERE workspace_id = ?",
                        (row["workspace_id"],),
                    )).fetchone()
                    if workspace is None or workspace["status"] != "active" or profile is None:
                        raise PartnerProvisioningConflictError(
                            "Tenant не готов к reactivation"
                        )
                if row["status"] != status:
                    await db.execute(
                        "UPDATE workspace_memberships SET status = ?, updated_at = ? "
                        "WHERE id = ?", (status, _now(), row["id"]),
                    )
                    row = await self._membership_row_by_id(db, row["id"])
                if row is None:
                    raise RuntimeError("Membership исчезла при обновлении")
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return _membership_from_row(row)

    async def _existing_provisioned_partner(
        self,
        db: aiosqlite.Connection,
        workspace_row: aiosqlite.Row,
        memberships: list[aiosqlite.Row],
        *,
        telegram_user_id: int,
        workspace_name: str,
        business_name: str,
        business_type: str,
        short_description: str,
        normalized_context: Mapping[str, Any],
        input_context: Mapping[str, Any],
    ) -> ProvisionedPartner:
        expected = [
            row for row in memberships if row["workspace_id"] == workspace_row["id"]
        ]
        profile_row = await (await db.execute(
            "SELECT * FROM partner_profiles WHERE workspace_id = ?",
            (workspace_row["id"],),
        )).fetchone()
        if (
            workspace_row["name"] != workspace_name
            or workspace_row["status"] != "active"
            or len(memberships) != 1
            or len(expected) != 1
            or expected[0]["telegram_user_id"] != telegram_user_id
            or expected[0]["role"] != "owner"
            or expected[0]["status"] != "active"
            or profile_row is None
        ):
            raise PartnerProvisioningConflictError(
                "Существующий tenant несовместим с provisioning request"
            )
        profile = _business_profile_from_row(profile_row)
        if (
            profile.business_name != business_name
            or profile.business_type != business_type
            or profile.short_description != short_description
            or not _profile_context_matches_input(
                profile.context, normalized_context, input_context
            )
        ):
            raise PartnerProvisioningConflictError(
                "Существующий Business Profile несовместим с provisioning request"
            )
        return ProvisionedPartner(
            _workspace_from_row(workspace_row),
            _membership_from_row(expected[0]), profile, False,
        )

    async def create_membership(
        self,
        workspace_id: int,
        telegram_user_id: int,
        *,
        role: str,
        status: str = "active",
    ) -> WorkspaceMembership:
        _validate_membership(role, status)
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            cursor = await db.execute(
                "INSERT INTO workspace_memberships "
                "(workspace_id, telegram_user_id, role, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (workspace_id, telegram_user_id, role, status, now, now),
            )
            await db.commit()
            row = await self._membership_row_by_id(db, cursor.lastrowid or 0)
        if row is None:
            raise RuntimeError("Не удалось создать membership")
        return _membership_from_row(row)

    async def get_membership(
        self, workspace_id: int, telegram_user_id: int
    ) -> WorkspaceMembership | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM workspace_memberships "
                "WHERE workspace_id = ? AND telegram_user_id = ?",
                (workspace_id, telegram_user_id),
            )
            row = await cursor.fetchone()
        return _membership_from_row(row) if row is not None else None

    async def list_memberships_by_telegram_id(
        self, telegram_user_id: int, *, status: str | None = None
    ) -> list[WorkspaceMembership]:
        if status is not None and status not in MEMBERSHIP_STATUSES:
            raise ValueError("Недопустимый membership status")
        condition = " AND status = ?" if status is not None else ""
        params: tuple[object, ...] = (
            (telegram_user_id, status) if status is not None else (telegram_user_id,)
        )
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM workspace_memberships WHERE telegram_user_id = ?"
                + condition
                + " ORDER BY workspace_id ASC",
                params,
            )
            rows = await cursor.fetchall()
        return [_membership_from_row(row) for row in rows]

    async def resolve_workspace_context(
        self, telegram_user_id: int
    ) -> WorkspaceContext | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT m.telegram_user_id, m.workspace_id, m.role, "
                "w.status AS workspace_status "
                "FROM workspace_memberships AS m "
                "JOIN partner_workspaces AS w ON w.id = m.workspace_id "
                "WHERE m.telegram_user_id = ? AND m.status = 'active' "
                "ORDER BY m.workspace_id ASC",
                (telegram_user_id,),
            )
            rows = await cursor.fetchall()
        if not rows:
            return None
        if len(rows) > 1:
            raise AmbiguousWorkspaceError(
                "Для пользователя найдено несколько активных рабочих пространств"
            )
        row = rows[0]
        if row["workspace_status"] != "active":
            return None
        return WorkspaceContext(
            telegram_user_id=row["telegram_user_id"],
            workspace_id=row["workspace_id"],
            role=row["role"],
            workspace_status=row["workspace_status"],
        )

    async def bootstrap_owner_membership(
        self, telegram_user_id: int
    ) -> WorkspaceMembership | None:
        """Backfill the known owner; create defaults only for a completely empty DB."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                profile_row = await self._profile_row_by_telegram_id(
                    db, telegram_user_id
                )
                if profile_row is None:
                    count_row = await (
                        await db.execute("SELECT COUNT(*) FROM partner_workspaces")
                    ).fetchone()
                    if count_row[0] != 0:
                        await db.rollback()
                        return None
                    workspace, _ = await self._create_initial_owner(db, telegram_user_id)
                    workspace_id = workspace["id"]
                else:
                    workspace_id = profile_row["workspace_id"]

                row = await self._membership_row(db, workspace_id, telegram_user_id)
                if row is None:
                    now = _now()
                    cursor = await db.execute(
                        "INSERT INTO workspace_memberships "
                        "(workspace_id, telegram_user_id, role, status, created_at, updated_at) "
                        "VALUES (?, ?, 'owner', 'active', ?, ?)",
                        (workspace_id, telegram_user_id, now, now),
                    )
                    row = await self._membership_row_by_id(db, cursor.lastrowid or 0)
                elif row["role"] != "owner" or row["status"] != "active":
                    raise OwnerMembershipConflictError(
                        "Membership владельца существует, но не является owner/active"
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return _membership_from_row(row) if row is not None else None

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

    async def get_business_profile(self, workspace_id: int) -> BusinessProfile | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                "SELECT * FROM partner_profiles WHERE workspace_id = ?", (workspace_id,)
            )).fetchone()
        return _business_profile_from_row(row) if row is not None else None

    async def create_business_profile(
        self,
        workspace_id: int,
        *,
        business_name: str,
        business_type: str,
        short_description: str,
        context: Mapping[str, Any],
        schema_version: int = BUSINESS_PROFILE_SCHEMA_VERSION,
    ) -> BusinessProfile:
        normalized = validate_business_profile_input(
            business_name, business_type, short_description, context, schema_version
        )
        now = _now()
        status = _profile_status(business_name, short_description, normalized)
        tone = normalized["communication"]["tone"]
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            cursor = await db.execute(
                "INSERT INTO partner_profiles "
                "(workspace_id, telegram_user_id, partner_name, project_name, "
                "business_description, communication_style, business_name, business_type, "
                "short_description, profile_status, schema_version, revision, context_json, "
                "created_at, updated_at) "
                "VALUES (?, NULL, '', ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (
                    workspace_id, business_name.strip(), short_description.strip(), tone,
                    business_name.strip(), business_type, short_description.strip(), status,
                    schema_version, _encode_context(normalized), now, now,
                ),
            )
            await db.commit()
            row = await (await db.execute(
                "SELECT * FROM partner_profiles WHERE id = ?", (cursor.lastrowid,)
            )).fetchone()
        if row is None:
            raise RuntimeError("Не удалось создать Business Profile")
        return _business_profile_from_row(row)

    async def update_business_profile(
        self,
        workspace_id: int,
        expected_revision: int,
        *,
        business_name: str,
        business_type: str,
        short_description: str,
        context: Mapping[str, Any],
        schema_version: int = BUSINESS_PROFILE_SCHEMA_VERSION,
    ) -> BusinessProfile:
        if expected_revision < 1:
            raise ValueError("expected_revision должен быть положительным")
        normalized = validate_business_profile_input(
            business_name, business_type, short_description, context, schema_version
        )
        status = _profile_status(business_name, short_description, normalized)
        tone = normalized["communication"]["tone"]
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "UPDATE partner_profiles SET business_name=?, business_type=?, "
                "short_description=?, profile_status=?, schema_version=?, context_json=?, "
                "project_name=?, business_description=?, communication_style=?, "
                "revision=revision+1, updated_at=? "
                "WHERE workspace_id=? AND revision=?",
                (
                    business_name.strip(), business_type, short_description.strip(), status,
                    schema_version, _encode_context(normalized), business_name.strip(),
                    short_description.strip(), tone, _now(), workspace_id, expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise StaleBusinessProfileError(
                    "Business Profile отсутствует или был изменён другим пользователем"
                )
            await db.commit()
            row = await (await db.execute(
                "SELECT * FROM partner_profiles WHERE workspace_id = ?", (workspace_id,)
            )).fetchone()
        if row is None:
            raise RuntimeError("Business Profile исчез после обновления")
        return _business_profile_from_row(row)

    async def update_profile(
        self,
        workspace_id: int,
        *,
        partner_name: str,
        project_name: str,
        business_description: str,
        communication_style: str,
    ) -> PartnerProfile | None:
        current = await self.get_business_profile(workspace_id)
        context = empty_business_context() if current is None else business_context_to_dict(current.context)
        context["communication"]["tone"] = communication_style.strip()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                "UPDATE partner_profiles SET partner_name = ?, project_name = ?, "
                "business_description = ?, communication_style = ?, business_name = ?, "
                "short_description = ?, context_json = ?, revision = revision + 1, updated_at = ? "
                "WHERE workspace_id = ?",
                (
                    partner_name,
                    project_name,
                    business_description,
                    communication_style,
                    project_name,
                    business_description,
                    _encode_context(context),
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

    @staticmethod
    async def _membership_row(
        db: aiosqlite.Connection, workspace_id: int, telegram_user_id: int
    ) -> aiosqlite.Row | None:
        cursor = await db.execute(
            "SELECT * FROM workspace_memberships "
            "WHERE workspace_id = ? AND telegram_user_id = ?",
            (workspace_id, telegram_user_id),
        )
        return await cursor.fetchone()

    @staticmethod
    async def _membership_row_by_id(
        db: aiosqlite.Connection, membership_id: int
    ) -> aiosqlite.Row | None:
        cursor = await db.execute(
            "SELECT * FROM workspace_memberships WHERE id = ?", (membership_id,)
        )
        return await cursor.fetchone()

    @staticmethod
    async def _create_initial_owner(
        db: aiosqlite.Connection, telegram_user_id: int
    ) -> tuple[aiosqlite.Row, aiosqlite.Row]:
        now = _now()
        cursor = await db.execute(
            "INSERT INTO partner_workspaces "
            "(name, slug, status, created_at, updated_at) "
            "VALUES (?, ?, 'active', ?, ?)",
            ("Основное рабочее пространство", "owner-workspace", now, now),
        )
        workspace_row = await PartnerRepository._workspace_row_by_id(
            db, cursor.lastrowid or 0
        )
        if workspace_row is None:
            raise RuntimeError("Не удалось создать рабочее пространство")
        await db.execute(
            "INSERT INTO partner_profiles "
            "(workspace_id, telegram_user_id, partner_name, project_name, "
            "business_description, communication_style, business_name, business_type, "
            "short_description, profile_status, schema_version, revision, context_json, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'club_partner', ?, 'incomplete', 1, 1, ?, ?, ?)",
            (
                workspace_row["id"], telegram_user_id, "Владелец",
                "Travel Advantage AI Ecosystem",
                "Внутреннее партнёрское рабочее пространство.",
                "Спокойный и профессиональный", "Travel Advantage AI Ecosystem",
                "Внутреннее партнёрское рабочее пространство.",
                _encode_context(_legacy_context("Спокойный и профессиональный")), now, now,
            ),
        )
        profile_row = await PartnerRepository._profile_row_by_telegram_id(
            db, telegram_user_id
        )
        if profile_row is None:
            raise RuntimeError("Не удалось создать профиль владельца")
        return workspace_row, profile_row


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


def _business_profile_from_row(row: aiosqlite.Row) -> BusinessProfile:
    if row["schema_version"] != BUSINESS_PROFILE_SCHEMA_VERSION:
        raise BusinessProfileValidationError("Неизвестная версия Business Profile")
    try:
        raw_context = json.loads(row["context_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise BusinessProfileValidationError("Некорректный context_json") from exc
    normalized = validate_business_context(raw_context)
    return BusinessProfile(
        id=row["id"], workspace_id=row["workspace_id"],
        business_name=row["business_name"], business_type=row["business_type"],
        short_description=row["short_description"], profile_status=row["profile_status"],
        schema_version=row["schema_version"], revision=row["revision"],
        context=_context_dto(normalized), created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def empty_business_context() -> dict[str, Any]:
    return {
        "specializations": [], "destinations": [], "audiences": [], "markets": [],
        "positioning": {"statement": "", "value_proposition": "", "differentiators": []},
        "communication": {
            "tone": "", "formality": "", "emoji_preference": "",
            "preferred_terms": [], "banned_formulations": [], "cta_preference": "",
        },
        "goals": [],
        "content_preferences": {
            "formats": [], "channels": [], "preferred_topics": [], "prohibited_topics": [],
        },
        "public_contacts": {
            "website": "", "phone": "", "email": "", "telegram": "", "vk": "",
            "booking_url": "",
        },
        "claims": [],
    }


def validate_business_profile_input(
    business_name: str,
    business_type: str,
    short_description: str,
    context: Mapping[str, Any],
    schema_version: int,
) -> dict[str, Any]:
    if schema_version != BUSINESS_PROFILE_SCHEMA_VERSION:
        raise BusinessProfileValidationError("Неизвестная версия Business Profile")
    if not isinstance(business_name, str) or not business_name.strip():
        raise BusinessProfileValidationError("business_name обязателен")
    if business_type not in BUSINESS_TYPES:
        raise BusinessProfileValidationError("Недопустимый business_type")
    if not isinstance(short_description, str):
        raise BusinessProfileValidationError("short_description должен быть строкой")
    return validate_business_context(context)


def validate_business_context(context: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(context, Mapping) or not set(context) <= _CONTEXT_KEYS:
        raise BusinessProfileValidationError("context содержит неизвестные sections")
    normalized = empty_business_context()
    for key in ("specializations", "destinations", "audiences", "markets", "goals"):
        if key in context:
            normalized[key] = _string_list(context[key], key)
    for key, allowed, list_keys in (
        ("positioning", _POSITIONING_KEYS, {"differentiators"}),
        ("communication", _COMMUNICATION_KEYS, {"preferred_terms", "banned_formulations"}),
        ("content_preferences", _CONTENT_KEYS, set(_CONTENT_KEYS)),
    ):
        if key in context:
            normalized[key].update(_validate_object(
                context[key], allowed, key, list_keys=list_keys
            ))
    contacts = context.get("public_contacts", {})
    if not isinstance(contacts, Mapping) or not set(contacts) <= _CONTACT_KEYS:
        raise BusinessProfileValidationError("public_contacts имеет неизвестные поля")
    normalized["public_contacts"].update({
        key: _string(value, f"public_contacts.{key}") for key, value in contacts.items()
    })
    claims = context.get("claims", [])
    if not isinstance(claims, list):
        raise BusinessProfileValidationError("claims должен быть списком")
    normalized["claims"] = [_validate_claim(item) for item in claims]
    return normalized


def business_context_to_dict(context: BusinessContext) -> dict[str, Any]:
    return {
        "specializations": list(context.specializations),
        "destinations": list(context.destinations),
        "audiences": list(context.audiences),
        "markets": list(context.markets),
        "positioning": {
            **dict(context.positioning),
            "differentiators": list(context.positioning["differentiators"]),
        },
        "communication": {
            **dict(context.communication),
            "preferred_terms": list(context.communication["preferred_terms"]),
            "banned_formulations": list(context.communication["banned_formulations"]),
        },
        "goals": list(context.goals),
        "content_preferences": {
            key: list(value) for key, value in context.content_preferences.items()
        },
        "public_contacts": dict(context.public_contacts),
        "claims": [
            {
                "text": claim.text, "verification_status": claim.verification_status,
                "evidence_reference": claim.evidence_reference, "updated_at": claim.updated_at,
                "verified_at": claim.verified_at,
            }
            for claim in context.claims
        ],
    }


def _profile_context_matches_input(
    stored: BusinessContext,
    normalized: Mapping[str, Any],
    original: Mapping[str, Any],
) -> bool:
    stored_values = business_context_to_dict(stored)
    expected = json.loads(json.dumps(normalized, ensure_ascii=False))
    original_claims = original.get("claims", [])
    for index, claim in enumerate(original_claims):
        if not claim.get("updated_at"):
            expected["claims"][index]["updated_at"] = stored_values["claims"][index][
                "updated_at"
            ]
    return stored_values == expected


def _validate_object(
    value: Any, keys: frozenset[str], name: str, *, list_keys: set[str]
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not set(value) <= keys:
        raise BusinessProfileValidationError(f"{name} имеет неизвестные поля")
    return {
        key: _string_list(value[key], f"{name}.{key}")
        if key in list_keys else _string(value[key], f"{name}.{key}")
        for key in value
    }


def _validate_claim(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not set(value) <= _CLAIM_KEYS:
        raise BusinessProfileValidationError("claim имеет неизвестные поля")
    text = _string(value.get("text"), "claim.text").strip()
    if not text:
        raise BusinessProfileValidationError("claim.text обязателен")
    status = value.get("verification_status", "unverified")
    if status not in {"unverified", "verified"}:
        raise BusinessProfileValidationError("Недопустимый verification_status")
    evidence = value.get("evidence_reference")
    verified_at = value.get("verified_at")
    if evidence is not None and not isinstance(evidence, str):
        raise BusinessProfileValidationError("evidence_reference должен быть строкой или null")
    if verified_at is not None and not isinstance(verified_at, str):
        raise BusinessProfileValidationError("verified_at должен быть строкой или null")
    updated_at = value.get("updated_at") or _now()
    if not isinstance(updated_at, str):
        raise BusinessProfileValidationError("updated_at должен быть строкой")
    return {
        "text": text, "verification_status": status,
        "evidence_reference": evidence, "updated_at": updated_at,
        "verified_at": verified_at,
    }


def _string_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise BusinessProfileValidationError(f"{name} должен быть list[str]")
    return [item.strip() for item in value]


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise BusinessProfileValidationError(f"{name} должен быть строкой")
    return value.strip()


def _profile_status(
    business_name: str, short_description: str, context: Mapping[str, Any]
) -> str:
    status = "usable" if (
        business_name.strip() and short_description.strip() and context["specializations"]
    ) else "incomplete"
    if status not in PROFILE_STATUSES:
        raise AssertionError("Некорректный profile status")
    return status


def _context_dto(context: Mapping[str, Any]) -> BusinessContext:
    return BusinessContext(
        specializations=tuple(context["specializations"]),
        destinations=tuple(context["destinations"]),
        audiences=tuple(context["audiences"]), markets=tuple(context["markets"]),
        positioning=MappingProxyType({
            **context["positioning"],
            "differentiators": tuple(context["positioning"]["differentiators"]),
        }),
        communication=MappingProxyType({
            **context["communication"],
            "preferred_terms": tuple(context["communication"]["preferred_terms"]),
            "banned_formulations": tuple(context["communication"]["banned_formulations"]),
        }), goals=tuple(context["goals"]),
        content_preferences=MappingProxyType({
            key: tuple(value) for key, value in context["content_preferences"].items()
        }),
        public_contacts=MappingProxyType(dict(context["public_contacts"])),
        claims=tuple(BusinessClaim(**claim) for claim in context["claims"]),
    )


def _legacy_context(tone: str) -> dict[str, Any]:
    context = empty_business_context()
    context["communication"]["tone"] = tone
    return context


def _encode_context(context: Mapping[str, Any]) -> str:
    return json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


async def _assert_stage5_profile_schema(
    db: aiosqlite.Connection, columns: Mapping[str, aiosqlite.Row]
) -> None:
    if columns["id"]["pk"] != 1:
        raise BusinessProfileSchemaError("partner_profiles.id не является primary key")
    required_not_null = _STAGE5_PROFILE_COLUMNS - {"id", "telegram_user_id"}
    if any(columns[name]["notnull"] != 1 for name in required_not_null):
        raise BusinessProfileSchemaError("Нарушена Stage-5 nullability partner_profiles")
    if columns["telegram_user_id"]["notnull"] != 0:
        raise BusinessProfileSchemaError("telegram_user_id должен быть nullable")
    default = str(columns["revision"]["dflt_value"] or "").strip("'\"() ")
    if default != "1":
        raise BusinessProfileSchemaError("revision должен иметь default 1")
    await _assert_unique_and_fk(db)
    sql_row = await (await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='partner_profiles'"
    )).fetchone()
    normalized = "".join(str(sql_row["sql"] or "").lower().split())
    fragments = (
        "check(business_typein('independent_agent','club_partner','agency','travel_company','other'))",
        "check(profile_statusin('incomplete','usable'))",
        "check(schema_version=1)",
        "check(revision>=1)",
    )
    if any(fragment not in normalized for fragment in fragments):
        raise BusinessProfileSchemaError("Нарушены Stage-5 CHECK constraints")


async def _assert_legacy_profile_schema(
    db: aiosqlite.Connection, columns: Mapping[str, aiosqlite.Row]
) -> None:
    if columns["id"]["pk"] != 1:
        raise BusinessProfileSchemaError("Legacy partner_profiles.id не является primary key")
    if any(columns[name]["notnull"] != 1 for name in _LEGACY_PROFILE_COLUMNS - {"id"}):
        raise BusinessProfileSchemaError("Нарушена legacy nullability partner_profiles")
    await _assert_unique_and_fk(db)


async def _assert_unique_and_fk(db: aiosqlite.Connection) -> None:
    unique_columns: set[tuple[str, ...]] = set()
    indexes = await (await db.execute("PRAGMA index_list(partner_profiles)")).fetchall()
    for index in indexes:
        if index["unique"] != 1:
            continue
        fields = await (await db.execute(
            f"PRAGMA index_info({json.dumps(index['name'])})"
        )).fetchall()
        unique_columns.add(tuple(field["name"] for field in fields))
    if not {("workspace_id",), ("telegram_user_id",)} <= unique_columns:
        raise BusinessProfileSchemaError("Нарушены UNIQUE constraints partner_profiles")
    foreign_keys = await (await db.execute(
        "PRAGMA foreign_key_list(partner_profiles)"
    )).fetchall()
    if not any(
        row["table"] == "partner_workspaces"
        and row["from"] == "workspace_id"
        and row["to"] == "id"
        for row in foreign_keys
    ):
        raise BusinessProfileSchemaError("Отсутствует FK partner_profiles.workspace_id")


def _legacy_business_type(row: aiosqlite.Row) -> str:
    if (
        row["project_name"] == "Travel Advantage AI Ecosystem"
        and row["business_description"]
        == "Внутреннее партнёрское рабочее пространство."
    ):
        return "club_partner"
    return "other"


async def _assert_foreign_keys(db: aiosqlite.Connection) -> None:
    violations = await (await db.execute(
        "PRAGMA foreign_key_check(partner_profiles)"
    )).fetchall()
    if violations:
        raise RuntimeError("Business Profile migration нарушила foreign keys")


def _membership_from_row(row: aiosqlite.Row) -> WorkspaceMembership:
    return WorkspaceMembership(
        id=row["id"],
        workspace_id=row["workspace_id"],
        telegram_user_id=row["telegram_user_id"],
        role=row["role"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _validate_membership(role: str, status: str) -> None:
    if role not in WORKSPACE_ROLES:
        raise ValueError("Недопустимая роль workspace membership")
    if status not in MEMBERSHIP_STATUSES:
        raise ValueError("Недопустимый membership status")
