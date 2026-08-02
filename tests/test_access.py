from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiogram.types import CallbackQuery, Message

from app.access import (
    ACCESS_DENIED_MESSAGE,
    AllowlistMiddleware,
    parse_allowed_user_ids,
)

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


# --- allowed user -----------------------------------------------------------

def test_allowed_user_reaches_handler() -> None:
    mw = AllowlistMiddleware(frozenset({OWNER_ID}))
    handler = AsyncMock(return_value="handled")
    msg = _message(OWNER_ID)

    result = _run(mw(handler, msg, {}))

    assert result == "handled"
    handler.assert_awaited_once_with(msg, {})
    msg.answer.assert_not_called()


# --- stranger blocked -------------------------------------------------------

def test_stranger_message_denied() -> None:
    mw = AllowlistMiddleware(frozenset({OWNER_ID}))
    handler = AsyncMock()
    msg = _message(STRANGER_ID)

    result = _run(mw(handler, msg, {}))

    assert result is None
    handler.assert_not_called()
    msg.answer.assert_awaited_once()
    assert msg.answer.await_args.args[0] == ACCESS_DENIED_MESSAGE


def test_stranger_callback_denied() -> None:
    mw = AllowlistMiddleware(frozenset({OWNER_ID}))
    handler = AsyncMock()
    cb = _callback(STRANGER_ID)

    result = _run(mw(handler, cb, {}))

    assert result is None
    handler.assert_not_called()
    cb.answer.assert_awaited_once()
    assert cb.answer.await_args.args[0] == ACCESS_DENIED_MESSAGE
    # Ответ — всплывающий алерт, без раскрытия содержимого панели.
    assert cb.answer.await_args.kwargs.get("show_alert") is True


# --- empty / missing allowlist closes access --------------------------------

def test_empty_allowlist_denies_owner_and_stranger() -> None:
    mw = AllowlistMiddleware(frozenset())
    handler = AsyncMock()

    owner_msg = _message(OWNER_ID)
    assert _run(mw(handler, owner_msg, {})) is None

    stranger_cb = _callback(STRANGER_ID)
    assert _run(mw(handler, stranger_cb, {})) is None

    handler.assert_not_called()
    owner_msg.answer.assert_awaited_once()
    stranger_cb.answer.assert_awaited_once()


def test_missing_from_user_denied() -> None:
    mw = AllowlistMiddleware(frozenset({OWNER_ID}))
    handler = AsyncMock()
    msg = _message(None)

    result = _run(mw(handler, msg, {}))

    assert result is None
    handler.assert_not_called()
