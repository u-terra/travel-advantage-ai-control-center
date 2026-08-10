from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers.text_review import (
    TextReview, review_artifact, review_free_text, save_artifact_review,
    save_free_text, start_free_text_review,
)
from app.keyboards import (
    ARTIFACT_CHECK_PREFIX, ARTIFACT_REVIEW_SAVE_PREFIX,
    SOURCE_ACTION_MAIN_MENU, TEXT_REVIEW_SAVE,
)
from app.services.llm.models import TextCheckResult, TextSafetyFinding
from tests.llm_fakes import FakeLLMProvider


def run(value):
    return asyncio.run(value)


class State:
    def __init__(self, data=None):
        self.data = data or {}
        self.state = None
    async def clear(self): self.data = {}; self.state = None
    async def set_state(self, value): self.state = value
    async def update_data(self, **kwargs): self.data.update(kwargs)
    async def get_data(self): return self.data


class Message:
    def __init__(self, text="Текст", user_id=1):
        self.text = text
        self.from_user = SimpleNamespace(id=user_id)
        self.answers = []
    async def answer(self, text, **kwargs): self.answers.append((text, kwargs))


class Callback:
    def __init__(self, data, user_id=1):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id) if user_id else None
        self.message = Message(user_id=user_id)
        self.answers = []
    async def answer(self, text=None, **kwargs): self.answers.append((text, kwargs))


def result(text="Улучшенный"):
    return TextCheckResult(
        warnings=(TextSafetyFinding("скидка", "Проверить условие"),),
        rewritten_text=text, rewrite_warnings=(), generation_mode="ai", ai_note=None,
    )


def deps(workspace=True, artifact=True, version=True):
    partner = SimpleNamespace(workspace_id=10) if workspace else None
    repo = SimpleNamespace(
        get_artifact=AsyncMock(return_value=SimpleNamespace(id=20, current_version_id=30) if artifact else None),
        get_current_artifact_version=AsyncMock(return_value=SimpleNamespace(id=30, content="Текущая") if version else None),
        add_artifact_version_if_current=AsyncMock(return_value=SimpleNamespace(version_number=2)),
        create_artifact_with_initial_version=AsyncMock(return_value=(
            SimpleNamespace(id=41), SimpleNamespace(id=42, version_number=1)
        )),
    )
    return partner, repo


def test_v2_button_prompts_for_arbitrary_text():
    message, state = Message(), State()
    run(start_free_text_review(message, state))
    assert state.state == TextReview.waiting_for_text
    assert message.answers[0][0] == "Пришли текст, который нужно проверить и улучшить."


def test_free_text_calls_check_once_and_does_not_create_artifact():
    message, state = Message("Исходник"), State()
    provider = FakeLLMProvider(check=result())
    run(review_free_text(message, state, provider))
    check = provider.check_text
    check.assert_called_once()
    assert check.call_args.kwargs["source_text"] == "Исходник"
    assert "Предлагаемая версия:\nУлучшенный" in message.answers[0][0]
    assert "Перед публикацией проверьте факты, даты, цены и ссылки." in message.answers[0][0]


def test_menu_review_without_rewrite_has_no_save_action_or_payload():
    message = Message("Исходник")
    state = State({"reviewed_text": "Старый результат"})
    run(review_free_text(message, state, FakeLLMProvider(check=result(None))))
    markup = message.answers[-1][1]["reply_markup"]
    assert not hasattr(markup, "inline_keyboard")
    assert TEXT_REVIEW_SAVE not in str(markup)
    assert state.data == {}
    assert "Текущую версию можно оставить" in message.answers[-1][0]


@pytest.mark.parametrize("data", ["artifact_check:x", "artifact_check:0", "artifact_check:"])
def test_malformed_artifact_callback_fails_closed(data):
    callback, state = Callback(data), State()
    partner, repo = deps()
    run(review_artifact(callback, state, partner, repo, FakeLLMProvider()))
    repo.get_artifact.assert_not_awaited()
    assert callback.answers[-1][1]["show_alert"] is True


@pytest.mark.parametrize("workspace,artifact,version", [
    (False, True, True), (True, False, True), (True, True, False),
])
def test_missing_workspace_artifact_or_current_version_fails_closed(workspace, artifact, version):
    callback, state = Callback("artifact_check:20"), State()
    partner, repo = deps(workspace, artifact, version)
    run(review_artifact(callback, state, partner, repo, FakeLLMProvider()))
    assert callback.answers[-1][1]["show_alert"] is True


def test_artifact_review_uses_current_text_once_without_changing_version():
    callback, state = Callback("artifact_check:20"), State()
    partner, repo = deps()
    provider = FakeLLMProvider(check=result())
    run(review_artifact(callback, state, partner, repo, provider))
    check = provider.check_text
    check.assert_called_once()
    assert check.call_args.kwargs["source_text"] == "Текущая"
    repo.add_artifact_version_if_current.assert_not_awaited()
    assert state.data["review_version_id"] == 30


def test_artifact_review_without_rewrite_has_no_save_or_version_payload():
    callback = Callback("artifact_check:20")
    state = State({
        "review_artifact_id": 99, "review_version_id": 98,
        "reviewed_text": "Старый результат",
    })
    partner, repo = deps()
    run(review_artifact(
        callback, state, partner, repo, FakeLLMProvider(check=result(None))
    ))
    markup = callback.message.answers[-1][1]["reply_markup"]
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert not any(value.startswith(ARTIFACT_REVIEW_SAVE_PREFIX) for value in callbacks)
    assert state.data == {}
    repo.add_artifact_version_if_current.assert_not_awaited()
    assert "Текущую версию можно оставить" in callback.message.answers[-1][0]


def test_ai_failure_hides_token_and_creates_no_version():
    callback, state = Callback("artifact_check:20"), State()
    partner, repo = deps()
    run(review_artifact(callback, state, partner, repo, FakeLLMProvider(check=None)))
    text = callback.message.answers[-1][0]
    assert "Исходный черновик сохранён и не изменён" in text and "secret" not in text
    repo.add_artifact_version_if_current.assert_not_awaited()


def test_save_rechecks_tenant_and_expected_version():
    callback = Callback("artifact_review_save:20")
    state = State({"review_artifact_id": 20, "review_version_id": 30, "reviewed_text": "Новый"})
    partner, repo = deps()
    run(save_artifact_review(callback, state, partner, repo))
    repo.add_artifact_version_if_current.assert_awaited_once_with(
        10, 20, 30, "Новый", generation_note="Safety Layer: улучшенная версия"
    )
    assert "Сохранена версия №2" in callback.message.answers[-1][0]


def test_old_button_for_other_artifact_is_rejected():
    callback = Callback("artifact_review_save:19")
    state = State({"review_artifact_id": 20, "review_version_id": 30, "reviewed_text": "Новый"})
    partner, repo = deps()
    run(save_artifact_review(callback, state, partner, repo))
    repo.add_artifact_version_if_current.assert_not_awaited()
    assert callback.answers[-1][1]["show_alert"] is True


def test_repository_failure_never_shows_success():
    callback = Callback("artifact_review_save:20")
    state = State({"review_artifact_id": 20, "review_version_id": 30, "reviewed_text": "Новый"})
    partner, repo = deps()
    repo.add_artifact_version_if_current.side_effect = RuntimeError("token-secret")
    run(save_artifact_review(callback, state, partner, repo))
    assert callback.message.answers == []
    assert "token-secret" not in callback.answers[-1][0]


def test_save_free_text_shows_check_for_created_artifact_and_main_menu():
    callback = Callback(TEXT_REVIEW_SAVE)
    state = State({"reviewed_text": "Проверенный текст"})
    partner, repo = deps()

    run(save_free_text(callback, state, partner, repo))

    assert repo.create_artifact_with_initial_version.call_args.args == (10,)
    created_artifact = repo.create_artifact_with_initial_version.return_value[0]
    message_text, kwargs = callback.message.answers[-1]
    callbacks = [
        button.callback_data
        for row in kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert f"Материал №{created_artifact.id}" in message_text
    assert f"{ARTIFACT_CHECK_PREFIX}{created_artifact.id}" in callbacks
    assert f"{ARTIFACT_CHECK_PREFIX}20" not in callbacks
    assert SOURCE_ACTION_MAIN_MENU in callbacks


def test_save_free_text_failure_does_not_show_artifact_check_button():
    callback = Callback(TEXT_REVIEW_SAVE)
    state = State({"reviewed_text": "Проверенный текст"})
    partner, repo = deps()
    repo.create_artifact_with_initial_version.side_effect = RuntimeError("private")

    run(save_free_text(callback, state, partner, repo))

    assert callback.message.answers == []
    assert callback.answers[-1][0] == "Не удалось сохранить материал."
