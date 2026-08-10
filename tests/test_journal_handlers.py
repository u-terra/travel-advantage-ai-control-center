from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.domain.partners import WorkspaceContext
from app.handlers.menu import on_last_task, on_radar_content_selected
from app.handlers.tasks import on_free_text, on_task_after_button
from app.services.llm.models import ContentDraft
from app.storage import JournalEntry
from tests.llm_fakes import FakeLLMProvider


def run(coro):
    return asyncio.run(coro)


def context(workspace_id: int = 42) -> WorkspaceContext:
    return WorkspaceContext(100, workspace_id, "owner", "active")


class Message:
    def __init__(self, text: str = "Создай FAQ для партнёра") -> None:
        self.text = text
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


class State:
    def __init__(self, data=None) -> None:
        self.data = data or {}

    async def get_data(self):
        return self.data

    async def clear(self):
        self.data = {}

    async def update_data(self, **kwargs):
        self.data.update(kwargs)


class Callback:
    def __init__(self) -> None:
        self.data = "radar_content:0"
        self.message = Message()
        self.answers = []
        self.message.edit_reply_markup = AsyncMock()

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


def journal():
    return SimpleNamespace(add=AsyncMock(return_value=1), last=AsyncMock())


def test_task_handlers_do_not_write_without_workspace_context() -> None:
    for handler, args in (
        (on_task_after_button, (Message(), State(), journal(), FakeLLMProvider(), None)),
        (on_free_text, (Message(), journal(), FakeLLMProvider(), None)),
    ):
        current_journal = args[2] if handler is on_task_after_button else args[1]
        run(handler(*args))
        current_journal.add.assert_not_awaited()


def test_task_handlers_pass_workspace_id_to_journal() -> None:
    first_journal = journal()
    run(on_task_after_button(
        Message(), State(), first_journal, FakeLLMProvider(), context(17)
    ))
    assert first_journal.add.await_args.args == (17,)

    second_journal = journal()
    run(on_free_text(Message(), second_journal, FakeLLMProvider(), context(23)))
    assert second_journal.add.await_args.args == (23,)


def test_radar_handler_does_not_write_without_workspace_context() -> None:
    current_journal = journal()
    state = State({"radar_content_ideas": [{"title": "Тема"}]})
    run(on_radar_content_selected(
        Callback(), state, current_journal, FakeLLMProvider(), None
    ))
    current_journal.add.assert_not_awaited()


def test_radar_handler_passes_workspace_id_without_changing_flow() -> None:
    current_journal = journal()
    state = State({"radar_content_ideas": [{"title": "Тема"}]})
    provider = FakeLLMProvider(draft=ContentDraft("Черновик", ()))
    callback = Callback()

    run(on_radar_content_selected(
        callback, state, current_journal, provider, context(31)
    ))

    assert current_journal.add.await_args.args == (31,)
    provider.generate_draft.assert_called_once()
    assert "Черновик" in callback.message.answers[-1][0]


def test_last_task_is_workspace_scoped_and_keeps_user_format() -> None:
    current_journal = journal()
    current_journal.last.return_value = JournalEntry(
        id=1,
        workspace_id=55,
        created_at="2026-01-01T00:00:00+00:00",
        task_text="Задача",
        primary_module="content",
        secondary_modules="",
        safety_level="low",
        status="new",
        note="",
    )
    message = Message()

    run(on_last_task(message, current_journal, context(55)))

    current_journal.last.assert_awaited_once_with(55)
    text = message.answers[0][0]
    assert text.startswith("📋 Последняя задача\n\n")
    assert "Задача: Задача" in text
    assert "Статус: new" in text


def test_last_task_does_not_read_without_workspace_context() -> None:
    current_journal = journal()
    run(on_last_task(Message(), current_journal, None))
    current_journal.last.assert_not_awaited()
