"""Beta Control Center only - cross-tenant workspace directory rows. See
app/repositories/admin_directory_repository.py for the one repository
allowed to read across tenants like this."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkspaceDirectoryRow:
    workspace_id: int
    name: str
    slug: str
    status: str
    created_at: str
    business_name: str | None
    ta_affiliated: bool
    subscription_status: str | None
    plan: str | None
    paid_until: str | None
    trial_until: str | None
    primary_email: str | None
    onboarding_completed: bool | None


@dataclass(frozen=True)
class WorkspaceMemberRow:
    telegram_user_id: int
    role: str
    status: str
    email: str | None
    onboarding_completed: bool | None
