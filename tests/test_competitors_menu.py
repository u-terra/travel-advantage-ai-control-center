from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

from app.domain.competitors import Competitor
from app.domain.partners import WorkspaceContext
from app.handlers.competitors import (
    AddCompetitor,
    RenameCompetitor,
    _EMPTY,
    _RENAME_NOT_FOUND,
    _RENAME_SAVED,
    _SAVED,
    _UNAVAILABLE,
    cancel_add_competitor,
    cancel_rename_competitor,
    receive_competitor_label,
    receive_competitor_url,
    show_competitors,
    start_add_competitor,
    start_rename_competitor,
)
from app.keyboards import (
    BTN_V2_MAIN_MENU,
    COMPETITOR_REGISTRY_ADD,
    COMPETITOR_REGISTRY_RENAME_PREFIX,
)
from app.repositories.competitor_repository import CompetitorAddressError, CompetitorLabelError
from app.repositories.conversation_state_repository import ConversationStateRepository


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _Message:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.answers: list[tuple[str, Any]] = []

    async def answer(self, text: str, reply_markup: Any = None, **kwargs: Any) -> None:
        self.answers.append((text, reply_markup))


class _Callback:
    def __init__(self, data: str = COMPETITOR_REGISTRY_ADD) -> None:
        self.data = data
        self.message = _Message()
        self.answered = False

    async def answer(self, *args: Any, **kwargs: Any) -> None:
        self.answered = True


class _State:
    def __init__(self) -> None:
        self.state: Any = None
        self.clear_calls = 0
        self.data: dict[str, Any] = {}

    async def set_state(self, state: Any) -> None:
        self.state = state

    async def clear(self) -> None:
        self.clear_calls += 1
        self.state = None
        self.data = {}

    async def get_data(self) -> dict[str, Any]:
        return self.data

    async def update_data(self, **kwargs: Any) -> None:
        self.data.update(kwargs)


def _context(workspace_id: int = 42) -> WorkspaceContext:
    return WorkspaceContext(100, workspace_id, "owner", "active")


def _competitor(
    competitor_id: int = 1, workspace_id: int = 42,
    url: str = "https://competitor.example.com",
) -> Competitor:
    return Competitor(competitor_id, workspace_id, url, url, "2026-08-01T10:00:00+00:00")


def _repository(
    *, competitors: list[Competitor] | None = None, update_label_result: Competitor | None = None,
) -> Any:
    return AsyncMock(
        list_for_workspace=AsyncMock(return_value=competitors or []),
        add_competitor=AsyncMock(),
        update_label=AsyncMock(return_value=update_label_result),
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


# ── Stage 3.2: human labels in the list + rename flow ───────────────────────


def test_show_competitors_with_human_label_shows_label_and_url() -> None:
    message = _Message()
    state = _State()
    labeled = Competitor(1, 42, "https://vk.ru/progulkipovolge", "ТурКлуб", "2026-08-25T11:16:31+00:00")
    repository = _repository(competitors=[labeled])

    _run(show_competitors(message, state, repository, _context(42)))

    text, markup = message.answers[0]
    assert "ТурКлуб" in text
    assert "https://vk.ru/progulkipovolge" in text


def test_show_competitors_without_human_label_shows_url_only_as_before() -> None:
    message = _Message()
    state = _State()
    bare = _competitor(1, 42, "https://vk.ru/progulkipovolge")  # label == url
    repository = _repository(competitors=[bare])

    _run(show_competitors(message, state, repository, _context(42)))

    text, _ = message.answers[0]
    # exactly one occurrence of the URL - not duplicated as "url — url"
    assert text.count("https://vk.ru/progulkipovolge") == 1


def test_show_competitors_keyboard_includes_a_rename_button_per_competitor() -> None:
    message = _Message()
    state = _State()
    competitors = [_competitor(1, 42, "https://a.example.com"), _competitor(2, 42, "https://b.example.com")]
    repository = _repository(competitors=competitors)

    _run(show_competitors(message, state, repository, _context(42)))

    _, markup = message.answers[0]
    rename_buttons = [
        button for row in markup.inline_keyboard for button in row
        if button.callback_data.startswith(COMPETITOR_REGISTRY_RENAME_PREFIX)
    ]
    assert {b.callback_data for b in rename_buttons} == {
        f"{COMPETITOR_REGISTRY_RENAME_PREFIX}1", f"{COMPETITOR_REGISTRY_RENAME_PREFIX}2",
    }


def test_start_rename_competitor_sets_waiting_state_with_id() -> None:
    callback = _Callback(f"{COMPETITOR_REGISTRY_RENAME_PREFIX}7")
    state = _State()

    _run(start_rename_competitor(callback, state))

    assert state.state == RenameCompetitor.waiting_for_label
    assert state.data["rename_competitor_id"] == 7
    assert callback.answered is True
    assert len(callback.message.answers) == 1


def test_start_rename_competitor_ignores_malformed_id() -> None:
    callback = _Callback(f"{COMPETITOR_REGISTRY_RENAME_PREFIX}not-a-number")
    state = _State()

    _run(start_rename_competitor(callback, state))

    assert state.state is None
    assert callback.message.answers == []


def test_receive_competitor_label_renames_for_caller_workspace() -> None:
    message = _Message("ТурКлуб")
    state = _State()
    state.data["rename_competitor_id"] = 7
    renamed = Competitor(7, 42, "https://vk.ru/progulkipovolge", "ТурКлуб", "2026-08-25T11:16:31+00:00")
    repository = _repository(update_label_result=renamed)

    _run(receive_competitor_label(message, state, repository, _context(42)))

    repository.update_label.assert_awaited_once_with(42, 7, "ТурКлуб")
    assert message.answers[0][0] == _RENAME_SAVED
    assert state.clear_calls == 1


def test_receive_competitor_label_unavailable_without_workspace_context() -> None:
    message = _Message("ТурКлуб")
    state = _State()
    state.data["rename_competitor_id"] = 7
    repository = _repository()

    _run(receive_competitor_label(message, state, repository, None))

    assert message.answers[0][0] == _UNAVAILABLE
    repository.update_label.assert_not_awaited()


def test_receive_competitor_label_missing_competitor_id_is_handled_gracefully() -> None:
    message = _Message("ТурКлуб")
    state = _State()  # no rename_competitor_id set at all
    repository = _repository()

    _run(receive_competitor_label(message, state, repository, _context(42)))

    assert message.answers[0][0] == _RENAME_NOT_FOUND
    repository.update_label.assert_not_awaited()


def test_receive_competitor_label_not_found_shows_friendly_message() -> None:
    message = _Message("ТурКлуб")
    state = _State()
    state.data["rename_competitor_id"] = 999
    repository = _repository(update_label_result=None)  # not found / wrong workspace

    _run(receive_competitor_label(message, state, repository, _context(42)))

    assert message.answers[0][0] == _RENAME_NOT_FOUND


def test_receive_competitor_label_rejects_empty_label() -> None:
    message = _Message("   ")
    state = _State()
    state.data["rename_competitor_id"] = 7
    repository = _repository()
    repository.update_label.side_effect = CompetitorLabelError("название не должно быть пустым")

    _run(receive_competitor_label(message, state, repository, _context(42)))

    text, _ = message.answers[0]
    assert "Не получилось" in text


def test_cancel_rename_competitor_returns_to_main_menu() -> None:
    message = _Message(BTN_V2_MAIN_MENU)
    state = _State()

    _run(cancel_rename_competitor(message, state))

    assert state.clear_calls == 1
    assert message.answers[0][0].startswith("Главное меню")


# ── F2A: PendingQuestion + subject_ref pilot (parallel to the FSM above) ────


def _conversation_repository(tmp_path) -> ConversationStateRepository:
    repository = ConversationStateRepository(tmp_path / "journal.sqlite3")
    _run(repository.init())
    return repository


def test_start_rename_competitor_creates_pending_question_and_subject_ref(tmp_path) -> None:
    """E + subject_ref: the persisted mirror carries the exact same
    workspace/user/competitor id as the FSM path, and records that the user
    is now referring to that known, already-persisted competitor."""
    callback = _Callback(f"{COMPETITOR_REGISTRY_RENAME_PREFIX}7")
    state = _State()
    conversation_repository = _conversation_repository(tmp_path)

    _run(start_rename_competitor(
        callback, state, _context(42), conversation_repository,
    ))

    question = _run(conversation_repository.get_active_question(42, 100))
    assert question is not None
    assert question.question_type == "competitor_label"
    assert question.subject_ref_type == "competitor"
    assert question.subject_ref_id == 7

    conv_state = _run(conversation_repository.get_state(42, 100))
    assert conv_state is not None
    assert conv_state.current_subject_ref_type == "competitor"
    assert conv_state.current_subject_ref_id == 7
    assert conv_state.active_module == "competitors"


def test_start_rename_competitor_without_conversation_repository_is_unaffected() -> None:
    """Existing callers/tests that don't pass conversation_state_repository
    (default None) must see identical FSM behaviour - see the many
    pre-existing tests above that call start_rename_competitor with just
    (callback, state)."""
    callback = _Callback(f"{COMPETITOR_REGISTRY_RENAME_PREFIX}7")
    state = _State()

    _run(start_rename_competitor(callback, state))

    assert state.state == RenameCompetitor.waiting_for_label
    assert state.data["rename_competitor_id"] == 7


def test_receive_competitor_label_answers_pending_question_on_success(tmp_path) -> None:
    """F: a successful rename marks the matching PendingQuestion answered."""
    conversation_repository = _conversation_repository(tmp_path)
    _run(conversation_repository.create_question(
        42, 100, "competitor_label", _RENAME_SAVED,
        subject_ref_type="competitor", subject_ref_id=7,
    ))
    message = _Message("ТурКлуб")
    state = _State()
    state.data["rename_competitor_id"] = 7
    renamed = Competitor(7, 42, "https://vk.ru/progulkipovolge", "ТурКлуб", "2026-08-25T11:16:31+00:00")
    repository = _repository(update_label_result=renamed)

    _run(receive_competitor_label(
        message, state, repository, _context(42), conversation_repository,
    ))

    assert message.answers[0][0] == _RENAME_SAVED
    question = _run(conversation_repository.get_active_question(42, 100))
    assert question is None  # answered, so no longer active


def test_receive_competitor_label_does_not_answer_a_different_users_question(tmp_path) -> None:
    """G: a question belonging to another workspace/user cannot be answered
    by this call - get_active_question is itself workspace/user-scoped, so
    the cross-tenant question is simply invisible here, not merely rejected."""
    conversation_repository = _conversation_repository(tmp_path)
    other_workspace, other_user = 99, 555
    _run(conversation_repository.create_question(
        other_workspace, other_user, "competitor_label", _RENAME_SAVED,
        subject_ref_type="competitor", subject_ref_id=7,
    ))
    message = _Message("ТурКлуб")
    state = _State()
    state.data["rename_competitor_id"] = 7
    renamed = Competitor(7, 42, "https://vk.ru/progulkipovolge", "ТурКлуб", "2026-08-25T11:16:31+00:00")
    repository = _repository(update_label_result=renamed)

    _run(receive_competitor_label(
        message, state, repository, _context(42), conversation_repository,
    ))

    assert message.answers[0][0] == _RENAME_SAVED
    other_question = _run(
        conversation_repository.get_active_question(other_workspace, other_user)
    )
    assert other_question is not None  # untouched - different tenant


def test_receive_competitor_label_without_conversation_repository_is_unaffected() -> None:
    """Existing callers/tests that don't pass conversation_state_repository
    (default None) must see identical rename behaviour."""
    message = _Message("ТурКлуб")
    state = _State()
    state.data["rename_competitor_id"] = 7
    renamed = Competitor(7, 42, "https://vk.ru/progulkipovolge", "ТурКлуб", "2026-08-25T11:16:31+00:00")
    repository = _repository(update_label_result=renamed)

    _run(receive_competitor_label(message, state, repository, _context(42)))


# ── F2B: verify PendingQuestion lifecycle (no new resolver, no restart
# recovery - only confirming the F2A invariants actually hold) ─────────────


def test_expired_rename_question_does_not_block_a_new_rename(tmp_path) -> None:
    """5: an old, expired-but-never-answered question must not permanently
    block starting a new rename (auto-supersession on create_question, same
    invariant proven at the repository level in
    test_conversation_state_repository.py, exercised here through the
    actual handler)."""
    conversation_repository = _conversation_repository(tmp_path)
    past = "2020-01-01T00:00:00+00:00"
    _run(conversation_repository.create_question(
        42, 100, "competitor_label", _RENAME_SAVED,
        subject_ref_type="competitor", subject_ref_id=3, expires_at=past,
    ))

    callback = _Callback(f"{COMPETITOR_REGISTRY_RENAME_PREFIX}9")
    state = _State()

    _run(start_rename_competitor(callback, state, _context(42), conversation_repository))

    assert state.state == RenameCompetitor.waiting_for_label
    question = _run(conversation_repository.get_active_question(42, 100))
    assert question is not None
    assert question.subject_ref_id == 9


def test_new_rename_after_answered_question_does_not_conflict(tmp_path) -> None:
    """5: a full rename cycle (start -> answer) followed by a second rename
    for a different competitor must not raise/conflict - the first question
    is already answered, not merely expired."""
    conversation_repository = _conversation_repository(tmp_path)
    first_callback = _Callback(f"{COMPETITOR_REGISTRY_RENAME_PREFIX}3")
    first_state = _State()
    _run(start_rename_competitor(first_callback, first_state, _context(42), conversation_repository))

    message = _Message("Первый конкурент")
    message_state = _State()
    message_state.data["rename_competitor_id"] = 3
    renamed = Competitor(3, 42, "https://a.example.com", "Первый конкурент", "2026-08-25T11:16:31+00:00")
    repository = _repository(update_label_result=renamed)
    _run(receive_competitor_label(
        message, message_state, repository, _context(42), conversation_repository,
    ))
    assert _run(conversation_repository.get_active_question(42, 100)) is None

    second_callback = _Callback(f"{COMPETITOR_REGISTRY_RENAME_PREFIX}9")
    second_state = _State()
    _run(start_rename_competitor(  # must not raise ConversationStateConflictError
        second_callback, second_state, _context(42), conversation_repository,
    ))

    assert second_state.state == RenameCompetitor.waiting_for_label
    question = _run(conversation_repository.get_active_question(42, 100))
    assert question is not None
    assert question.subject_ref_id == 9

    assert message.answers[0][0] == _RENAME_SAVED
