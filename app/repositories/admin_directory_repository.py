"""READ-ONLY, cross-tenant reporting queries for the Beta Control Center
only (app/admin_api.py). Every other repository in this codebase
deliberately scopes every read to one workspace - this one exists
precisely BECAUSE the platform admin is the one legitimate caller allowed
to see across tenants, gated by app.web_api.require_platform_admin. Never
imported or used by any regular-user-facing code path - that boundary is
enforced by convention (only app/admin_api.py imports this module), not by
a runtime check, so keep it that way.

Joins existing tables (partner_workspaces, partner_profiles,
workspace_memberships, workspace_subscriptions, web_auth_bindings,
web_auth_users) in the same shared journal DB - no new workspace/business
data model, just a read path across what already exists.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite

from app.domain.admin_directory import WorkspaceDirectoryRow, WorkspaceMemberRow

_MAX_LIMIT = 200


class AdminDirectoryRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def search_workspaces(
        self, *, query: str | None = None, limit: int = 20, offset: int = 0,
    ) -> tuple[list[WorkspaceDirectoryRow], int]:
        """Search matches workspace name/slug, business name, any member's
        web-auth email, or any member's telegram_user_id - see the task's
        search requirement (email / telegram id / workspace-or-business
        name)."""
        limit = max(1, min(limit, _MAX_LIMIT))
        clauses: list[str] = []
        params: list[object] = []
        if query and query.strip():
            like = f"%{query.strip()}%"
            clauses.append(
                "(w.name LIKE ? OR w.slug LIKE ? OR p.business_name LIKE ? "
                "OR EXISTS (SELECT 1 FROM web_auth_bindings b "
                "JOIN web_auth_users u ON u.id = b.web_user_id "
                "WHERE b.workspace_id = w.id AND u.email LIKE ?) "
                "OR EXISTS (SELECT 1 FROM workspace_memberships m "
                "WHERE m.workspace_id = w.id AND CAST(m.telegram_user_id AS TEXT) LIKE ?))"
            )
            params.extend([like, like, like, like, like])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            count_row = await (await db.execute(
                "SELECT COUNT(*) FROM partner_workspaces w "
                f"LEFT JOIN partner_profiles p ON p.workspace_id = w.id {where}",
                params,
            )).fetchone()
            total = int(count_row[0]) if count_row else 0

            cursor = await db.execute(
                "SELECT w.id, w.name, w.slug, w.status, w.created_at, "
                "p.business_name, p.ta_affiliated, "
                "s.status AS sub_status, s.plan AS sub_plan, s.paid_until, s.trial_until "
                "FROM partner_workspaces w "
                "LEFT JOIN partner_profiles p ON p.workspace_id = w.id "
                "LEFT JOIN workspace_subscriptions s ON s.workspace_id = w.id "
                f"{where} ORDER BY w.id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            )
            rows = await cursor.fetchall()
            workspace_ids = [row["id"] for row in rows]
            emails, onboarding = await _primary_emails(db, workspace_ids)

        result = [
            WorkspaceDirectoryRow(
                workspace_id=row["id"], name=row["name"], slug=row["slug"],
                status=row["status"], created_at=row["created_at"],
                business_name=row["business_name"],
                ta_affiliated=bool(row["ta_affiliated"]) if row["ta_affiliated"] is not None else False,
                subscription_status=row["sub_status"], plan=row["sub_plan"],
                paid_until=row["paid_until"], trial_until=row["trial_until"],
                primary_email=emails.get(row["id"]),
                onboarding_completed=onboarding.get(row["id"]),
            )
            for row in rows
        ]
        return result, total

    async def get_workspace(self, workspace_id: int) -> WorkspaceDirectoryRow | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                "SELECT w.id, w.name, w.slug, w.status, w.created_at, "
                "p.business_name, p.ta_affiliated, "
                "s.status AS sub_status, s.plan AS sub_plan, s.paid_until, s.trial_until "
                "FROM partner_workspaces w "
                "LEFT JOIN partner_profiles p ON p.workspace_id = w.id "
                "LEFT JOIN workspace_subscriptions s ON s.workspace_id = w.id "
                "WHERE w.id = ?",
                (workspace_id,),
            )).fetchone()
            if row is None:
                return None
            emails, onboarding = await _primary_emails(db, [workspace_id])
        return WorkspaceDirectoryRow(
            workspace_id=row["id"], name=row["name"], slug=row["slug"],
            status=row["status"], created_at=row["created_at"],
            business_name=row["business_name"],
            ta_affiliated=bool(row["ta_affiliated"]) if row["ta_affiliated"] is not None else False,
            subscription_status=row["sub_status"], plan=row["sub_plan"],
            paid_until=row["paid_until"], trial_until=row["trial_until"],
            primary_email=emails.get(workspace_id),
            onboarding_completed=onboarding.get(workspace_id),
        )

    async def get_members(self, workspace_id: int) -> list[WorkspaceMemberRow]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            member_rows = await (await db.execute(
                "SELECT telegram_user_id, role, status FROM workspace_memberships "
                "WHERE workspace_id = ? ORDER BY id ASC",
                (workspace_id,),
            )).fetchall()
            binding_rows = await (await db.execute(
                "SELECT b.telegram_user_id, u.email, b.onboarding_completed_at "
                "FROM web_auth_bindings b JOIN web_auth_users u ON u.id = b.web_user_id "
                "WHERE b.workspace_id = ? ORDER BY b.id ASC",
                (workspace_id,),
            )).fetchall()
        email_by_telegram_id: dict[int, str] = {}
        onboarding_by_telegram_id: dict[int, bool] = {}
        for row in binding_rows:
            tid = row["telegram_user_id"]
            if tid not in email_by_telegram_id:
                email_by_telegram_id[tid] = row["email"]
                onboarding_by_telegram_id[tid] = row["onboarding_completed_at"] is not None
        return [
            WorkspaceMemberRow(
                telegram_user_id=row["telegram_user_id"], role=row["role"],
                status=row["status"],
                email=email_by_telegram_id.get(row["telegram_user_id"]),
                onboarding_completed=onboarding_by_telegram_id.get(row["telegram_user_id"]),
            )
            for row in member_rows
        ]


async def _primary_emails(
    db: aiosqlite.Connection, workspace_ids: list[int],
) -> tuple[dict[int, str], dict[int, bool]]:
    if not workspace_ids:
        return {}, {}
    placeholders = ",".join("?" for _ in workspace_ids)
    cursor = await db.execute(
        "SELECT b.workspace_id, u.email, b.onboarding_completed_at, b.id "
        "FROM web_auth_bindings b JOIN web_auth_users u ON u.id = b.web_user_id "
        f"WHERE b.workspace_id IN ({placeholders}) ORDER BY b.id ASC",
        workspace_ids,
    )
    rows = await cursor.fetchall()
    emails: dict[int, str] = {}
    onboarding: dict[int, bool] = {}
    for row in rows:
        wid = row["workspace_id"]
        if wid not in emails:
            emails[wid] = row["email"]
            onboarding[wid] = row["onboarding_completed_at"] is not None
    return emails, onboarding
