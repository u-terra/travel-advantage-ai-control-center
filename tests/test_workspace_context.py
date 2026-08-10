from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.access import AllowlistMiddleware
from app.domain.partners import WorkspaceContext
from app.repositories.partner_repository import AmbiguousWorkspaceError
from app.workspace_context import WorkspaceContextMiddleware


def run(coro):
    return asyncio.run(coro)


def event(user_id: int):
    return SimpleNamespace(from_user=SimpleNamespace(id=user_id), answer=AsyncMock())


def context(user_id: int, workspace_id: int = 10) -> WorkspaceContext:
    return WorkspaceContext(user_id, workspace_id, "member", "active")


def test_middleware_resolves_once_and_exposes_update_scoped_context() -> None:
    repository = SimpleNamespace(resolve_workspace_context=AsyncMock(return_value=context(1)))
    middleware = WorkspaceContextMiddleware(repository)
    seen = []

    async def handler(_event, data):
        seen.append(data["workspace_context"])

    run(middleware(handler, event(1), {}))
    repository.resolve_workspace_context.assert_awaited_once_with(1)
    assert seen == [context(1)]


def test_no_workspace_context_continues_handler_pipeline() -> None:
    repository = SimpleNamespace(resolve_workspace_context=AsyncMock(return_value=None))
    middleware = WorkspaceContextMiddleware(repository)
    handler = AsyncMock(return_value="handled")
    data = {}

    result = run(middleware(handler, event(1), data))

    assert result == "handled"
    handler.assert_awaited_once()
    assert data["workspace_context"] is None
    assert data["workspace_context_ambiguous"] is False


def test_unexpected_repository_error_is_not_swallowed() -> None:
    error = RuntimeError("database unavailable")
    repository = SimpleNamespace(
        resolve_workspace_context=AsyncMock(side_effect=error)
    )
    middleware = WorkspaceContextMiddleware(repository)
    handler = AsyncMock()

    try:
        run(middleware(handler, event(1), {}))
    except RuntimeError as raised:
        assert raised is error
    else:
        raise AssertionError("Unexpected repository error was swallowed")
    handler.assert_not_awaited()


def test_context_does_not_leak_between_updates_and_ambiguous_is_fail_closed() -> None:
    repository = SimpleNamespace(
        resolve_workspace_context=AsyncMock(
            side_effect=[context(1), None, AmbiguousWorkspaceError()]
        )
    )
    middleware = WorkspaceContextMiddleware(repository)
    seen = []

    async def handler(_event, data):
        seen.append((data["workspace_context"], data["workspace_context_ambiguous"]))

    run(middleware(handler, event(1), {}))
    run(middleware(handler, event(2), {}))
    run(middleware(handler, event(3), {}))
    assert seen == [(context(1), False), (None, False), (None, True)]


def test_allowlist_runs_before_workspace_resolver() -> None:
    repository = SimpleNamespace(resolve_workspace_context=AsyncMock())
    workspace = WorkspaceContextMiddleware(repository)
    allowlist = AllowlistMiddleware(frozenset({1}))
    final = AsyncMock()

    async def workspace_handler(current_event, data):
        return await workspace(final, current_event, data)

    run(allowlist(workspace_handler, event(2), {}))
    repository.resolve_workspace_context.assert_not_awaited()
    final.assert_not_awaited()
