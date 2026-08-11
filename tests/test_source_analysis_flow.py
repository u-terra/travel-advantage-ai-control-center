from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.handlers.source_analysis import AnalyzeSource, receive_source_text, router, start_source_analysis
from app.handlers import build_router
from app.handlers import menu as menu_handlers, tasks as task_handlers
from app.keyboards import (
    BTN_V2_ANALYZE_LINK, BTN_V2_ANALYZE_MORE, BTN_V2_MAIN_MENU,
    source_analysis_result_keyboard,
)
from app.services.llm.models import SourceAnalysisPayload
from tests.llm_fakes import FakeLLMProvider


def run(value): return asyncio.run(value)


class State:
    def __init__(self): self.state = None; self.clear_calls = 0
    async def clear(self): self.state = None; self.clear_calls += 1
    async def set_state(self, value): self.state = value


class Message:
    def __init__(self, text, user_id=1):
        self.text = text
        self.from_user = SimpleNamespace(id=user_id) if user_id is not None else None
        self.answers = []
    async def answer(self, text, **kwargs): self.answers.append((text, kwargs))


def test_button_sets_dedicated_state_and_honest_prompt():
    message, state = Message(BTN_V2_ANALYZE_LINK), State()
    run(start_source_analysis(message, state))
    assert state.state == AnalyzeSource.waiting_for_text
    assert "вставленный текст" in message.answers[0][0]
    assert "чтение ссылок" in message.answers[0][0]


def test_real_router_order_and_repeat_keyboard_label():
    owner = router.parent_router or build_router()
    assert [child.name for child in owner.sub_routers] == [
        "start", "consent", "menu", "sources", "source_analysis", "material_generation",
        "text_review", "tasks",
    ]
    message, state = Message(BTN_V2_ANALYZE_MORE), State()
    run(start_source_analysis(message, state))
    assert state.state == AnalyzeSource.waiting_for_text
    assert [button.text for row in source_analysis_result_keyboard().keyboard for button in row] == [
        BTN_V2_ANALYZE_MORE, BTN_V2_MAIN_MENU
    ]


async def matching_button(flag_data):
    message = Message(BTN_V2_ANALYZE_LINK)
    for handler in router.message.handlers:
        matched, _ = await handler.check(message, raw_state=None, **flag_data)
        if matched: return handler.callback.__name__
    return None


def test_button_is_fail_closed_by_v2_flag():
    assert run(matching_button({"v2_menu_enabled": True})) == "start_source_analysis"
    assert run(matching_button({"v2_menu_enabled": False})) is None
    assert run(matching_button({})) is None


async def first_handler_for_state(text, raw_state):
    message = Message(text)
    for handler in router.message.handlers:
        matched, _ = await handler.check(message, raw_state=raw_state, v2_menu_enabled=True)
        if matched: return handler.callback.__name__
    return None


def test_processing_state_swallows_repeated_text_before_general_tasks():
    assert run(first_handler_for_state("Повтор", AnalyzeSource.processing.state)) == "source_analysis_in_progress"
    assert run(first_handler_for_state(BTN_V2_ANALYZE_LINK, AnalyzeSource.processing.state)) == "source_analysis_in_progress"


def dependencies(workspace=True):
    partner = SimpleNamespace(workspace_id=10) if workspace else None
    source = SimpleNamespace(id=20)
    artifacts = SimpleNamespace(create_source=AsyncMock(return_value=source))
    analyses = SimpleNamespace(save_successful_analysis=AsyncMock(return_value=SimpleNamespace(
        summary="Итог", key_facts=(), disputed_claims=(), audience_value="Польза",
        target_audiences=(), content_angles=(), recommended_formats=(), warnings=())))
    return partner, artifacts, analyses


def test_empty_and_too_long_do_not_create_source():
    for text in ("   ", "x" * 12001):
        message, state = Message(text), State()
        state.state = AnalyzeSource.waiting_for_text
        partner, artifacts, analyses = dependencies()
        run(receive_source_text(message, state, partner, artifacts, analyses, FakeLLMProvider()))
        artifacts.create_source.assert_not_called()
        assert state.state == AnalyzeSource.waiting_for_text


async def first_handler_across_feature_routers(text, raw_state, flag_data):
    message = Message(text)
    for checked_router in (menu_handlers.router, router, task_handlers.router):
        for handler in checked_router.message.handlers:
            matched, _ = await handler.check(message, raw_state=raw_state, **flag_data)
            if matched:
                return handler.callback.__name__
    return None


def test_router_navigation_and_disabled_manual_label_regressions():
    waiting = AnalyzeSource.waiting_for_text.state
    assert run(first_handler_across_feature_routers(BTN_V2_MAIN_MENU, waiting, {"v2_menu_enabled": True})) == "on_v2_main_menu"
    assert run(first_handler_across_feature_routers(BTN_V2_ANALYZE_MORE, waiting, {"v2_menu_enabled": True})) == "start_source_analysis"
    for flags in ({"v2_menu_enabled": False}, {}):
        assert run(first_handler_across_feature_routers(BTN_V2_ANALYZE_LINK, None, flags)) == "on_free_text"


def test_missing_workspace_does_not_create_source():
    message, state = Message("Текст"), State()
    partner, artifacts, analyses = dependencies(False)
    run(receive_source_text(message, state, partner, artifacts, analyses, FakeLLMProvider()))
    artifacts.create_source.assert_not_called()
    assert state.state is None


def test_success_creates_source_before_ai_and_saves_analysis_without_artifact_or_journal():
    message, state = Message(" Заголовок\nТекст "), State()
    partner, artifacts, analyses = dependencies()
    payload = SourceAnalysisPayload("Итог", (), (), "Польза", (), (), (), ())
    provider = FakeLLMProvider(analysis=payload)
    run(receive_source_text(message, state, partner, artifacts, analyses, provider))
    analyze = provider.analyze_source
    artifacts.create_source.assert_awaited_once()
    assert artifacts.create_source.call_args.args == (10,)
    assert artifacts.create_source.call_args.kwargs == {
        "source_type": "text", "original_url": None,
        "original_text": "Заголовок\nТекст", "title": "Заголовок", "status": "new",
    }
    analyze.assert_called_once()
    analyses.save_successful_analysis.assert_awaited_once_with(10, 20, payload)
    assert "🔎 Анализ источника" in message.answers[-1][0]
    markup = message.answers[-1][1]["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data == "source_material:20"
    assert "Что важно" not in message.answers[-1][0]
    assert state.state is None


def test_repository_and_pre_source_exceptions_clear_processing_state():
    cases = ("source", "analysis")
    for failure in cases:
        message, state = Message("Текст"), State()
        partner, artifacts, analyses = dependencies()
        if failure == "source":
            artifacts.create_source.side_effect = RuntimeError("internal")
        else:
            analyses.save_successful_analysis.side_effect = RuntimeError("internal")
        payload = SourceAnalysisPayload("Итог", (), (), "Польза", (), (), (), ())
        run(receive_source_text(
            message, state, partner, artifacts, analyses,
            FakeLLMProvider(analysis=payload),
        ))
        assert state.state is None
        assert len(message.answers) == 1
        assert "internal" not in message.answers[0][0]


def test_card_formatting_exception_clears_state_and_reports_saved_analysis():
    message, state = Message("Текст"), State()
    partner, artifacts, analyses = dependencies()
    payload = SourceAnalysisPayload("Итог", (), (), "Польза", (), (), (), ())
    with patch(
        "app.handlers.source_analysis.source_analysis_card", side_effect=RuntimeError("internal")
    ):
        run(receive_source_text(
            message, state, partner, artifacts, analyses,
            FakeLLMProvider(analysis=payload),
        ))
    assert state.state is None
    assert len(message.answers) == 1
    assert "Анализ сохранён" in message.answers[0][0]


def test_navigation_labels_are_not_received_as_source_during_processing():
    for text in (BTN_V2_ANALYZE_MORE, BTN_V2_MAIN_MENU):
        assert run(first_handler_for_state(text, AnalyzeSource.processing.state)) == "source_analysis_in_progress"


def test_api_error_leaves_persistence_to_new_source_only():
    message, state = Message("Текст"), State()
    partner, artifacts, analyses = dependencies()
    run(receive_source_text(
        message, state, partner, artifacts, analyses, FakeLLMProvider(analysis=None)
    ))
    artifacts.create_source.assert_awaited_once()
    assert artifacts.create_source.call_args.kwargs["status"] == "new"
    analyses.save_successful_analysis.assert_not_called()
    assert "Текст сохранён" in message.answers[-1][0]
    assert state.state is None
