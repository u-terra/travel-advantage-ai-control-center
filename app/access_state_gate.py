"""Централизованный gate рабочего доступа (Stage 3A / Unified Subscription).

Та же идея, что и app/onboarding_gate.py: один раз на update вычислить
access_state и положить готовое значение в workflow data, чтобы хендлеры не
повторяли одну и ту же логику. Router-уровневый catch-all в
app/handlers/lobby.py читает этот флаг через MagicData(...) и не даёт
обойти лобби ни одним другим хендлером, пока доступ не активен.

Источник access_state - SubscriptionRepository.resolve_access_state()
(app/repositories/subscription_repository.py): единственный источник
subscription state и для Telegram, и для Web (см. app/web_api.py) - оба
канала вызывают тот же метод, поэтому оба всегда видят одно и то же
значение для одного workspace. partner_workspaces.access_status/
access_expires_at (app/domain/partners.py) больше не читаются здесь -
deprecated, см. их докстринг.

Важно: это НЕ то же самое, что AllowlistMiddleware (app/access.py).
AllowlistMiddleware больше не блокирует вход - публичный слой (/start,
"Осмотреться") доступен любому Telegram-пользователю. Access state решает
только одно: показывать рабочий Оркестратор или лобби.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.repositories.subscription_repository import SubscriptionRepository
from app.services.access_state import NO_WORKSPACE


class AccessStateMiddleware(BaseMiddleware):
    def __init__(self, subscription_repository: SubscriptionRepository) -> None:
        self.subscription_repository = subscription_repository

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        workspace_context = data.get("workspace_context")
        state = NO_WORKSPACE
        if workspace_context is not None:
            state = await self.subscription_repository.resolve_access_state(
                workspace_context.workspace_id,
            )
        data["access_state"] = state
        return await handler(event, data)
