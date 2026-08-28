from __future__ import annotations

import asyncio

import pytest

from app.planner.context import PlannerExecutionContext
from app.planner.cost import LLMCallBudget
from app.planner.plan import validate_task_plan
from app.planner.runner import PlannerRunResult
from app.planner.synthesis import build_final_reply
from app.services.llm.models import ContentDraft
from tests.llm_fakes import FakeLLMProvider


def _run(coro):
    return asyncio.run(coro)


def _plan(steps):
    return validate_task_plan({
        "goal": "test goal",
        "reason": "test reason",
        "steps": steps,
        "final_output": "test final output",
    })


def _step(id, executor, depends_on=None):
    return {"id": id, "action": "do it", "executor": executor, "input": {}, "depends_on": depends_on or []}


def _run_result(plan, step_results):
    completed = tuple(s.id for s in plan.steps if s.id in step_results)
    return PlannerRunResult(
        plan=plan, step_results=step_results, completed_steps=completed,
        failed_step=None, success=True, error=None,
    )


def _context(**overrides):
    base = dict(workspace_id=1)
    base.update(overrides)
    return PlannerExecutionContext(**base)


# ── zero-cost path: plan already ends in ready prose ────────────────────────


def test_generate_content_final_step_is_used_directly_with_no_extra_llm_call():
    plan = _plan([_step("step_1", "generate_content")])
    run_result = _run_result(plan, {"step_1": {"text": "Ready actionable answer.", "warnings": []}})
    provider = FakeLLMProvider()
    context = _context(llm_provider=provider)

    reply, used_llm = _run(build_final_reply(
        user_task="task", plan=plan, run_result=run_result, context=context,
    ))

    assert reply == "Ready actionable answer."
    assert used_llm is False
    provider.generate_draft.assert_not_called()


def test_check_safety_final_step_with_rewritten_text_is_used_directly():
    plan = _plan([_step("step_1", "check_safety")])
    run_result = _run_result(plan, {"step_1": {"warnings": [], "rewritten_text": "Safe rewritten answer."}})
    provider = FakeLLMProvider()
    context = _context(llm_provider=provider)

    reply, used_llm = _run(build_final_reply(
        user_task="task", plan=plan, run_result=run_result, context=context,
    ))

    assert reply == "Safe rewritten answer."
    assert used_llm is False
    provider.generate_draft.assert_not_called()


def test_check_safety_without_rewritten_text_falls_through_to_synthesis():
    plan = _plan([_step("step_1", "check_safety")])
    run_result = _run_result(plan, {"step_1": {"warnings": [], "rewritten_text": None}})
    provider = FakeLLMProvider(draft=ContentDraft(text="synthesized", warnings=()))
    context = _context(llm_provider=provider)

    reply, used_llm = _run(build_final_reply(
        user_task="task", plan=plan, run_result=run_result, context=context,
    ))

    assert reply == "synthesized"
    assert used_llm is True
    provider.generate_draft.assert_called_once()


# ── paid path: plan ends in a data-shaped step ──────────────────────────────


def test_analyze_source_final_step_triggers_exactly_one_synthesis_call():
    plan = _plan([_step("step_1", "analyze_source")])
    run_result = _run_result(plan, {"step_1": {"summary": "Competitor undercuts pricing.", "key_facts": ["fact"]}})
    provider = FakeLLMProvider(draft=ContentDraft(text="Actionable synthesized answer.", warnings=()))
    budget = LLMCallBudget(max_calls=5)
    context = _context(llm_provider=provider, llm_call_budget=budget)

    reply, used_llm = _run(build_final_reply(
        user_task="Проанализируй конкурента", plan=plan, run_result=run_result, context=context,
    ))

    assert reply == "Actionable synthesized answer."
    assert used_llm is True
    provider.generate_draft.assert_called_once()
    assert budget.used == 1
    assert budget.calls == ("synthesis",)


def test_synthesis_task_text_includes_task_goal_and_results_not_step_ids():
    plan = _plan([_step("step_1", "analyze_source")])
    run_result = _run_result(plan, {"step_1": {"summary": "Competitor undercuts pricing."}})
    provider = FakeLLMProvider(draft=ContentDraft(text="ok", warnings=()))
    context = _context(llm_provider=provider)

    _run(build_final_reply(
        user_task="Проанализируй конкурента ТурКлуб", plan=plan, run_result=run_result, context=context,
    ))

    _, kwargs = provider.generate_draft.call_args
    source_text = kwargs["source_text"]
    assert "Проанализируй конкурента ТурКлуб" in source_text
    assert "Competitor undercuts pricing." in source_text
    assert "step_1" not in source_text
    assert "analyze_source" not in source_text


# ── fallback behavior when synthesis is unavailable ─────────────────────────


def test_no_llm_provider_falls_back_to_deterministic_summary():
    plan = _plan([_step("step_1", "analyze_source")])
    run_result = _run_result(plan, {"step_1": {"summary": "Some finding."}})
    context = _context(llm_provider=None)

    reply, used_llm = _run(build_final_reply(
        user_task="task", plan=plan, run_result=run_result, context=context,
    ))

    assert used_llm is False
    assert "Some finding." in reply
    assert "не удалось" in reply.lower()


def test_exhausted_budget_falls_back_without_calling_provider():
    plan = _plan([_step("step_1", "analyze_source")])
    run_result = _run_result(plan, {"step_1": {"summary": "Some finding."}})
    provider = FakeLLMProvider(draft=ContentDraft(text="should not be used", warnings=()))
    budget = LLMCallBudget(max_calls=0)
    context = _context(llm_provider=provider, llm_call_budget=budget)

    reply, used_llm = _run(build_final_reply(
        user_task="task", plan=plan, run_result=run_result, context=context,
    ))

    assert used_llm is False
    provider.generate_draft.assert_not_called()
    assert "Some finding." in reply


def test_synthesis_provider_returning_none_falls_back_to_summary():
    plan = _plan([_step("step_1", "analyze_source")])
    run_result = _run_result(plan, {"step_1": {"summary": "Some finding."}})
    provider = FakeLLMProvider(draft=None)
    context = _context(llm_provider=provider)

    reply, used_llm = _run(build_final_reply(
        user_task="task", plan=plan, run_result=run_result, context=context,
    ))

    assert used_llm is True  # a call WAS attempted
    assert "Some finding." in reply


def test_synthesis_provider_raising_falls_back_to_summary_not_an_exception():
    plan = _plan([_step("step_1", "analyze_source")])
    run_result = _run_result(plan, {"step_1": {"summary": "Some finding."}})

    provider = FakeLLMProvider()
    provider.generate_draft.side_effect = RuntimeError("boom")
    context = _context(llm_provider=provider)

    reply, used_llm = _run(build_final_reply(
        user_task="task", plan=plan, run_result=run_result, context=context,
    ))

    assert used_llm is True
    assert "Some finding." in reply


def test_empty_step_results_produces_a_safe_placeholder_not_a_crash():
    plan = _plan([_step("step_1", "list_competitors")])
    run_result = _run_result(plan, {"step_1": {"competitors": []}})
    context = _context(llm_provider=None)

    reply, used_llm = _run(build_final_reply(
        user_task="task", plan=plan, run_result=run_result, context=context,
    ))
    assert used_llm is False
    assert isinstance(reply, str) and reply.strip()
