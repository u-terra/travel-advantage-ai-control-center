from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

from app.domain.competitors import Competitor
from app.domain.partners import WorkspaceContext
from app.handlers.competitors import (
    AddCompetitor,
    _EMPTY,
    _SAVED,
    _UNAVAILABLE,
    cancel_add_competitor,
    receive_competitor_url,
    show_competitors,
    start_add_competitor,
)
from app.keyboards import BTN_V2_MAIN_MENU, COMPETITOR_REGISTRY_ADD
from app.repositories.competitor_repository import CompetitorAddressError


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _Message:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.answers: list[tuple[str, Any]] = []

    async def answer(self, text: str, reply_markup: Any = None, **kwargs: Any) -> None:
        self.answers.append((text, reply_markup))


class _Callback:
    def __init__(self) -> None:
        self.data = COMPETITOR_REGISTRY_ADD
        self.message = _Message()
        self.answered = False

    async def answer(self, *args: Any, **kwargs: Any) -> None:
        self.answered = True


class _State:
    def __init__(self) -> None:
        self.state: Any = None
        self.clear_calls = 0

    async def set_state(self, state: Any) -> None:
        self.state = state

    async def clear(self) -> None:
        self.clear_calls += 1
        self.state = None


def _context(workspace_id: int = 42) -> WorkspaceContext:
    return WorkspaceContext(100, workspace_id, "owner", "active")


def _competitor(
    competitor_id: int = 1, workspace_id: int = 42,
    url: str = "https://competitor.example.com",
) -> Competitor:
    return Competitor(competitor_id, workspace_id, url, url, "2026-08-01T10:00:00+00:00")


def _repository(*, competitors: list[Competitor] | None = None) -> Any:
    return AsyncMock(
        list_for_workspace=AsyncMock(return_value=competitors or []),
        add_competitor=AsyncMock(),
    )


def test_show_competitors_is_scoped_to_caller_workspace() -> None:
    message = _Message()
    state = _State()
    competitors = [_competitor(1, 42, "https://a.example.com")]
    repository = _repository(competitors=competitors)

    _run(show_competitors(message, state, repository, _context(42)))

    repository.list_for_workspace.assert_awaited_once_with(42, limit=20)
    text, markup = message.answers[0]
    assert "https://a.example.com" in text
    assert state.clear_calls == 1


def test_show_competitors_empty_shows_friendly_message_with_add_button() -> None:
    message = _Message()
    state = _State()
    repository = _repository(competitors=[])

    _run(show_competitors(message, state, repository, _context(42)))

    text, markup = message.answers[0]
    assert text == _EMPTY
    buttons = [button for row in markup.inline_keyboard for button in row]
    assert any(button.callback_data == COMPETITOR_REGISTRY_ADD for button in buttons)


def test_show_competitors_unavailable_without_workspace_context() -> None:
    message = _Message()
    state = _State()
    repository = _repository(competitors=[_competitor()])

    _run(show_competitors(message, state, repository, None))

    assert message.answers[0][0] == _UNAVAILABLE
    repository.list_for_workspace.assert_not_awaited()


def test_start_add_competitor_sets_waiting_state_and_prompts() -> None:
    callback = _Callback()
    state = _State()

    _run(start_add_competitor(callback, state))

    assert state.state == AddCompetitor.waiting_for_url
    assert callback.answered is True
    assert len(callback.message.answers) == 1


def test_receive_competitor_url_saves_for_caller_workspace() -> None:
    message = _Message("https://new-competitor.example.com")
    state = _State()
    repository = _repository()

    _run(receive_competitor_url(message, state, repository, _context(42)))

    repository.add_competitor.assert_awaited_once_with(
        42, "https://new-competitor.example.com"
    )
    assert message.answers[0][0] == _SAVED
    assert state.clear_calls == 1


def test_receive_competitor_url_unavailable_without_workspace_context() -> None:
    message = _Message("https://new-competitor.example.com")
    state = _State()
    repository = _repository()

    _run(receive_competitor_url(message, state, repository, None))

    assert message.answers[0][0] == _UNAVAILABLE
    repository.add_competitor.assert_not_awaited()


def test_receive_competitor_url_rejects_invalid_address_without_saving() -> None:
    message = _Message("not-a-url")
    state = _State()
    repository = _repository()
    repository.add_competitor.side_effect = CompetitorAddressError(
        "ссылка должна начинаться с http:// или https://"
    )

    _run(receive_competitor_url(message, state, repository, _context(42)))

    text, _ = message.answers[0]
    assert "Не получилось" in text
    assert state.clear_calls == 0


def test_cancel_add_competitor_returns_to_main_menu() -> None:
    message = _Message(BTN_V2_MAIN_MENU)
    state = _State()

    _run(cancel_add_competitor(message, state))

    assert state.clear_calls == 1
    assert message.answers[0][0].startswith("Главное меню")
