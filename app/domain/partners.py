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


@dataclass(frozen=True)
class UserConsent:
    id: int
    workspace_id: int
    telegram_user_id: int
    consent_version: str
    accepted_at: str


@dataclass(frozen=True)
class WorkspaceUserPreferences:
    """Stage 3B1: личный стиль КОНКРЕТНОГО пользователя внутри workspace —

    отдельно от BusinessProfile (стиль компании). Ключ — (workspace_id,
    telegram_user_id): один и тот же Telegram-пользователь в разных
    workspace получает независимые записи, и разные пользователи одного
    workspace не видят чужой стиль/примеры.
    """
    workspace_id: int
    telegram_user_id: int
    style_description: str
    example_posts: tuple[str, ...]
    avoid_phrases: tuple[str, ...]
    created_at: str
    updated_at: str
