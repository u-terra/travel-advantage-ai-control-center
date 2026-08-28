from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

from aiogram.types import ReplyKeyboardRemove

from app.cards import build_card
from app.domain.partners import PartnerWorkspace, WorkspaceContext
from app.handlers.lobby import (
    WELCOME_EXPIRED_TEXT,
    WELCOME_NEW_TEXT,
    WORKSPACE_AMBIGUOUS_TEXT,
)
from app.handlers.start import (
    HELP_TEXT,
    WORKSPACE_UNAVAILABLE_TEXT,
    cmd_help,
    cmd_start,
    how_it_works,
)
from app.keyboards import (
    BTN_LOBBY_BROWSE,
    BTN_LOBBY_EXTEND_ACCESS,
    BTN_LOBBY_SUBSCRIBE_MONTH,
    BTN_LOBBY_TRY_14_DAYS,
    BTN_LOBBY_WHATS_INCLUDED,
    active_main_menu,
)
from app.routing.router import route_text


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _texts(markup: Any) -> list[str]:
    return [button.text for row in markup.keyboard for button in row]


class _Message:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.answers: list[tuple[str, Any]] = []

    async def answer(self, text: str, reply_markup: Any = None, **kwargs: Any) -> None:
        self.answers.append((text, reply_markup))


class _State:
    def __init__(self) -> None:
        self.clear_calls = 0

    async def clear(self) -> None:
        self.clear_calls += 1


def _workspace_context(workspace_id: int = 42) -> WorkspaceContext:
    return WorkspaceContext(100, workspace_id, "member", "active")


def _repository(workspace: PartnerWorkspace | None) -> Any:
    return AsyncMock(get_workspace=AsyncMock(return_value=workspace))


# --- workspace_context есть + access_state active/trial_active: как раньше --

def test_start_with_workspace_shows_workspace_name() -> None:
    message = _Message()
    repository = _repository(
        PartnerWorkspace(42, "Acme Travel Club", "acme", "active", "now", "now")
    )

    _run(cmd_start(
        message, _State(),
        workspace_context=_workspace_context(42),
        partner_repository=repository,
    ))

    text, markup = message.answers[0]
    assert "Acme Travel Club" in text
    repository.get_workspace.assert_awaited_once_with(42)
    assert not isinstance(markup, ReplyKeyboardRemove)
    assert _texts(markup) == _texts(active_main_menu(False))


def test_start_does_not_reveal_workspace_id() -> None:
    message = _Message()
    repository = _repository(
        PartnerWorkspace(4242, "Acme Travel Club", "acme", "active", "now", "now")
    )

    _run(cmd_start(
        message, _State(),
        workspace_context=_workspace_context(4242),
        partner_repository=repository,
    ))

    text = message.answers[0][0]
    assert "4242" not in text


def test_start_falls_back_when_workspace_lookup_is_inconsistent() -> None:
    """Fail closed: контекст есть, доступ active, но сам workspace не
    нашёлся — не выдумывать имя."""
    message = _Message()
    repository = _repository(None)

    _run(cmd_start(
        message, _State(),
        workspace_context=_workspace_context(42),
        partner_repository=repository,
    ))

    text, markup = message.answers[0]
    assert text == WORKSPACE_UNAVAILABLE_TEXT
    assert isinstance(markup, ReplyKeyboardRemove)


def test_start_selects_menu_from_flag_when_workspace_is_resolved() -> None:
    for v2_menu_enabled in (False, True):
        message = _Message()
        repository = _repository(
            PartnerWorkspace(42, "Acme Travel Club", "acme", "active", "now", "now")
        )

        _run(cmd_start(
            message, _State(),
            v2_menu_enabled=v2_menu_enabled,
            workspace_context=_workspace_context(42),
            partner_repository=repository,
        ))

        markup = message.answers[0][1]
        assert _texts(markup) == _texts(active_main_menu(v2_menu_enabled))


def test_start_trial_active_workspace_gets_normal_menu_too() -> None:
    message = _Message()
    repository = _repository(
        PartnerWorkspace(42, "Acme Travel Club", "acme", "active", "now", "now")
    )

    _run(cmd_start(
        message, _State(),
        workspace_context=_workspace_context(42),
        partner_repository=repository,
        access_state="trial_active",
    ))

    text, markup = message.answers[0]
    assert "Acme Travel Club" in text
    assert not isinstance(markup, ReplyKeyboardRemove)


# --- Stage 3A: нет workspace / доступ истёк / приостановлен → лобби ---------

def test_start_new_visitor_without_workspace_sees_lobby_not_admin_message() -> None:
    """Ключевое изменение Stage 3A: новый посетитель без workspace получает
    лобби, а не "обратитесь к администратору"."""
    message = _Message()

    _run(cmd_start(message, _State()))

    text, markup = message.answers[0]
    assert text == WELCOME_NEW_TEXT
    assert text != WORKSPACE_UNAVAILABLE_TEXT
    assert "администратор" not in text.lower()
    assert not isinstance(markup, ReplyKeyboardRemove)
    assert _texts(markup) == [
        BTN_LOBBY_BROWSE, BTN_LOBBY_TRY_14_DAYS, BTN_LOBBY_SUBSCRIBE_MONTH,
        BTN_LOBBY_WHATS_INCLUDED,
    ]


def test_start_new_visitor_does_not_create_anything() -> None:
    """/start нового пользователя не должен трогать partner_repository —
    workspace не создаётся автоматически (см. Stage 3A решение п.3)."""
    message = _Message()
    repository = AsyncMock()

    _run(cmd_start(message, _State(), partner_repository=repository))

    repository.get_workspace.assert_not_called()
    repository.provision_partner.assert_not_called()
    repository.create_membership.assert_not_called()
    repository.create_business_profile.assert_not_called()


def test_start_ambiguous_workspace_gets_distinct_message_and_hides_menu() -> None:
    message = _Message()

    _run(cmd_start(
        message, _State(),
        workspace_context=None,
        workspace_context_ambiguous=True,
    ))

    text, markup = message.answers[0]
    assert text == WORKSPACE_AMBIGUOUS_TEXT
    assert text != WELCOME_NEW_TEXT
    assert "несколько рабочих пространств" in text
    assert isinstance(markup, ReplyKeyboardRemove)


def test_start_missing_and_ambiguous_workspace_do_not_leak_internal_details() -> None:
    for ambiguous in (False, True):
        message = _Message()
        _run(cmd_start(
            message, _State(),
            workspace_context=None,
            workspace_context_ambiguous=ambiguous,
        ))
        text = message.answers[0][0].lower()
        for forbidden in ("workspace_id", "sqlite", "membership", "id "):
            assert forbidden not in text


def test_start_expired_workspace_sees_expired_lobby() -> None:
    message = _Message()

    _run(cmd_start(
        message, _State(),
        workspace_context=_workspace_context(42),
        access_state="expired",
    ))

    text, markup = message.answers[0]
    assert text == WELCOME_EXPIRED_TEXT
    assert _texts(markup) == [
        BTN_LOBBY_BROWSE, BTN_LOBBY_EXTEND_ACCESS, BTN_LOBBY_WHATS_INCLUDED,
    ]


def test_start_suspended_workspace_sees_plain_block_message() -> None:
    message = _Message()

    _run(cmd_start(
        message, _State(),
        workspace_context=_workspace_context(42),
        access_state="suspended",
    ))

    text, markup = message.answers[0]
    assert "приостановлен" in text.lower()
    assert isinstance(markup, ReplyKeyboardRemove)


def test_start_expired_and_suspended_do_not_touch_repository() -> None:
    """Продление/оплата — отдельные явные заглушки, /start сам по себе
    ничего не создаёт и не меняет для expired/suspended workspace."""
    for state in ("expired", "suspended"):
        message = _Message()
        repository = AsyncMock()
        _run(cmd_start(
            message, _State(),
            workspace_context=_workspace_context(42),
            partner_repository=repository,
            access_state=state,
        ))
        repository.get_workspace.assert_not_called()


def test_start_without_workspace_kwargs_does_not_crash_and_shows_lobby() -> None:
    """Обратная совместимость: старые вызовы без workspace-параметров не
    падают и теперь показывают лобби (а не ReplyKeyboardRemove-заглушку)."""
    message = _Message()
    state = _State()

    _run(cmd_start(message, state, v2_menu_enabled=True))

    assert message.answers[0][0] == WELCOME_NEW_TEXT
    assert state.clear_calls == 1


def test_help_texts_are_neutral_and_do_not_name_the_owner() -> None:
    assert "Владимир" not in HELP_TEXT
    assert "Travel AI Orchestrator" in HELP_TEXT

    help_message = _Message()
    _run(cmd_help(help_message))
    assert "Владимир" not in help_message.answers[0][0]

    how_message = _Message()
    _run(how_it_works(how_message))
    assert "Владимир" not in how_message.answers[0][0]


def test_route_card_does_not_name_the_owner() -> None:
    decision = route_text("Нужен пост о путешествиях")
    card = build_card(decision)
    assert "Владимир" not in card
    assert "Ваше решение:" in card
