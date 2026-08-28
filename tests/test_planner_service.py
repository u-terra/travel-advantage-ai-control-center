from __future__ import annotations

import asyncio

import pytest

import app.planner.executors as executors_module
from app.planner.context import PlannerExecutionContext
from app.planner.fetch import FetchedPublicSource
from app.planner.plan import TaskPlan
from app.planner.provider import PlannerLLMProvider
from app.planner.service import PlannerOutcome, run_planner_for_task
from app.services.llm.models import ContentDraft, SourceAnalysisPayload
from tests.llm_fakes import FakeLLMProvider


def _run(coro):
    return asyncio.run(coro)


class _FakeCompetitorRepository:
    def __init__(self, competitors=None):
        self._competitors = competitors or []

    async def list_for_workspace(self, workspace_id, limit=20):
        return self._competitors[:limit]


class _FakePlannerProvider(PlannerLLMProvider):
    name = "fake"

    def __init__(self, *, plan_result=None, raises: Exception | None = None):
        self._plan_result = plan_result
        self._raises = raises
        self.calls = 0

    @property
    def is_configured(self) -> bool:
        return True

    def plan(self, *, request):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._plan_result


def _valid_competitor_plan() -> dict:
    return {
        "goal": "Analyze competitor and propose next steps",
        "reason": "user asked to analyze a competitor",
        "steps": [
            {"id": "step_1", "action": "List competitors", "executor": "list_competitors", "input": {}, "depends_on": []},
            {"id": "step_2", "action": "Fetch competitor page", "executor": "fetch_public_source", "input": {"url": "https://competitor.example.com/"}, "depends_on": ["step_1"]},
            {"id": "step_3", "action": "Analyze page", "executor": "analyze_source", "input": {}, "depends_on": ["step_2"]},
            {"id": "step_4", "action": "Generate recommendations", "executor": "generate_content", "input": {}, "depends_on": ["step_3"]},
        ],
        "final_output": "Actionable recommendations",
    }


def _context(**overrides):
    base = dict(workspace_id=1, competitor_repository=_FakeCompetitorRepository())
    base.update(overrides)
    return PlannerExecutionContext(**base)


def test_successful_full_run_returns_actionable_reply(monkeypatch):
    monkeypatch.setattr(
        executors_module, "fetch_public_source_sync",
        lambda url: FetchedPublicSource(
            url=url, final_url=url, title="T", text="Competitor sells budget tours.", content_type="text/html",
        ),
    )
    llm_provider = FakeLLMProvider(
        analysis=SourceAnalysisPayload(
            summary="Competitor undercuts pricing", key_facts=("cheap tours",),
            disputed_claims=(), audience_value="v", target_audiences=(),
            content_angles=(), recommended_formats=(), warnings=(),
        ),
        draft=ContentDraft(text="Here is your actionable plan.", warnings=()),
    )
    planner_provider = _FakePlannerProvider(plan_result=_valid_competitor_plan())
    context = _context(llm_provider=llm_provider)

    accepted_plans = []

    async def on_accepted(plan: TaskPlan) -> None:
        accepted_plans.append(plan)

    outcome = _run(run_planner_for_task(
        "Проанализируй конкурента и предложи что делать",
        provider=planner_provider, execution_context=context,
        on_plan_accepted=on_accepted,
    ))

    assert isinstance(outcome, PlannerOutcome)
    assert outcome.success is True
    assert outcome.reply_text == "Here is your actionable plan."
    assert outcome.fallback_reason is None
    assert planner_provider.calls == 1
    assert len(accepted_plans) == 1
    assert accepted_plans[0].goal == "Analyze competitor and propose next steps"


def test_provider_plan_raising_falls_back(monkeypatch):
    planner_provider = _FakePlannerProvider(raises=RuntimeError("boom"))
    context = _context()

    outcome = _run(run_planner_for_task(
        "Проанализируй конкурента X", provider=planner_provider, execution_context=context,
    ))

    assert outcome.success is False
    assert outcome.reply_text is None
    assert outcome.fallback_reason == "plan_provider_error"


def test_provider_plan_returning_none_falls_back():
    planner_provider = _FakePlannerProvider(plan_result=None)
    context = _context()

    outcome = _run(run_planner_for_task(
        "Проанализируй конкурента X", provider=planner_provider, execution_context=context,
    ))

    assert outcome.success is False
    assert outcome.fallback_reason == "plan_provider_unavailable"


def test_invalid_plan_json_falls_back():
    planner_provider = _FakePlannerProvider(plan_result={"not": "a valid plan"})
    context = _context()

    outcome = _run(run_planner_for_task(
        "Проанализируй конкурента X", provider=planner_provider, execution_context=context,
    ))

    assert outcome.success is False
    assert outcome.fallback_reason == "invalid_plan"


def test_plan_with_unknown_executor_falls_back():
    plan = _valid_competitor_plan()
    plan["steps"][0]["executor"] = "not_a_real_executor"
    planner_provider = _FakePlannerProvider(plan_result=plan)
    context = _context()

    outcome = _run(run_planner_for_task(
        "Проанализируй конкурента X", provider=planner_provider, execution_context=context,
    ))

    assert outcome.success is False
    assert outcome.fallback_reason == "invalid_plan"


def test_step_execution_failure_falls_back_with_step_reason():
    plan = _valid_competitor_plan()
    # No llm_provider in context -> analyze_source step will fail in a
    # controlled way.
    planner_provider = _FakePlannerProvider(plan_result=plan)
    context = _context(llm_provider=None)

    outcome = _run(run_planner_for_task(
        "Проанализируй конкурента X", provider=planner_provider, execution_context=context,
    ))

    assert outcome.success is False
    assert outcome.fallback_reason is not None
    assert outcome.fallback_reason.startswith("step_failed:")


def test_synthesis_raising_unexpectedly_is_a_controlled_failure(monkeypatch):
    plan = {
        "goal": "g", "reason": "r", "final_output": "f",
        "steps": [{"id": "step_1", "action": "a", "executor": "list_competitors", "input": {}, "depends_on": []}],
    }
    planner_provider = _FakePlannerProvider(plan_result=plan)
    context = _context()

    async def _boom(**kwargs):
        raise RuntimeError("synthesis exploded")

    monkeypatch.setattr("app.planner.service.build_final_reply", _boom)

    outcome = _run(run_planner_for_task(
        "Проанализируй конкурента X", provider=planner_provider, execution_context=context,
    ))

    assert outcome.success is False
    assert outcome.fallback_reason == "synthesis_error"


def test_on_plan_accepted_callback_failure_does_not_break_the_run():
    plan = {
        "goal": "g", "reason": "r", "final_output": "f",
        "steps": [{"id": "step_1", "action": "a", "executor": "list_competitors", "input": {}, "depends_on": []}],
    }
    planner_provider = _FakePlannerProvider(plan_result=plan)
    context = _context()

    async def _boom(plan):
        raise RuntimeError("ack failed")

    outcome = _run(run_planner_for_task(
        "Проанализируй конкурента X", provider=planner_provider, execution_context=context,
        on_plan_accepted=_boom,
    ))

    assert outcome.success is True


def test_each_planner_run_gets_an_independent_budget():
    """Two separate calls must not share LLM-call accounting."""
    plan = {
        "goal": "g", "reason": "r", "final_output": "f",
        "steps": [{"id": "step_1", "action": "a", "executor": "list_competitors", "input": {}, "depends_on": []}],
    }
    planner_provider = _FakePlannerProvider(plan_result=plan)
    context = _context()

    outcome1 = _run(run_planner_for_task("task 1", provider=planner_provider, execution_context=context))
    outcome2 = _run(run_planner_for_task("task 2", provider=planner_provider, execution_context=context))

    assert outcome1.success is True
    assert outcome2.success is True
