from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.handlers.material_generation import (
    SUPPORTED_FORMATS,
    build_source_context,
    choose_material_format,
    generate_source_material,
    _draft_messages,
)
from app.keyboards import ARTIFACT_CHECK_PREFIX, source_material_formats_keyboard
from app.services.content_factory import ContentDraft, ContentFactoryConfig


def run(value):
    return asyncio.run(value)


class Message:
    def __init__(self):
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


class Callback:
    def __init__(self, data, user_id=1, with_message=True):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id) if user_id is not None else None
        self.message = Message() if with_message else None
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


def analysis(source_id=20, workspace_id=10):
    return SimpleNamespace(
        source_id=source_id, workspace_id=workspace_id, summary="Итог",
        key_facts=("Факт",), disputed_claims=("Спорное",),
        audience_value="Польза", target_audiences=("Туристы",),
        content_angles=("Обзор",), warnings=("Проверить цену",),
    )


def dependencies(*, workspace=True, source=True, analyzed=True):
    partner = SimpleNamespace(find_workspace_by_telegram_id=AsyncMock(
        return_value=SimpleNamespace(id=10) if workspace else None
    ))
    source_value = SimpleNamespace(
        id=20, workspace_id=10, original_text="Исходный текст", title="Источник"
    ) if source else None
    artifacts = SimpleNamespace(
        get_source=AsyncMock(return_value=source_value),
        create_artifact_with_initial_version=AsyncMock(
            return_value=(SimpleNamespace(id=30), object())
        ),
    )
    analyses = SimpleNamespace(get_by_source_id=AsyncMock(
        return_value=analysis() if analyzed else None
    ))
    return partner, artifacts, analyses


def test_format_buttons_are_scoped_to_source_and_supported_formats_only():
    keyboard = source_material_formats_keyboard(42)
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert callbacks[:2] == [
        "source_material_format:42:telegram", "source_material_format:42:vk"
    ]
    assert SUPPORTED_FORMATS == {"telegram": "Telegram", "vk": "VK"}
    assert all(len(value.encode()) <= 64 for value in callbacks)


@pytest.mark.parametrize("data", ["source_material:x", "source_material:0", "source_material:"])
def test_malformed_source_callback_fails_closed(data):
    callback = Callback(data)
    partner, artifacts, analyses = dependencies()
    run(choose_material_format(callback, partner, artifacts, analyses))
    assert callback.answers[-1][1]["show_alert"] is True
    partner.find_workspace_by_telegram_id.assert_not_awaited()


@pytest.mark.parametrize("workspace,source,analyzed", [
    (False, True, True), (True, False, True), (True, True, False),
])
def test_missing_workspace_source_or_analysis_fails_closed(workspace, source, analyzed):
    callback = Callback("source_material:20")
    deps = dependencies(workspace=workspace, source=source, analyzed=analyzed)
    run(choose_material_format(callback, *deps))
    assert callback.answers[-1][1]["show_alert"] is True
    assert callback.message.answers == []


def test_foreign_workspace_source_is_not_recovered_by_unscoped_id():
    callback = Callback("source_material:20", user_id=2)
    partner, artifacts, analyses = dependencies()
    artifacts.get_source.return_value = None
    analyses.get_by_source_id.return_value = None
    run(choose_material_format(callback, partner, artifacts, analyses))
    analyses.get_by_source_id.assert_awaited_once_with(10, 20)
    artifacts.get_source.assert_awaited_once_with(10, 20)
    assert callback.answers[-1][1]["show_alert"] is True


def test_choose_format_rechecks_owned_source_and_shows_selector():
    callback = Callback("source_material:20")
    deps = dependencies()
    run(choose_material_format(callback, *deps))
    assert callback.message.answers[-1][0] == "Выбери формат материала."


@pytest.mark.parametrize("data", [
    "source_material_format:20:reels", "source_material_format:x:telegram",
    "source_material_format:20", "source_material_format:20:vk:extra",
])
def test_unsupported_or_malformed_format_never_calls_dependencies(data):
    callback = Callback(data)
    partner, artifacts, analyses = dependencies()
    run(generate_source_material(
        callback, partner, artifacts, analyses, ContentFactoryConfig("u", "t", 1)
    ))
    partner.find_workspace_by_telegram_id.assert_not_awaited()
    artifacts.create_artifact_with_initial_version.assert_not_awaited()


def test_context_contains_full_saved_analysis_and_protects_risky_claims():
    source = SimpleNamespace(original_text="Оригинал")
    text = build_source_context(source, analysis())
    for expected in (
        "Оригинал", "Итог", "Факт", "Спорное", "Польза", "Туристы",
        "Обзор", "Проверить цену", "Не использовать как подтверждённые факты",
    ):
        assert expected in text


def test_long_draft_is_split_into_telegram_safe_messages_without_data_loss():
    original = "x" * 8000
    messages = _draft_messages("telegram", original, ("Проверить",))
    assert all(len(message) <= 3900 for message in messages)
    assert "".join(message for message in messages).count("x") == len(original)
    assert "ручной проверки" in messages[-1]


@pytest.mark.parametrize("output_format", ["telegram", "vk"])
def test_generation_calls_factory_once_then_saves_linked_artifact(output_format):
    callback = Callback(f"source_material_format:20:{output_format}")
    partner, artifacts, analyses = dependencies()
    config = ContentFactoryConfig("http://factory/internal/generate", "secret", 7.5)
    with patch(
        "app.handlers.material_generation.generate_draft_sync",
        return_value=ContentDraft("Черновик", ("Ручная проверка",)),
    ) as generate:
        run(generate_source_material(callback, partner, artifacts, analyses, config))
    assert generate.call_count == 1
    assert generate.call_args.kwargs["material_type"] == "market_offer"
    assert generate.call_args.kwargs["output_format"] == output_format
    assert generate.call_args.kwargs["mode"] == "ai"
    assert "Исходный текст" in generate.call_args.kwargs["source_text"]
    artifacts.create_artifact_with_initial_version.assert_awaited_once()
    kwargs = artifacts.create_artifact_with_initial_version.call_args.kwargs
    assert kwargs["source_id"] == 20 and kwargs["content"] == "Черновик"
    assert "✍️ Черновик материала" in callback.message.answers[-1][0]
    assert "проверки" in callback.message.answers[-1][0]
    assert "secret" not in callback.message.answers[-1][0]
    buttons = callback.message.answers[-1][1]["reply_markup"].inline_keyboard
    artifact = artifacts.create_artifact_with_initial_version.return_value[0]
    assert buttons[0][0].callback_data == f"{ARTIFACT_CHECK_PREFIX}{artifact.id}"


def test_ai_failure_creates_no_artifact_and_hides_details():
    callback = Callback("source_material_format:20:telegram")
    partner, artifacts, analyses = dependencies()
    with patch("app.handlers.material_generation.generate_draft_sync", return_value=None):
        run(generate_source_material(
            callback, partner, artifacts, analyses,
            ContentFactoryConfig("u", "secret-token", 1),
        ))
    artifacts.create_artifact_with_initial_version.assert_not_awaited()
    assert "Исходный разбор сохранён" in callback.message.answers[-1][0]
    assert "secret-token" not in callback.message.answers[-1][0]


def test_persistence_failure_does_not_show_false_success():
    callback = Callback("source_material_format:20:telegram")
    partner, artifacts, analyses = dependencies()
    artifacts.create_artifact_with_initial_version.side_effect = RuntimeError("private")
    with patch(
        "app.handlers.material_generation.generate_draft_sync",
        return_value=ContentDraft("Черновик", ()),
    ):
        run(generate_source_material(
            callback, partner, artifacts, analyses, ContentFactoryConfig("u", "t", 1)
        ))
    assert len(callback.message.answers) == 1
    assert "Не удалось" in callback.message.answers[0][0]
    assert "private" not in callback.message.answers[0][0]
