"""Централизованный gate обязательного Business Onboarding.

Одна ответственность: один раз на update вычислить, требуется ли онбординг
для текущего workspace, и положить готовый флаг (+ уже прочитанный профиль,
чтобы хендлерам не делать повторный запрос) в workflow data — по тому же
принципу, что WorkspaceContextMiddleware кладёт туда workspace_context.

Router-уровневый catch-all в app/handlers/onboarding.py читает этот флаг
через MagicData(F.onboarding_required) и не даёт обойти онбординг ни одним
другим хендлером — вместо повторения одной и той же проверки в каждом
handler'е меню/задач.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.repositories.partner_repository import PartnerRepository
from app.services.business_profile_context import is_onboarding_required


class OnboardingGateMiddleware(BaseMiddleware):
    def __init__(self, repository: PartnerRepository, rollout_at: datetime | None) -> None:
        self.repository = repository
        self.rollout_at = rollout_at

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        workspace_context = data.get("workspace_context")
        required = False
        profile = None
        if workspace_context is not None:
            workspace = await self.repository.get_workspace(workspace_context.workspace_id)
            profile = await self.repository.get_business_profile(workspace_context.workspace_id)
            if workspace is not None:
                required = is_onboarding_required(
                    profile_status=profile.profile_status if profile is not None else None,
                    workspace_created_at=workspace.created_at,
                    rollout_at=self.rollout_at,
                )
        data["onboarding_required"] = required
        data["onboarding_profile"] = profile
        return await handler(event, data)
