"""Per-(workspace_id, telegram_user_id) update serialization (F1).

aiogram processes incoming updates concurrently by default: ``Dispatcher.
start_polling`` uses ``handle_as_tasks=True`` (the default, not overridden in
app.main), so every update - regardless of chat/user - gets its own
``asyncio.create_task`` with no per-chat/per-user lock anywhere in aiogram
itself (verified against the installed aiogram==3.13.1 source, not assumed).
Two fast consecutive messages from the same user can therefore be picked up
and handled out of order or genuinely in parallel by two live tasks.

This middleware closes that gap for exactly the scope F1 needs: the same
(workspace_id, telegram_user_id) is serialized; different users, and the
same telegram_user_id in a different workspace, never block each other.

Registration point: an outer_middleware on both dp.message/dp.callback_query,
placed immediately AFTER WorkspaceContextMiddleware (it needs
data["workspace_context"] already resolved) and before AccessStateMiddleware/
OnboardingGateMiddleware/the router - so the entire remaining pipeline for
one update finishes before the next update for the same user begins. If
workspace_context is missing (unresolved user, or partner_repository not
configured at all) this is a no-op passthrough - there is no key to lock on,
and F1 must not change behavior for that case.

Lock lifetime / cleanup: locks are created lazily, one per
(workspace_id, telegram_user_id), and kept for the life of the process -
never pruned. This is a deliberate simplification, not an oversight: the key
space is bounded by TELEGRAM_ALLOWED_USER_IDS (a small, manually managed
allowlist - low single/double digits for this deployment), so the dict can
never grow unbounded, and an idle asyncio.Lock costs a handful of bytes. A
refcounted eviction scheme would add real concurrency-bug surface (exactly
the class of subtle bug this project's review history keeps having to catch)
for a resource leak that cannot actually occur at this scale.

Lazy creation itself needs no extra guard mutex: ``dict.setdefault`` with a
plain, non-awaiting ``asyncio.Lock()`` default runs to completion within one
synchronous step of the cooperative event loop - no other task can observe
the dict mid-update, so two concurrent updates for the same key are
guaranteed to end up sharing the exact same Lock object.

Deadlock: this middleware wraps a whole update exactly once, at the outer
dispatcher level - it is never re-entered from inside a handler (nested
handler/service calls are plain function calls, not new dispatched updates),
so recursive acquisition of the same lock by the same logical update chain
is structurally impossible.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject


class ChatSerializationMiddleware(BaseMiddleware):
    def __init__(self) -> None:
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        context = data.get("workspace_context")
        if context is None:
            return await handler(event, data)
        key = (context.workspace_id, context.telegram_user_id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        # asyncio.Lock's own __aenter__/__aexit__ already release on any exit
        # path (normal return, raised exception, or cancellation delivered
        # while waiting) - no custom try/finally needed on top of it.
        async with lock:
            return await handler(event, data)
