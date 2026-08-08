"""Интеграционные тесты: env → load_settings → guard, плюс сборка dispatcher.

Проверяют не только middleware в изоляции, но и итоговую конфигурацию (реальный
load_settings из окружения) и сборку dispatcher (_build_dispatcher регистрирует
guard на message и callback_query). Ключевая гарантия — fail-closed: доступ
управляется ТОЛЬКО через TELEGRAM_ALLOWED_USER_IDS.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message

from app.access import ACCESS_DENIED_MESSAGE, AllowlistMiddleware
from app.config import load_settings
from app.main import _build_dispatcher

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


async def _reaches_handler(guard: AllowlistMiddleware, event: MagicMock) -> bool:
    handler = AsyncMock(return_value="handled")
    result = await guard(handler, event, {})
    if handler.await_count:
        return True
    # Отказ: хендлер не вызван, отправлен единственный ответ с нужным текстом.
    event.answer.assert_awaited_once()
    assert event.answer.await_args.args[0] == ACCESS_DENIED_MESSAGE
    assert result is None
    return False


# 1. TELEGRAM_ALLOWED_USER_IDS отсутствует, ADMIN_TELEGRAM_ID задан — доступ закрыт.
def test_missing_allowlist_closes_access_even_with_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = _guard_from_env(monkeypatch, allowed=None)
    assert guard.allowed_user_ids == frozenset()
    assert _run(_reaches_handler(guard, _message(OWNER_ID))) is False
    assert _run(_reaches_handler(guard, _message(STRANGER_ID))) is False


# 2. TELEGRAM_ALLOWED_USER_IDS пуст — доступ закрыт.
def test_empty_allowlist_closes_access(monkeypatch: pytest.MonkeyPatch) -> None:
    guard = _guard_from_env(monkeypatch, allowed="   ")
    assert guard.allowed_user_ids == frozenset()
    assert _run(_reaches_handler(guard, _message(OWNER_ID))) is False
    assert _run(_reaches_handler(guard, _callback(OWNER_ID))) is False


# 3. TELEGRAM_ALLOWED_USER_IDS=586249067 — пользователь 586249067 разрешён.
def test_allowlisted_user_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    guard = _guard_from_env(monkeypatch, allowed=str(OWNER_ID))
    assert guard.allowed_user_ids == frozenset({OWNER_ID})
    assert _run(_reaches_handler(guard, _message(OWNER_ID))) is True
    assert _run(_reaches_handler(guard, _callback(OWNER_ID))) is True


# 4. Посторонний message заблокирован.
def test_stranger_message_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    guard = _guard_from_env(monkeypatch, allowed=str(OWNER_ID))
    assert _run(_reaches_handler(guard, _message(STRANGER_ID))) is False


# 5. Посторонний callback заблокирован.
def test_stranger_callback_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    guard = _guard_from_env(monkeypatch, allowed=str(OWNER_ID))
    cb = _callback(STRANGER_ID)
    assert _run(_reaches_handler(guard, cb)) is False
    assert cb.answer.await_args.kwargs.get("show_alert") is True


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
