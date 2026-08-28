"""Централизованный gate рабочего доступа (Stage 3A).

Та же идея, что и app/onboarding_gate.py: один раз на update вычислить
access_state и положить готовое значение в workflow data, чтобы хендлеры не
повторяли одну и ту же логику. Router-уровневый catch-all в
app/handlers/lobby.py читает этот флаг через MagicData(...) и не даёт
обойти лобби ни одним другим хендлером, пока доступ не активен.

Важно: это НЕ то же самое, что AllowlistMiddleware (app/access.py).
AllowlistMiddleware больше не блокирует вход — публичный слой (/start,
"Осмотреться") доступен любому Telegram-пользователю. Access state решает
только одно: показывать рабочий Оркестратор или лобби.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.repositories.partner_repository import PartnerRepository
from app.services.access_state import NO_WORKSPACE, compute_access_state


class AccessStateMiddleware(BaseMiddleware):
    def __init__(self, repository: PartnerRepository) -> None:
        self.repository = repository

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        workspace_context = data.get("workspace_context")
        state = NO_WORKSPACE
        if workspace_context is not None:
            workspace = await self.repository.get_workspace(workspace_context.workspace_id)
            if workspace is not None:
                state = compute_access_state(
                    workspace.access_status, workspace.access_expires_at,
                )
        data["access_state"] = state
        return await handler(event, data)
