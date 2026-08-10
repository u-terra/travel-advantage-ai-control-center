from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.repositories.partner_repository import (
    AmbiguousWorkspaceError,
    PartnerRepository,
)


class WorkspaceContextMiddleware(BaseMiddleware):
    """Resolve workspace authorization once and expose it as update-scoped data."""

    def __init__(self, repository: PartnerRepository) -> None:
        self.repository = repository

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        user_id = getattr(user, "id", None)
        context = None
        ambiguous = False
        if user_id is not None:
            try:
                context = await self.repository.resolve_workspace_context(user_id)
            except AmbiguousWorkspaceError:
                ambiguous = True
        data["workspace_context"] = context
        data["workspace_context_ambiguous"] = ambiguous
        return await handler(event, data)
