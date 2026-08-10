from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PartnerWorkspace:
    id: int
    name: str
    slug: str
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PartnerProfile:
    id: int
    workspace_id: int
    telegram_user_id: int
    partner_name: str
    project_name: str
    business_description: str
    communication_style: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class WorkspaceMembership:
    id: int
    workspace_id: int
    telegram_user_id: int
    role: str
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class WorkspaceContext:
    telegram_user_id: int
    workspace_id: int
    role: str
    workspace_status: str
