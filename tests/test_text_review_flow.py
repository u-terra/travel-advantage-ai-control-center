from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.handlers.text_review import (
    TextReview, review_artifact, review_free_text, save_artifact_review,
    start_free_text_review,
)
from app.keyboards import ARTIFACT_REVIEW_SAVE_PREFIX, TEXT_REVIEW_SAVE
from app.services.content_factory import (
    ContentFactoryConfig, TextCheckResult, TextSafetyFinding,
)


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
    partner = SimpleNamespace(find_workspace_by_telegram_id=AsyncMock(
        return_value=SimpleNamespace(id=10) if workspace else None
    ))
    repo = SimpleNamespace(
        get_artifact=AsyncMock(return_value=SimpleNamespace(id=20, current_version_id=30) if artifact else None),
        get_current_artifact_version=AsyncMock(return_value=SimpleNamespace(id=30, content="Текущая") if version else None),
        add_artifact_version_if_current=AsyncMock(return_value=SimpleNamespace(version_number=2)),
    )
    return partner, repo


def test_v2_button_prompts_for_arbitrary_text():
    message, state = Message(), State()
    run(start_free_text_review(message, state))
    assert state.state == TextReview.waiting_for_text
    assert message.answers[0][0] == "Пришли текст, который нужно проверить и улучшить."


def test_free_text_calls_check_once_and_does_not_create_artifact():
    message, state = Message("Исходник"), State()
    with patch("app.handlers.text_review.check_text_sync", return_value=result()) as check:
        run(review_free_text(message, state, ContentFactoryConfig("u", "token", 1)))
    check.assert_called_once()
    assert check.call_args.kwargs["source_text"] == "Исходник"
    assert "Предлагаемая версия:\nУлучшенный" in message.answers[0][0]
    assert "Перед публикацией проверьте факты, даты, цены и ссылки." in message.answers[0][0]


def test_menu_review_without_rewrite_has_no_save_action_or_payload():
    message = Message("Исходник")
    state = State({"reviewed_text": "Старый результат"})
    with patch("app.handlers.text_review.check_text_sync", return_value=result(None)):
        run(review_free_text(message, state, ContentFactoryConfig("u", "t", 1)))
    markup = message.answers[-1][1]["reply_markup"]
    assert not hasattr(markup, "inline_keyboard")
    assert TEXT_REVIEW_SAVE not in str(markup)
    assert state.data == {}
    assert "Текущую версию можно оставить" in message.answers[-1][0]


@pytest.mark.parametrize("data", ["artifact_check:x", "artifact_check:0", "artifact_check:"])
def test_malformed_artifact_callback_fails_closed(data):
    callback, state = Callback(data), State()
    partner, repo = deps()
    run(review_artifact(callback, state, partner, repo, ContentFactoryConfig("u", "t", 1)))
    partner.find_workspace_by_telegram_id.assert_not_awaited()
    assert callback.answers[-1][1]["show_alert"] is True


@pytest.mark.parametrize("workspace,artifact,version", [
    (False, True, True), (True, False, True), (True, True, False),
])
def test_missing_workspace_artifact_or_current_version_fails_closed(workspace, artifact, version):
    callback, state = Callback("artifact_check:20"), State()
    partner, repo = deps(workspace, artifact, version)
    run(review_artifact(callback, state, partner, repo, ContentFactoryConfig("u", "t", 1)))
    assert callback.answers[-1][1]["show_alert"] is True


def test_artifact_review_uses_current_text_once_without_changing_version():
    callback, state = Callback("artifact_check:20"), State()
    partner, repo = deps()
    with patch("app.handlers.text_review.check_text_sync", return_value=result()) as check:
        run(review_artifact(callback, state, partner, repo, ContentFactoryConfig("u", "t", 1)))
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
    with patch("app.handlers.text_review.check_text_sync", return_value=result(None)):
        run(review_artifact(
            callback, state, partner, repo, ContentFactoryConfig("u", "t", 1)
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
    with patch("app.handlers.text_review.check_text_sync", return_value=None):
        run(review_artifact(callback, state, partner, repo, ContentFactoryConfig("u", "secret", 1)))
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
