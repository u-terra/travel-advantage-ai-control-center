"""Интеграционные тесты: env → load_settings → guard, плюс сборка dispatcher.

Stage 3A: AllowlistMiddleware больше не блокирует доступ — публичный слой
(/start, лобби) обязан быть доступен любому Telegram-пользователю без
TELEGRAM_ALLOWED_USER_IDS. Guard остаётся зарегистрирован (порядок
middleware не меняется — AccessStateMiddleware/WorkspaceContextMiddleware
идут следом), но теперь только помечает is_allowlisted и всегда пропускает
обработку дальше.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message

from app.access import AllowlistMiddleware
from app.access_state_gate import AccessStateMiddleware
from app.config import load_settings
from app.main import _build_dispatcher
from app.workspace_context import WorkspaceContextMiddleware

OWNER_ID = 586249067
STRANGER_ID = 111222333


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _message(user_id: int) -> MagicMock:
    msg = MagicMock(spec=Message)
    msg.from_user = MagicMock(id=user_id)
    msg.answer = AsyncMock()
    return msg


def _callback(user_id: int) -> MagicMock:
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = MagicMock(id=user_id)
    cb.answer = AsyncMock()
    return cb


def _load_settings_from_env(
    monkeypatch: pytest.MonkeyPatch, allowed: str | None
) -> Any:
    """Реальная сборка конфигурации из окружения (изолированно от .env на диске)."""
    monkeypatch.setattr("app.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("BOT_TOKEN", "dummy-token")
    monkeypatch.setenv("ADMIN_TELEGRAM_ID", str(OWNER_ID))
    if allowed is None:
        monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)
    else:
        monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", allowed)
    return load_settings()


def _guard_from_env(
    monkeypatch: pytest.MonkeyPatch, allowed: str | None
) -> AllowlistMiddleware:
    """Guard, построенный из итоговой конфигурации (env → load_settings)."""
    settings = _load_settings_from_env(monkeypatch, allowed)
    return AllowlistMiddleware(settings.allowed_user_ids)


async def _is_allowlisted_after_pass_through(
    guard: AllowlistMiddleware, event: MagicMock
) -> bool:
    """Guard больше не блокирует: и allowlisted, и посторонний доходят до
    хендлера. Возвращает is_allowlisted, положенный guard'ом в data."""
    handler = AsyncMock(return_value="handled")
    result = await guard(handler, event, {})
    assert result == "handled"
    handler.assert_awaited_once()
    event.answer.assert_not_called()
    return handler.await_args.args[1]["is_allowlisted"]


# 1. TELEGRAM_ALLOWED_USER_IDS отсутствует, ADMIN_TELEGRAM_ID задан — публичный
#    слой всё равно доступен (is_allowlisted=False для всех).
def test_missing_allowlist_still_reaches_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = _guard_from_env(monkeypatch, allowed=None)
    assert guard.allowed_user_ids == frozenset()
    assert _run(_is_allowlisted_after_pass_through(guard, _message(OWNER_ID))) is False
    assert _run(_is_allowlisted_after_pass_through(guard, _message(STRANGER_ID))) is False


# 2. TELEGRAM_ALLOWED_USER_IDS пуст — тот же результат.
def test_empty_allowlist_still_reaches_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    guard = _guard_from_env(monkeypatch, allowed="   ")
    assert guard.allowed_user_ids == frozenset()
    assert _run(_is_allowlisted_after_pass_through(guard, _message(OWNER_ID))) is False
    assert _run(_is_allowlisted_after_pass_through(guard, _callback(OWNER_ID))) is False


# 3. TELEGRAM_ALLOWED_USER_IDS=586249067 — пользователь 586249067 помечен allowlisted.
def test_allowlisted_user_is_flagged_true(monkeypatch: pytest.MonkeyPatch) -> None:
    guard = _guard_from_env(monkeypatch, allowed=str(OWNER_ID))
    assert guard.allowed_user_ids == frozenset({OWNER_ID})
    assert _run(_is_allowlisted_after_pass_through(guard, _message(OWNER_ID))) is True
    assert _run(_is_allowlisted_after_pass_through(guard, _callback(OWNER_ID))) is True


# 4. Посторонний message доходит до хендлера, но помечен not allowlisted.
def test_stranger_message_reaches_handler_as_not_allowlisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = _guard_from_env(monkeypatch, allowed=str(OWNER_ID))
    assert _run(_is_allowlisted_after_pass_through(guard, _message(STRANGER_ID))) is False


# 5. Посторонний callback — аналогично, без show_alert-отказа.
def test_stranger_callback_reaches_handler_as_not_allowlisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = _guard_from_env(monkeypatch, allowed=str(OWNER_ID))
    cb = _callback(STRANGER_ID)
    assert _run(_is_allowlisted_after_pass_through(guard, cb)) is False
    cb.answer.assert_not_called()


# ADMIN_TELEGRAM_ID сохраняется в конфигурации (для прочих функций проекта),
# но НЕ попадает в allowlist автоматически.
def test_admin_id_kept_in_settings_but_not_in_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _load_settings_from_env(monkeypatch, allowed=None)
    assert settings.admin_telegram_id == OWNER_ID
    assert settings.allowed_user_ids == frozenset()


# Сборка dispatcher: guard из настроек регистрируется как outer-middleware
# и на message, и на callback_query (единый объект). Вызывается один раз, т.к.
# роутеры-синглтоны нельзя присоединить повторно.
def test_build_dispatcher_registers_guard_on_both_observers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _load_settings_from_env(monkeypatch, allowed=str(OWNER_ID))
    dp = _build_dispatcher(
        settings.allowed_user_ids,
        journal=None,
        llm_provider=None,
        lead_radar_config=None,
    )

    msg_guards = [
        m
        for m in dp.message.outer_middleware._middlewares
        if isinstance(m, AllowlistMiddleware)
    ]
    cb_guards = [
        m
        for m in dp.callback_query.outer_middleware._middlewares
        if isinstance(m, AllowlistMiddleware)
    ]
    assert len(msg_guards) == 1
    assert len(cb_guards) == 1
    assert msg_guards[0] is cb_guards[0]
    assert msg_guards[0].allowed_user_ids == frozenset({OWNER_ID})


def test_workspace_middleware_is_registered_after_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.main.build_router", lambda: __import__(
        "aiogram"
    ).Router())
    repository = MagicMock()
    subscription_repository = MagicMock()
    dp = _build_dispatcher(
        frozenset({OWNER_ID}), None, None, None,
        partner_repository=repository,
        subscription_repository=subscription_repository,
    )
    for observer in (dp.message, dp.callback_query):
        middlewares = observer.outer_middleware._middlewares
        guard_index = next(
            i for i, item in enumerate(middlewares)
            if isinstance(item, AllowlistMiddleware)
        )
        workspace_index = next(
            i for i, item in enumerate(middlewares)
            if isinstance(item, WorkspaceContextMiddleware)
        )
        access_state_index = next(
            i for i, item in enumerate(middlewares)
            if isinstance(item, AccessStateMiddleware)
        )
        assert guard_index < workspace_index
        # AccessStateMiddleware — центральный gate рабочего доступа (Stage
        # 3A), ему нужен уже готовый workspace_context.
        assert workspace_index < access_state_index
