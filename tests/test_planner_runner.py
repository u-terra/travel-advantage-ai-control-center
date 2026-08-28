from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import app.planner.executors as executors_module
import app.planner.runner as runner_module
from app.planner.context import PlannerExecutionContext
from app.planner.executors import PlannerExecutionError
from app.planner.fetch import FetchedPublicSource
from app.planner.plan import MAX_STEPS
from app.planner.runner import run_planner_plan
from app.repositories.competitor_repository import CompetitorRepository
from app.repositories.partner_repository import PartnerRepository
from app.services.llm.models import ContentDraft, SourceAnalysisPayload
from tests.llm_fakes import FakeLLMProvider


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _context(**overrides) -> PlannerExecutionContext:
    base = dict(workspace_id=1)
    base.update(overrides)
    return PlannerExecutionContext(**base)


def _plan(steps: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "goal": "test goal",
        "reason": "test reason",
        "steps": steps,
        "final_output": "test final output",
    }


def _step(id: str, executor: str, *, input: dict | None = None, depends_on: list[str] | None = None) -> dict:
    return {
        "id": id,
        "action": f"do {executor}",
        "executor": executor,
        "input": input or {},
        "depends_on": depends_on or [],
    }


def _spy_executor(name: str, calls: list, *, result: Any = None, error: Exception | None = None, sleep: float | None = None):
    async def _executor(*, step_input, dependency_results, context):
        calls.append((name, dict(step_input), dict(dependency_results)))
        if sleep is not None:
            await asyncio.sleep(sleep)
        if error is not None:
            raise error
        return result

    return _executor


# ── basic sequential execution ──────────────────────────────────────────────


def test_successful_sequential_plan_executes_in_order_and_wires_dependencies(monkeypatch):
    calls: list = []
    monkeypatch.setitem(
        executors_module.PLANNER_EXECUTORS, "list_competitors",
        _spy_executor("list_competitors", calls, result={"a": 1}),
    )
    monkeypatch.setitem(
        executors_module.PLANNER_EXECUTORS, "fetch_public_source",
        _spy_executor("fetch_public_source", calls, result={"b": 2}),
    )
    raw_plan = _plan([
        _step("step_1", "list_competitors"),
        _step("step_2", "fetch_public_source", input={"x": 1}, depends_on=["step_1"]),
    ])

    result = _run(run_planner_plan(raw_plan, context=_context()))

    assert result.success is True
    assert result.error is None
    assert result.failed_step is None
    assert result.completed_steps == ("step_1", "step_2")
    assert result.step_results == {"step_1": {"a": 1}, "step_2": {"b": 2}}
    assert [call[0] for call in calls] == ["list_competitors", "fetch_public_source"]
    # step_2 must see exactly step_1's result, keyed by step id, nothing else
    assert calls[1][2] == {"step_1": {"a": 1}}


def test_each_step_is_executed_exactly_once(monkeypatch):
    calls: list = []
    for executor_id in ("list_competitors", "fetch_public_source", "analyze_source", "generate_content"):
        monkeypatch.setitem(
            executors_module.PLANNER_EXECUTORS, executor_id,
            _spy_executor(executor_id, calls, result={"id": executor_id}),
        )
    raw_plan = _plan([
        _step("step_1", "list_competitors"),
        _step("step_2", "fetch_public_source", depends_on=["step_1"]),
        _step("step_3", "analyze_source", depends_on=["step_2"]),
        _step("step_4", "generate_content", depends_on=["step_3"]),
    ])

    result = _run(run_planner_plan(raw_plan, context=_context()))

    assert result.success is True
    names = [call[0] for call in calls]
    assert names == ["list_competitors", "fetch_public_source", "analyze_source", "generate_content"]
    assert len(names) == len(set(names)) == 4


def test_executor_only_sees_its_own_declared_dependency_results(monkeypatch):
    """Regression guard for the core Phase 2 requirement: an executor must
    structurally be unable to see a step result it did not depend on, even
    when that result already exists in the runner's internal step_results.
    """
    calls: list = []
    monkeypatch.setitem(
        executors_module.PLANNER_EXECUTORS, "list_competitors",
        _spy_executor("list_competitors", calls, result={"unrelated": "step_1 result"}),
    )
    monkeypatch.setitem(
        executors_module.PLANNER_EXECUTORS, "rank_signals",
        _spy_executor("rank_signals", calls, result={"relevant": "step_2 result"}),
    )
    monkeypatch.setitem(
        executors_module.PLANNER_EXECUTORS, "check_safety",
        _spy_executor("check_safety", calls, result={"final": True}),
    )
    raw_plan = _plan([
        _step("step_1", "list_competitors"),
        _step("step_2", "rank_signals"),
        # step_3 depends ONLY on step_2, even though step_1 already ran.
        _step("step_3", "check_safety", depends_on=["step_2"]),
    ])

    result = _run(run_planner_plan(raw_plan, context=_context()))

    assert result.success is True
    step_3_dependency_results = calls[2][2]
    assert step_3_dependency_results == {"step_2": {"relevant": "step_2 result"}}
    assert "step_1" not in step_3_dependency_results


def test_analyze_source_receives_only_its_declared_dependency_end_to_end(monkeypatch):
    """Same guarantee as test_executor_only_sees_its_own_declared_dependency_
    results, but through the REAL analyze_source executor (not a spy): if the
    runner ever leaked an unrelated step's result in first, its
    _first_dependency_result would pick that one up instead and
    analyze_source would fail (list_competitors' result has no 'text' field)
    rather than succeeding with the fetch step's text."""
    provider = FakeLLMProvider(
        analysis=SourceAnalysisPayload(
            summary="ok", key_facts=(), disputed_claims=(), audience_value="v",
            target_audiences=(), content_angles=(), recommended_formats=(), warnings=(),
        )
    )

    class _FakeCompetitorRepo:
        async def list_for_workspace(self, workspace_id, limit=20):
            return []

    def fake_fetch(url: str) -> FetchedPublicSource:
        return FetchedPublicSource(
            url=url, final_url=url, title="T", text="the real fetched text", content_type="text/html",
        )

    monkeypatch.setattr(executors_module, "fetch_public_source_sync", fake_fetch)

    raw_plan = _plan([
        _step("step_1", "list_competitors"),
        _step("step_2", "fetch_public_source", input={"url": "https://example.com/"}),
        # depends only on step_2, not step_1.
        _step("step_3", "analyze_source", depends_on=["step_2"]),
    ])
    context = _context(competitor_repository=_FakeCompetitorRepo(), llm_provider=provider)

    result = _run(run_planner_plan(raw_plan, context=context))

    assert result.success is True, result.error
    provider.analyze_source.assert_called_once_with(source_text="the real fetched text")


# ── failure handling ─────────────────────────────────────────────────────────


def test_executor_failure_stops_the_run_before_later_steps(monkeypatch):
    calls: list = []
    monkeypatch.setitem(
        executors_module.PLANNER_EXECUTORS, "list_competitors",
        _spy_executor("list_competitors", calls, result={"a": 1}),
    )
    monkeypatch.setitem(
        executors_module.PLANNER_EXECUTORS, "fetch_public_source",
        _spy_executor("fetch_public_source", calls, error=PlannerExecutionError("boom")),
    )
    monkeypatch.setitem(
        executors_module.PLANNER_EXECUTORS, "analyze_source",
        _spy_executor("analyze_source", calls, result={"never": True}),
    )
    raw_plan = _plan([
        _step("step_1", "list_competitors"),
        _step("step_2", "fetch_public_source", depends_on=["step_1"]),
        _step("step_3", "analyze_source", depends_on=["step_2"]),
    ])

    result = _run(run_planner_plan(raw_plan, context=_context()))

    assert result.success is False
    assert result.failed_step == "step_2"
    assert "boom" in result.error
    assert result.completed_steps == ("step_1",)
    assert [call[0] for call in calls] == ["list_competitors", "fetch_public_source"]
    assert result.step_results == {"step_1": {"a": 1}}


def test_unexpected_executor_exception_is_a_controlled_failure_not_a_crash(monkeypatch):
    async def _boom(*, step_input, dependency_results, context):
        raise ValueError("totally unexpected")

    monkeypatch.setitem(executors_module.PLANNER_EXECUTORS, "list_competitors", _boom)
    raw_plan = _plan([_step("step_1", "list_competitors")])

    result = _run(run_planner_plan(raw_plan, context=_context()))

    assert result.success is False
    assert result.failed_step == "step_1"
    assert "unexpected error" in result.error


def test_step_timeout_is_a_controlled_failure(monkeypatch):
    calls: list = []
    monkeypatch.setitem(
        executors_module.PLANNER_EXECUTORS, "list_competitors",
        _spy_executor("list_competitors", calls, sleep=0.5),
    )
    raw_plan = _plan([_step("step_1", "list_competitors")])

    result = _run(
        run_planner_plan(raw_plan, context=_context(), step_timeout_seconds=0.01)
    )

    assert result.success is False
    assert result.failed_step == "step_1"
    assert "timed out" in result.error


def test_runner_is_fail_closed_if_registry_and_schema_ever_drift(monkeypatch):
    """Defense-in-depth: validate_task_plan + the closed-set assertion in
    app.planner.executors guarantee this cannot happen today, but the runner
    itself must not assume that forever."""
    monkeypatch.setattr(runner_module, "PLANNER_EXECUTORS", {})
    raw_plan = _plan([_step("step_1", "list_competitors")])

    result = _run(run_planner_plan(raw_plan, context=_context()))

    assert result.success is False
    assert result.failed_step == "step_1"
    assert "no executor registered" in result.error


# ── revalidation / MAX_STEPS integration ────────────────────────────────────


def test_invalid_raw_plan_is_a_controlled_failure_without_executing_anything(monkeypatch):
    calls: list = []
    monkeypatch.setitem(
        executors_module.PLANNER_EXECUTORS, "list_competitors",
        _spy_executor("list_competitors", calls, result={}),
    )
    raw_plan = {"goal": "g", "reason": "r", "final_output": "f", "steps": [
        {"id": "step_1", "action": "a", "executor": "not_a_real_executor", "input": {}, "depends_on": []}
    ]}

    result = _run(run_planner_plan(raw_plan, context=_context()))

    assert result.success is False
    assert result.plan is None
    assert result.completed_steps == ()
    assert "invalid_task_plan" in result.error
    assert calls == []


def test_raw_plan_exceeding_max_steps_is_rejected_before_execution(monkeypatch):
    calls: list = []
    monkeypatch.setitem(
        executors_module.PLANNER_EXECUTORS, "check_safety",
        _spy_executor("check_safety", calls, result={}),
    )
    steps = [_step(f"step_{i}", "check_safety") for i in range(MAX_STEPS + 1)]
    raw_plan = _plan(steps)

    result = _run(run_planner_plan(raw_plan, context=_context()))

    assert result.success is False
    assert "invalid_task_plan" in result.error
    assert calls == []


# ── full competitor-analysis scenario (section 6) ───────────────────────────


def _setup_competitor_repository(tmp_path: Path) -> tuple[CompetitorRepository, int]:
    db_path = tmp_path / "workspace.sqlite3"
    partners = PartnerRepository(db_path)
    _run(partners.init())
    workspace, _profile = _run(partners.ensure_owner_workspace(111222333))

    competitors = CompetitorRepository(db_path)
    _run(competitors.init())
    return competitors, workspace.id


def test_competitor_analysis_scenario_runs_end_to_end(tmp_path, monkeypatch):
    """The chain the whole Phase 2 exists for:

        list_competitors -> fetch_public_source -> analyze_source -> generate_content

    proving: the URL genuinely passes through the fetch executor - resolved
    at RUNTIME from step_1's list_competitors result via 'competitor_id',
    never hardcoded in step_2's own input (the TaskPlan is built BEFORE
    list_competitors runs, so a Planner LLM could not have known the real
    URL ahead of time) - the extracted text genuinely reaches analyze_source,
    the analysis genuinely reaches generate_content, each step runs exactly
    once, and plan order is respected. No real network or LLM call is made.
    """
    competitor_url = "https://competitor.example.com/pricing"
    competitor_repository, workspace_id = _setup_competitor_repository(tmp_path)
    saved_competitor = _run(competitor_repository.add_competitor(workspace_id, competitor_url))

    fetch_calls: list[str] = []

    def fake_fetch_public_source_sync(url: str) -> FetchedPublicSource:
        fetch_calls.append(url)
        return FetchedPublicSource(
            url=url,
            final_url=url,
            title="Competitor Pricing",
            text="Competitor sells budget tours to Turkey starting at $299.",
            content_type="text/html",
        )

    monkeypatch.setattr(
        executors_module, "fetch_public_source_sync", fake_fetch_public_source_sync,
    )

    llm_provider = FakeLLMProvider(
        analysis=SourceAnalysisPayload(
            summary="Competitor undercuts our Turkey package pricing.",
            key_facts=("Starting price $299", "Focus on budget travelers"),
            disputed_claims=(),
            audience_value="Price-sensitive travelers",
            target_audiences=("budget travelers",),
            content_angles=("value comparison",),
            recommended_formats=("post",),
            warnings=(),
        ),
        draft=ContentDraft(
            text="Here is how we can differentiate from this competitor on value, not price.",
            warnings=(),
        ),
    )

    context = PlannerExecutionContext(
        workspace_id=workspace_id,
        llm_provider=llm_provider,
        competitor_repository=competitor_repository,
    )

    # step_2 carries NO url - only a competitor_id selector. The real URL
    # can only reach fetch_public_source via step_1's runtime result.
    raw_plan = _plan([
        _step("step_1", "list_competitors"),
        _step(
            "step_2", "fetch_public_source",
            input={"competitor_id": saved_competitor.id}, depends_on=["step_1"],
        ),
        _step("step_3", "analyze_source", depends_on=["step_2"]),
        _step("step_4", "generate_content", depends_on=["step_3"]),
    ])

    result = _run(run_planner_plan(raw_plan, context=context))

    assert result.success is True, result.error
    assert result.failed_step is None
    assert result.completed_steps == ("step_1", "step_2", "step_3", "step_4")

    # 1. the URL genuinely reached the fetch executor exactly once, resolved
    #    from step_1's runtime result via competitor_id - not hardcoded.
    assert fetch_calls == [competitor_url]

    # 2. list_competitors genuinely returned the workspace's saved URL.
    assert result.step_results["step_1"]["competitors"][0]["url"] == competitor_url
    assert result.step_results["step_1"]["competitors"][0]["id"] == saved_competitor.id

    # 3. the extracted text genuinely reached analyze_source.
    llm_provider.analyze_source.assert_called_once_with(
        source_text="Competitor sells budget tours to Turkey starting at $299.",
    )

    # 4. the analysis result genuinely reached generate_content.
    llm_provider.generate_draft.assert_called_once()
    _, generate_kwargs = llm_provider.generate_draft.call_args
    assert "Competitor undercuts our Turkey package pricing." in generate_kwargs["source_text"]
    assert "Starting price $299" in generate_kwargs["source_text"]

    # 5. the final step result is the actionable draft the scenario needs.
    assert result.step_results["step_4"]["text"] == (
        "Here is how we can differentiate from this competitor on value, not price."
    )
