"""Workspace-scoped Telegram source handlers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.domain.partners import WorkspaceContext
from app.domain.sources import WorkspaceSource
from app.repositories.source_catalog_repository import SubmitSourceRequestResult
from app.handlers.sources import (
    AddSource,
    receive_source_url,
    router,
    show_sources,
    start_add_source,
    toggle_source,
)
from app.keyboards import (
    BTN_V2_MAIN_MENU,
    BTN_V2_SOURCES,
    SOURCE_REGISTRY_ADD,
    SOURCE_TOGGLE_PREFIX,
    sources_registry_keyboard,
    v2_main_menu,
)
from app.services.source_registry_store import SourceAddressError, UnknownSourceError


def run(value):
    return asyncio.run(value)


def context(workspace_id: int = 42) -> WorkspaceContext:
    return WorkspaceContext(100, workspace_id, "owner", "active")


def source(source_id: str = "source-1", *, enabled: bool = True) -> WorkspaceSource:
    return WorkspaceSource(
        id=source_id, name="Источник", platform="web",
        source_type="monitored_source", purpose="mixed", enabled=enabled,
        usage_role="monitoring", url="https://example.com/source",
    )


class State:
    def __init__(self) -> None:
        self.state = None
        self.clear_calls = 0

    async def clear(self) -> None:
        self.state = None
        self.clear_calls += 1

    async def set_state(self, value) -> None:
        self.state = value


class Message:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.from_user = SimpleNamespace(id=100)
        self.answers: list[tuple[str, dict]] = []
        self.edits: list[tuple[str, dict]] = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class Callback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = Message()
        self.answers: list[tuple[str, dict]] = []

    async def answer(self, text: str = "", **kwargs):
        self.answers.append((text, kwargs))


def repository(items=()):
    return SimpleNamespace(
        list_for_workspace=AsyncMock(return_value=tuple(items)),
        toggle=AsyncMock(),
        submit_source_request=AsyncMock(),
    )


def _button_texts(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def _callback_data(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def test_list_passes_workspace_id_and_shows_state() -> None:
    repo = repository((source("on"), source("off", enabled=False)))
    message = Message(BTN_V2_SOURCES)
    run(show_sources(message, State(), repo, context(77)))

    repo.list_for_workspace.assert_awaited_once_with(77)
    text, kwargs = message.answers[0]
    assert "Всего: 2" in text and "активных: 1" in text
    assert "⚪️ Источник · выключен" in _button_texts(kwargs["reply_markup"])


def test_empty_list_keeps_add_action() -> None:
    message = Message(BTN_V2_SOURCES)
    run(show_sources(message, State(), repository(), context()))
    assert SOURCE_REGISTRY_ADD in _callback_data(message.answers[0][1]["reply_markup"])


def test_list_without_context_fails_closed_without_repository_call() -> None:
    repo = repository()
    message = Message(BTN_V2_SOURCES)
    run(show_sources(message, State(), repo, None))
    repo.list_for_workspace.assert_not_awaited()
    assert "недоступен" in message.answers[0][0]


def test_toggle_passes_workspace_id_and_refreshes() -> None:
    updated = source(enabled=False)
    repo = repository((updated,))
    repo.toggle.return_value = updated
    callback = Callback(f"{SOURCE_TOGGLE_PREFIX}{updated.id}")
    run(toggle_source(callback, repo, context(88)))

    repo.toggle.assert_awaited_once_with(88, updated.id)
    repo.list_for_workspace.assert_awaited_once_with(88)
    assert "выключен" in callback.answers[0][0]


def test_toggle_foreign_or_unknown_source_fails_closed() -> None:
    repo = repository()
    repo.toggle.side_effect = UnknownSourceError("no")
    callback = Callback(f"{SOURCE_TOGGLE_PREFIX}foreign")
    run(toggle_source(callback, repo, context(2)))
    assert "не найден" in callback.answers[0][0]
    assert callback.message.answers == []


def test_toggle_without_context_does_not_call_repository() -> None:
    repo = repository()
    callback = Callback(f"{SOURCE_TOGGLE_PREFIX}source-1")
    run(toggle_source(callback, repo, None))
    repo.toggle.assert_not_awaited()
    repo.list_for_workspace.assert_not_awaited()


def test_add_passes_workspace_and_monitoring_role() -> None:
    repo = repository()
    repo.submit_source_request.return_value = SubmitSourceRequestResult(None, "pending")
    message, state = Message("https://example.com/source"), State()
    run(receive_source_url(message, state, repo, context(91)))
    repo.submit_source_request.assert_awaited_once_with(
        91, 100, "https://example.com/source"
    )
    assert "отправлен на проверку" in message.answers[0][0]
    assert state.clear_calls == 1


def test_duplicate_enabled_reports_actual_state() -> None:
    repo = repository()
    repo.submit_source_request.return_value = SubmitSourceRequestResult(
        None, "already_connected"
    )
    message = Message("https://example.com/source")
    run(receive_source_url(message, State(), repo, context()))
    assert "уже подключён" in message.answers[0][0]


def test_duplicate_disabled_reports_actual_state() -> None:
    repo = repository()
    repo.submit_source_request.return_value = SubmitSourceRequestResult(
        None, "already_pending"
    )
    message = Message("https://example.com/source")
    run(receive_source_url(message, State(), repo, context()))
    assert "ожидает проверки" in message.answers[0][0]


def test_rejected_request_is_reported_as_resubmitted() -> None:
    repo = repository()
    repo.submit_source_request.return_value = SubmitSourceRequestResult(
        None, "reopened"
    )
    message = Message("https://example.com/source")
    run(receive_source_url(message, State(), repo, context()))
    assert "повторно отправлен" in message.answers[0][0]


def test_add_without_context_does_not_call_repository() -> None:
    repo = repository()
    message = Message("https://example.com/source")
    run(receive_source_url(message, State(), repo, None))
    repo.submit_source_request.assert_not_awaited()


def test_invalid_address_preserves_input_state() -> None:
    repo = repository()
    repo.submit_source_request.side_effect = SourceAddressError("invalid")
    message, state = Message("ftp://example.com/file"), State()
    run(receive_source_url(message, state, repo, context()))
    assert "Не получилось" in message.answers[0][0]
    assert state.clear_calls == 0


def test_handler_path_has_no_json_store_dependency() -> None:
    import inspect
    from app.handlers import sources

    text = inspect.getsource(sources)
    assert "SourceRegistryStore" not in text


def test_add_button_asks_for_a_url() -> None:
    callback, state = Callback(SOURCE_REGISTRY_ADD), State()
    run(start_add_source(callback, state))
    assert state.state == AddSource.waiting_for_url
    assert "ссылку" in callback.message.answers[0][0]


def test_sources_button_and_keyboard_remain_available() -> None:
    assert BTN_V2_SOURCES in [
        button.text for row in v2_main_menu().keyboard for button in row
    ]
    markup = sources_registry_keyboard((("id", "Название", True),))
    assert _button_texts(markup)[-2:] == ["➕ Предложить источник", BTN_V2_MAIN_MENU]


def test_sources_router_is_registered() -> None:
    import inspect
    from app.handlers import build_router

    assert "include_router(sources.router)" in inspect.getsource(build_router)


async def _matching_handler(text: str, flag_data: dict, raw_state=None):
    message = Message(text)
    for handler in router.message.handlers:
        matched, _ = await handler.check(message, raw_state=raw_state, **flag_data)
        if matched:
            return handler.callback.__name__
    return None


def test_section_is_fail_closed_by_v2_flag() -> None:
    assert run(_matching_handler(BTN_V2_SOURCES, {"v2_menu_enabled": True})) == "show_sources"
    assert run(_matching_handler(BTN_V2_SOURCES, {"v2_menu_enabled": False})) is None


def test_main_menu_button_cancels_adding() -> None:
    assert run(_matching_handler(
        BTN_V2_MAIN_MENU, {"v2_menu_enabled": True},
        raw_state=AddSource.waiting_for_url.state,
    )) == "cancel_add_source"
