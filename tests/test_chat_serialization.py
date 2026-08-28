from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.domain.partners import WorkspaceContext
from app.services.chat_serialization import ChatSerializationMiddleware

WORKSPACE_A = 1
WORKSPACE_B = 2
USER_A = 586249067
USER_B = 111222333


def _ctx(workspace_id: int, telegram_user_id: int) -> WorkspaceContext:
    return WorkspaceContext(
        telegram_user_id=telegram_user_id,
        workspace_id=workspace_id,
        role="owner",
        workspace_status="active",
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ── A: same (workspace, user) - strictly serialized ─────────────────────


def test_second_update_for_same_user_waits_for_first_to_finish() -> None:
    async def scenario() -> list[str]:
        middleware = ChatSerializationMiddleware()
        order: list[str] = []
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        async def handler_first(event: Any, data: dict[str, Any]) -> str:
            order.append("first_start")
            first_started.set()
            await release_first.wait()
            order.append("first_end")
            return "first"

        async def handler_second(event: Any, data: dict[str, Any]) -> str:
            order.append("second_start")
            order.append("second_end")
            return "second"

        ctx = _ctx(WORKSPACE_A, USER_A)
        task_first = asyncio.create_task(
            middleware(handler_first, object(), {"workspace_context": ctx})
        )
        await first_started.wait()

        # handler_first is guaranteed to still be holding the lock here (it
        # is parked on release_first.wait()), so task_second's
        # lock.acquire() cannot complete no matter how the loop schedules it
        # - no sleep/timing dependency needed to make this deterministic.
        task_second = asyncio.create_task(
            middleware(handler_second, object(), {"workspace_context": ctx})
        )
        release_first.set()

        results = await asyncio.wait_for(
            asyncio.gather(task_first, task_second), timeout=2.0
        )
        assert results == ["first", "second"]
        return order

    order = _run(scenario())
    assert order == ["first_start", "first_end", "second_start", "second_end"]


# ── B: different users - never block each other ─────────────────────────


def test_different_users_are_not_serialized() -> None:
    async def scenario() -> tuple[str, str]:
        middleware = ChatSerializationMiddleware()
        a_entered = asyncio.Event()
        b_entered = asyncio.Event()

        async def handler_a(event: Any, data: dict[str, Any]) -> str:
            a_entered.set()
            # If this middleware wrongly serialized different users, handler_b
            # could never reach b_entered.set() while handler_a holds a lock
            # that blocks it - this would deadlock and the outer wait_for
            # would fail the test instead of hanging forever.
            await asyncio.wait_for(b_entered.wait(), timeout=1.0)
            return "a"

        async def handler_b(event: Any, data: dict[str, Any]) -> str:
            b_entered.set()
            await asyncio.wait_for(a_entered.wait(), timeout=1.0)
            return "b"

        task_a = asyncio.create_task(
            middleware(handler_a, object(), {"workspace_context": _ctx(WORKSPACE_A, USER_A)})
        )
        task_b = asyncio.create_task(
            middleware(handler_b, object(), {"workspace_context": _ctx(WORKSPACE_A, USER_B)})
        )
        return await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=2.0)

    result_a, result_b = _run(scenario())
    assert (result_a, result_b) == ("a", "b")


# ── C: same telegram_user_id, different workspace - never block each other ──


def test_same_user_in_different_workspaces_is_not_serialized() -> None:
    async def scenario() -> tuple[str, str]:
        middleware = ChatSerializationMiddleware()
        first_entered = asyncio.Event()
        second_entered = asyncio.Event()

        async def handler_first(event: Any, data: dict[str, Any]) -> str:
            first_entered.set()
            await asyncio.wait_for(second_entered.wait(), timeout=1.0)
            return "first"

        async def handler_second(event: Any, data: dict[str, Any]) -> str:
            second_entered.set()
            await asyncio.wait_for(first_entered.wait(), timeout=1.0)
            return "second"

        task_first = asyncio.create_task(
            middleware(
                handler_first, object(), {"workspace_context": _ctx(WORKSPACE_A, USER_A)}
            )
        )
        task_second = asyncio.create_task(
            middleware(
                handler_second, object(), {"workspace_context": _ctx(WORKSPACE_B, USER_A)}
            )
        )
        return await asyncio.wait_for(
            asyncio.gather(task_first, task_second), timeout=2.0
        )

    result_first, result_second = _run(scenario())
    assert (result_first, result_second) == ("first", "second")


# ── D: exception in first handler releases the lock ─────────────────────


def test_exception_in_handler_still_releases_the_lock() -> None:
    async def scenario() -> str:
        middleware = ChatSerializationMiddleware()
        ctx = _ctx(WORKSPACE_A, USER_A)

        async def handler_fail(event: Any, data: dict[str, Any]) -> None:
            raise RuntimeError("boom")

        async def handler_ok(event: Any, data: dict[str, Any]) -> str:
            return "ok"

        with pytest.raises(RuntimeError, match="boom"):
            await middleware(handler_fail, object(), {"workspace_context": ctx})

        # If the lock were left held after the exception, this would hang -
        # wait_for turns that into a fast, clear failure instead.
        return await asyncio.wait_for(
            middleware(handler_ok, object(), {"workspace_context": ctx}), timeout=1.0
        )

    assert _run(scenario()) == "ok"


# ── E: cancellation does not leave the lock held forever ────────────────


def test_cancelling_the_lock_holder_releases_the_lock() -> None:
    async def scenario() -> str:
        middleware = ChatSerializationMiddleware()
        ctx = _ctx(WORKSPACE_A, USER_A)
        started = asyncio.Event()
        never = asyncio.Event()

        async def handler_hang(event: Any, data: dict[str, Any]) -> None:
            started.set()
            await never.wait()  # never completes on its own - only cancellation ends this

        async def handler_ok(event: Any, data: dict[str, Any]) -> str:
            return "ok"

        task = asyncio.create_task(
            middleware(handler_hang, object(), {"workspace_context": ctx})
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        return await asyncio.wait_for(
            middleware(handler_ok, object(), {"workspace_context": ctx}), timeout=1.0
        )

    assert _run(scenario()) == "ok"


def test_cancelling_a_waiter_does_not_corrupt_the_lock_for_others() -> None:
    async def scenario() -> tuple[bool, str, str]:
        middleware = ChatSerializationMiddleware()
        ctx = _ctx(WORKSPACE_A, USER_A)
        holder_started = asyncio.Event()
        release_holder = asyncio.Event()
        waiter_ran = asyncio.Event()

        async def handler_holder(event: Any, data: dict[str, Any]) -> str:
            holder_started.set()
            await release_holder.wait()
            return "holder"

        async def handler_waiter(event: Any, data: dict[str, Any]) -> str:
            waiter_ran.set()
            return "waiter"

        async def handler_ok(event: Any, data: dict[str, Any]) -> str:
            return "ok"

        task_holder = asyncio.create_task(
            middleware(handler_holder, object(), {"workspace_context": ctx})
        )
        await holder_started.wait()

        task_waiter = asyncio.create_task(
            middleware(handler_waiter, object(), {"workspace_context": ctx})
        )
        # One scheduling tick so task_waiter actually reaches lock.acquire()
        # and parks there - deterministic because handler_holder still holds
        # the lock the whole time regardless of how many ticks pass.
        await asyncio.sleep(0)
        task_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task_waiter

        release_holder.set()
        holder_result = await task_holder

        ok_result = await asyncio.wait_for(
            middleware(handler_ok, object(), {"workspace_context": ctx}), timeout=1.0
        )
        return waiter_ran.is_set(), holder_result, ok_result

    waiter_ran, holder_result, ok_result = _run(scenario())
    assert waiter_ran is False
    assert holder_result == "holder"
    assert ok_result == "ok"


# ── passthrough when there is no workspace_context ───────────────────────


def test_missing_workspace_context_is_a_passthrough_noop() -> None:
    async def scenario() -> str:
        middleware = ChatSerializationMiddleware()

        async def handler(event: Any, data: dict[str, Any]) -> str:
            return "ran"

        return await middleware(handler, object(), {"workspace_context": None})

    assert _run(scenario()) == "ran"


def test_missing_workspace_context_key_entirely_is_a_passthrough_noop() -> None:
    async def scenario() -> str:
        middleware = ChatSerializationMiddleware()

        async def handler(event: Any, data: dict[str, Any]) -> str:
            return "ran"

        return await middleware(handler, object(), {})

    assert _run(scenario()) == "ran"
