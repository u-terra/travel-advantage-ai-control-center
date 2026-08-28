from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiogram.types import CallbackQuery, Message

from app.access import AllowlistMiddleware, parse_allowed_user_ids

OWNER_ID = 586249067
STRANGER_ID = 111222333


def _run(coro: Any) -> Any:
    # Проект не подключает pytest-asyncio, поэтому корутины запускаем напрямую.
    return asyncio.run(coro)


def _message(user_id: int | None) -> MagicMock:
    msg = MagicMock(spec=Message)
    msg.from_user = MagicMock(id=user_id) if user_id is not None else None
    msg.answer = AsyncMock()
    return msg


def _callback(user_id: int | None) -> MagicMock:
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = MagicMock(id=user_id) if user_id is not None else None
    cb.answer = AsyncMock()
    return cb


# --- parse_allowed_user_ids -------------------------------------------------

def test_parse_single_id() -> None:
    assert parse_allowed_user_ids("586249067") == frozenset({586249067})


def test_parse_multiple_ids_with_separators() -> None:
    assert parse_allowed_user_ids("1, 2 ;3") == frozenset({1, 2, 3})


def test_parse_missing_is_empty() -> None:
    assert parse_allowed_user_ids(None) == frozenset()
    assert parse_allowed_user_ids("") == frozenset()


def test_parse_ignores_non_numeric() -> None:
    assert parse_allowed_user_ids("586249067, abc, ") == frozenset({586249067})


# --- Stage 3A: allowlist больше никого не блокирует -------------------------
# (публичный слой /start и лобби обязан быть доступен любому пользователю).


def test_allowed_user_reaches_handler_and_is_flagged() -> None:
    mw = AllowlistMiddleware(frozenset({OWNER_ID}))
    handler = AsyncMock(return_value="handled")
    msg = _message(OWNER_ID)

    result = _run(mw(handler, msg, {}))

    assert result == "handled"
    handler.assert_awaited_once_with(msg, {"is_allowlisted": True})
    msg.answer.assert_not_called()


def test_stranger_message_still_reaches_handler() -> None:
    mw = AllowlistMiddleware(frozenset({OWNER_ID}))
    handler = AsyncMock(return_value="handled")
    msg = _message(STRANGER_ID)

    result = _run(mw(handler, msg, {}))

    assert result == "handled"
    handler.assert_awaited_once_with(msg, {"is_allowlisted": False})
    # Никакого автоматического ответа постороннему — публичный слой решает
    # дальше сам (access_state/лобби), а не этот guard.
    msg.answer.assert_not_called()


def test_stranger_callback_still_reaches_handler() -> None:
    mw = AllowlistMiddleware(frozenset({OWNER_ID}))
    handler = AsyncMock(return_value="handled")
    cb = _callback(STRANGER_ID)

    result = _run(mw(handler, cb, {}))

    assert result == "handled"
    handler.assert_awaited_once_with(cb, {"is_allowlisted": False})
    cb.answer.assert_not_called()


def test_empty_allowlist_no_longer_blocks_owner_or_stranger() -> None:
    mw = AllowlistMiddleware(frozenset())
    handler = AsyncMock(return_value="handled")

    owner_msg = _message(OWNER_ID)
    assert _run(mw(handler, owner_msg, {})) == "handled"

    stranger_cb = _callback(STRANGER_ID)
    assert _run(mw(handler, stranger_cb, {})) == "handled"

    assert handler.await_count == 2
    owner_msg.answer.assert_not_called()
    stranger_cb.answer.assert_not_called()


def test_missing_from_user_reaches_handler_as_not_allowlisted() -> None:
    mw = AllowlistMiddleware(frozenset({OWNER_ID}))
    handler = AsyncMock(return_value="handled")
    msg = _message(None)

    result = _run(mw(handler, msg, {}))

    assert result == "handled"
    handler.assert_awaited_once_with(msg, {"is_allowlisted": False})


def test_is_allowed_still_works_as_explicit_check() -> None:
    """is_allowed()/allowed_user_ids остаются доступны для явного,
    изолированного legacy/admin/pilot-использования — сам middleware их
    больше не использует для блокировки."""
    mw = AllowlistMiddleware(frozenset({OWNER_ID}))
    assert mw.is_allowed(OWNER_ID) is True
    assert mw.is_allowed(STRANGER_ID) is False
    assert mw.is_allowed(None) is False
