"""Stage 3B1: доступ к личному стилю КОНКРЕТНОГО пользователя.

Отдельно от BusinessProfileService (тот — про стиль компании/workspace,
доступен только owner/admin на запись). Личный стиль — данные самого
пользователя: читает и пишет любой активный участник workspace (включая
role='member'), но только СВОИ собственные — workspace_context.
telegram_user_id используется как ключ во всех вызовах репозитория, поэтому
подменить чужой telegram_user_id через этот сервис структурно невозможно.
"""

from __future__ import annotations

from app.domain.partners import WorkspaceContext, WorkspaceUserPreferences
from app.repositories.partner_repository import PartnerRepository


class UserStyleAccessError(PermissionError):
    pass


class UserStyleService:
    def __init__(self, repository: PartnerRepository) -> None:
        self.repository = repository

    async def get(
        self, workspace_context: WorkspaceContext | None
    ) -> WorkspaceUserPreferences | None:
        _require_access(workspace_context)
        return await self.repository.get_user_preferences(
            workspace_context.workspace_id, workspace_context.telegram_user_id
        )

    async def set_style_description(
        self, workspace_context: WorkspaceContext | None, style_description: str
    ) -> WorkspaceUserPreferences:
        _require_access(workspace_context)
        return await self.repository.set_user_style_description(
            workspace_context.workspace_id, workspace_context.telegram_user_id,
            style_description,
        )

    async def add_example_post(
        self, workspace_context: WorkspaceContext | None, text: str
    ) -> WorkspaceUserPreferences:
        _require_access(workspace_context)
        return await self.repository.add_user_example_post(
            workspace_context.workspace_id, workspace_context.telegram_user_id, text,
        )

    async def clear_example_posts(
        self, workspace_context: WorkspaceContext | None
    ) -> WorkspaceUserPreferences:
        _require_access(workspace_context)
        return await self.repository.clear_user_example_posts(
            workspace_context.workspace_id, workspace_context.telegram_user_id,
        )

    async def set_avoid_phrases(
        self, workspace_context: WorkspaceContext | None, phrases: list[str]
    ) -> WorkspaceUserPreferences:
        _require_access(workspace_context)
        return await self.repository.set_user_avoid_phrases(
            workspace_context.workspace_id, workspace_context.telegram_user_id, phrases,
        )

    async def set_voice_sample(
        self, workspace_context: WorkspaceContext | None, voice_sample: str
    ) -> WorkspaceUserPreferences:
        """"Мой стиль / Голос бренда". Empty string clears the sample -
        same "Очистить стиль" path as setting it, no separate method."""
        _require_access(workspace_context)
        return await self.repository.set_user_voice_sample(
            workspace_context.workspace_id, workspace_context.telegram_user_id, voice_sample,
        )


def _require_access(context: WorkspaceContext | None) -> None:
    if context is None or context.workspace_status != "active":
        raise UserStyleAccessError("Личный стиль недоступен")
