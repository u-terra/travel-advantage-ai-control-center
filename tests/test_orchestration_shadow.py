"""Phase 1 eval: shadow-mode LLM orchestration vs the old keyword router.

No live model is available in this environment, so cases A-H are run against
a ScriptedOrchestrationLLMProvider that returns a fixed, hand-written raw
JSON decision per case - the same fake-provider convention already used
throughout this codebase (see tests/llm_fakes.py::FakeLLMProvider). This
validates the HARNESS end to end (prompt separation, structured-output
validation, agreement computation, fail-closed fallback, logging) against
real route_text() output for each case - not the reasoning quality of a
live model, which cannot be exercised here. That distinction is called out
explicitly in the Phase 1 report.
"""

from __future__ import annotations

import asyncio

from app.orchestration.decision import OrchestrationIntent
from app.orchestration.provider import OrchestrationLLMProvider
from app.orchestration.shadow import (
    ShadowComparisonLogger,
    ShadowComparisonRecord,
    run_shadow_orchestration,
)
from app.routing.modules import Module
from app.routing.router import route_text


def run(value):
    return asyncio.run(value)


class State:
    def __init__(self, data=None):
        self.data = data or {}
        self.state = None

    async def get_data(self):
        return self.data

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def get_state(self):
        return self.state


class ScriptedOrchestrationLLMProvider(OrchestrationLLMProvider):
    """Fake provider: returns a pre-scripted raw decision (or None/garbage
    to simulate an error/invalid response), like FakeLLMProvider elsewhere."""

    name = "scripted"

    def __init__(self, raw=None, *, configured: bool = True) -> None:
        self._raw = raw
        self._configured = configured
        self.calls: list = []

    @property
    def is_configured(self) -> bool:
        return self._configured

    def classify(self, *, request):
        self.calls.append(request)
        return self._raw


class RecordingLogger(ShadowComparisonLogger):
    def __init__(self) -> None:
        self.records: list[ShadowComparisonRecord] = []

    def log(self, record: ShadowComparisonRecord) -> None:
        self.records.append(record)


def _decision(**overrides) -> dict:
    base = dict(
        intent="create_content",
        primary_module=Module.CONTENT_FACTORY.value,
        secondary_modules=[],
        safety_required=False,
        uses_previous_turn=False,
        needs_source_analysis=False,
        needs_generation=True,
        needs_clarification=False,
        confidence=0.9,
        reason_code="ok",
    )
    base.update(overrides)
    return base


def _run_case(task_text: str, raw_decision: dict, *, state=None, workspace_id=1):
    old_decision = route_text(task_text)
    provider = ScriptedOrchestrationLLMProvider(raw=raw_decision)
    logger = RecordingLogger()
    record = run(run_shadow_orchestration(
        task_text=task_text, old_decision=old_decision, workspace_id=workspace_id,
        provider=provider, state=state, logger=logger,
    ))
    assert logger.records == [record]
    return old_decision, record, provider


# --- A: explicit rewrite -> Content Factory, old and LLM agree ---

def test_case_a_rewrite_plagiarism_concern_routes_to_content_factory():
    text = (
        "Перепиши этот текст так, чтобы меня не обвинили в плагиате: "
        "Свежий кейс! Отель отдали за 47 т.р. вместо 113 на Букинге."
    )
    old, record, _ = _run_case(text, _decision(
        intent="rewrite", primary_module=Module.CONTENT_FACTORY.value,
        reason_code="leading_rewrite_verb",
    ))
    assert old.primary_module is Module.CONTENT_FACTORY
    assert record.status == "ok"
    assert record.llm_intent == OrchestrationIntent.REWRITE.value
    assert record.llm_primary == Module.CONTENT_FACTORY.value
    assert record.agreement is True


# --- B: explicit safety check -> Safety Layer, old and LLM agree ---

def test_case_b_explicit_risk_check_routes_to_safety_layer():
    text = "Проверь этот пост на риски: скидка 67%, отель за 47 т.р. вместо 113."
    old, record, _ = _run_case(text, _decision(
        intent="check_safety", primary_module=Module.SAFETY_LAYER.value,
        secondary_modules=[Module.CONTENT_FACTORY.value], safety_required=True,
        needs_generation=False, reason_code="leading_check_verb",
    ))
    assert old.primary_module is Module.SAFETY_LAYER
    assert record.status == "ok"
    assert record.agreement is True


# --- C: feedback on a previous Radar result -> old router gets this WRONG
# (it matches the bare word "пост" and treats it as a new content request).
# This is the class of problem Phase 1 exists to catch. ---

def test_case_c_feedback_on_previous_radar_idea_disagrees_with_old_router():
    text = "А почему ты предлагаешь этот никчёмный повод оформить в виде поста? Серьёзно?"
    state = State(data={"orchestration_recent_turns": [
        {"role": "assistant", "text": "Radar предложил пост-идею: Скидки на отели в Сочи",
         "module": Module.LEAD_RADAR.value},
    ]})
    old, record, provider = _run_case(text, _decision(
        intent="feedback_on_previous_result", primary_module=Module.LEAD_RADAR.value,
        uses_previous_turn=True, needs_generation=False, needs_clarification=True,
        confidence=0.75, reason_code="reacting_to_past_assistant_result",
    ), state=state)

    # The bug this case documents: old router sees "пост" and creates content.
    assert old.primary_module is Module.CONTENT_FACTORY

    assert record.status == "ok"
    assert record.llm_intent == OrchestrationIntent.FEEDBACK_ON_PREVIOUS_RESULT.value
    assert record.llm_primary == Module.LEAD_RADAR.value
    # Old said Content Factory, LLM said Lead Radar/feedback - a real
    # disagreement, correctly surfaced rather than hidden.
    assert record.agreement is False

    # And the mechanism that makes this possible: the past Radar result
    # reached the model as PAST ASSISTANT RESULT, not as raw instructions.
    request = provider.calls[0]
    assert any("Радар" in t or "Radar" in t for t in request.past_assistant_result)


# --- D: multi-item content plan -> Content Factory, old and LLM agree ---

def test_case_d_two_week_content_plan_routes_to_content_factory():
    text = "Составь контент-план на 2 недели"
    old, record, _ = _run_case(text, _decision(
        intent="create_content", primary_module=Module.CONTENT_FACTORY.value,
        reason_code="multi_item_plan_request",
    ))
    assert old.primary_module is Module.CONTENT_FACTORY
    assert record.agreement is True


# --- E: long pasted text with no command -> source-analysis candidate to
# OFFER, old router (correctly) has no confident opinion either. ---

_LONG_PASTED_POST = (
    "Хотим поделиться свежим кейсом клиента. Семья из Москвы слетала в "
    "Анталию на десять дней и нашла отель через наш сервис. "
    "Итоговая стоимость проживания оказалась заметно меньше, чем на "
    "популярных туристических сайтах, а трансфер получилось согласовать "
    "отдельно и тоже дешевле обычного. Делимся деталями, чтобы показать, "
    "как сравнение предложений помогает сэкономить при планировании "
    "поездки заранее и без лишних сложностей для всей семьи в дороге."
)


def test_case_e_bare_pasted_post_is_a_source_analysis_candidate_not_auto_rewrite():
    old, record, _ = _run_case(_LONG_PASTED_POST, _decision(
        intent="analyze_source", primary_module=Module.CONTENT_FACTORY.value,
        needs_source_analysis=True, needs_generation=False, needs_clarification=True,
        confidence=0.7, reason_code="long_pasted_material_no_command",
    ))
    assert old.is_uncertain
    assert old.primary_module is Module.ORCHESTRATOR
    assert record.status == "ok"
    # needs_clarification=True means "offer, don't auto-generate" -
    # the exact behaviour required by case E.
    assert record.llm_intent == OrchestrationIntent.ANALYZE_SOURCE.value


# --- F: plain single-post request -> Content Factory, old and LLM agree ---

def test_case_f_single_post_request_routes_to_content_factory():
    text = "Напиши пост про Travel Advantage"
    old, record, _ = _run_case(text, _decision(
        intent="create_content", primary_module=Module.CONTENT_FACTORY.value,
        reason_code="single_post_request",
    ))
    assert old.primary_module is Module.CONTENT_FACTORY
    assert record.agreement is True


# --- G: leading "Перепиши", quoted material contains "проверить" -> rewrite,
# not Safety. Old router already fixed for this class of bug; LLM agrees. ---

def test_case_g_rewrite_wins_over_check_word_inside_quoted_material():
    text = (
        "Перепиши этот пост своими словами: Пришла повестка из военкомата. "
        "Нужно проверить актуальные ограничения перед покупкой билетов."
    )
    old, record, provider = _run_case(text, _decision(
        intent="rewrite", primary_module=Module.CONTENT_FACTORY.value,
        reason_code="leading_rewrite_verb",
    ))
    assert old.primary_module is Module.CONTENT_FACTORY
    assert record.agreement is True
    request = provider.calls[0]
    assert request.user_instruction.startswith("Перепиши")
    assert "военкомата" in request.pasted_material
    assert "военкомата" not in request.user_instruction


# --- H: leading "Проверь", quoted material full of "пост"/"перепиши" ->
# Safety, not rewrite. ---

def test_case_h_leading_check_wins_over_rewrite_words_inside_quoted_material():
    text = (
        "Проверь этот пост на риски: Перепиши этот пост, если хочешь, но "
        "сначала посмотри на структуру поста и черновик поста."
    )
    old, record, provider = _run_case(text, _decision(
        intent="check_safety", primary_module=Module.SAFETY_LAYER.value,
        secondary_modules=[Module.CONTENT_FACTORY.value], safety_required=True,
        needs_generation=False, reason_code="leading_check_verb",
    ))
    assert old.primary_module is Module.SAFETY_LAYER
    assert record.agreement is True
    request = provider.calls[0]
    assert request.user_instruction.startswith("Проверь")
    assert "Перепиши этот пост" in request.pasted_material


# --- Fallback behaviour: never affects the user, always fails closed ---

def test_not_configured_provider_skips_shadow_entirely():
    old = route_text("Напиши пост про Travel Advantage")
    provider = ScriptedOrchestrationLLMProvider(raw=_decision(), configured=False)
    logger = RecordingLogger()
    record = run(run_shadow_orchestration(
        task_text="Напиши пост про Travel Advantage", old_decision=old,
        workspace_id=1, provider=provider, logger=logger,
    ))
    assert record is None
    assert logger.records == []
    assert provider.calls == []  # never even called


def test_provider_returning_none_is_logged_as_error_not_raised():
    old = route_text("Напиши пост про Travel Advantage")
    provider = ScriptedOrchestrationLLMProvider(raw=None)  # network/timeout
    logger = RecordingLogger()
    record = run(run_shadow_orchestration(
        task_text="Напиши пост про Travel Advantage", old_decision=old,
        workspace_id=1, provider=provider, logger=logger,
    ))
    assert record.status == "error"
    assert record.agreement is None
    assert record.llm_primary is None


def test_invalid_json_shape_is_logged_as_invalid_output_not_raised():
    old = route_text("Напиши пост про Travel Advantage")
    provider = ScriptedOrchestrationLLMProvider(raw={"garbage": True})
    logger = RecordingLogger()
    record = run(run_shadow_orchestration(
        task_text="Напиши пост про Travel Advantage", old_decision=old,
        workspace_id=1, provider=provider, logger=logger,
    ))
    assert record.status == "invalid_output"
    assert record.agreement is None


def test_unknown_module_value_is_logged_as_invalid_output_not_raised():
    old = route_text("Напиши пост про Travel Advantage")
    provider = ScriptedOrchestrationLLMProvider(raw=_decision(primary_module="Not A Module"))
    logger = RecordingLogger()
    record = run(run_shadow_orchestration(
        task_text="Напиши пост про Travel Advantage", old_decision=old,
        workspace_id=1, provider=provider, logger=logger,
    ))
    assert record.status == "invalid_output"


def test_provider_raising_is_swallowed_and_logged_as_error():
    """A provider that raises outright (not just returns None) must still
    never propagate - shadow mode must be structurally impossible to break
    the caller's actual response."""
    class ExplodingProvider(OrchestrationLLMProvider):
        name = "exploding"

        @property
        def is_configured(self) -> bool:
            return True

        def classify(self, *, request):
            raise RuntimeError("boom")

    old = route_text("Напиши пост про Travel Advantage")
    logger = RecordingLogger()
    record = run(run_shadow_orchestration(
        task_text="Напиши пост про Travel Advantage", old_decision=old,
        workspace_id=1, provider=ExplodingProvider(), logger=logger,
    ))
    assert record.status == "error"
    assert logger.records == [record]


def test_shadow_record_never_carries_raw_reason_prose_or_task_text_in_full():
    """Structural guard against chain-of-thought / full raw content leaking
    into logs: task_text_preview is bounded, and there is no field on the
    record shaped to hold free-form model prose."""
    long_text = "Напиши пост: " + ("x" * 500)
    old = route_text(long_text)
    provider = ScriptedOrchestrationLLMProvider(raw=_decision())
    logger = RecordingLogger()
    record = run(run_shadow_orchestration(
        task_text=long_text, old_decision=old, workspace_id=1,
        provider=provider, logger=logger,
    ))
    assert len(record.task_text_preview) <= 120
    assert len(record.llm_reason_code) <= 64
    assert "\n" not in record.llm_reason_code
