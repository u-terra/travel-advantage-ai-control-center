"""Stage 3.1 cost-guard scenarios: counts ACTUAL paid LLM calls per Planner
run end to end (planner_provider.plan() calls + business llm_provider
calls), not just unit-level budget mechanics (see test_planner_cost.py for
those).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.planner.executors as executors_module
from app.domain.partners import WorkspaceContext
from app.handlers.tasks import on_free_text
from app.planner.cost import DEFAULT_MAX_LLM_CALLS_PER_PLANNER_RUN, LLMCallBudget
from app.planner.errors import PlannerExecutionError
from app.planner.fetch import FetchedPublicSource
from app.planner.provider import PlannerLLMProvider
from app.services.llm.models import ContentDraft, SourceAnalysisPayload
from tests.llm_fakes import FakeLLMProvider


def run(value):
    return asyncio.run(value)


class Message:
    def __init__(self, text: str, *, telegram_user_id: int | None = 100):
        self.text = text
        self.from_user = SimpleNamespace(id=telegram_user_id) if telegram_user_id is not None else None
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


def journal():
    return SimpleNamespace(add=AsyncMock(return_value=1), last=AsyncMock())


def context(workspace_id: int = 42, telegram_user_id: int = 100) -> WorkspaceContext:
    return WorkspaceContext(telegram_user_id, workspace_id, "owner", "active")


def profile_repository(profile=None):
    return SimpleNamespace(
        get_business_profile=AsyncMock(return_value=profile),
        get_user_preferences=AsyncMock(return_value=None),
    )


class _FakeCompetitorRepository:
    def __init__(self, competitors=None):
        self._competitors = competitors or []

    async def list_for_workspace(self, workspace_id, limit=20):
        return self._competitors[:limit]


class _Competitor:
    def __init__(self, id, url, label):
        self.id = id
        self.url = url
        self.label = label


class _CountingPlannerProvider(PlannerLLMProvider):
    """Counts real plan() calls - the one paid Planner-side call per run."""

    name = "fake"

    def __init__(self, *, plan_result):
        self._plan_result = plan_result
        self.calls = 0

    @property
    def is_configured(self) -> bool:
        return True

    def plan(self, *, request):
        self.calls += 1
        return self._plan_result


def _plan(steps, goal="goal", final_output="final output"):
    return {"goal": goal, "reason": "reason", "steps": steps, "final_output": final_output}


def _step(id, executor, input=None, depends_on=None):
    return {"id": id, "action": "do it", "executor": executor, "input": input or {}, "depends_on": depends_on or []}


def _call_on_free_text(
    message, *, planner_provider, llm_provider=None, competitor_repository=None,
    max_llm_calls=DEFAULT_MAX_LLM_CALLS_PER_PLANNER_RUN,
):
    return run(on_free_text(
        message,
        journal(),
        llm_provider or FakeLLMProvider(),
        context(),
        profile_repository(),
        planner_llm_provider=planner_provider,
        planner_enabled=True,
        planner_allowed_telegram_user_ids=frozenset({100}),
        planner_max_llm_calls=max_llm_calls,
        competitor_repository=competitor_repository,
    ))


# ── A: simple request -> zero Planner calls at all ──────────────────────────


def test_A_simple_request_makes_zero_planner_calls():
    """The old router still legitimately calls the business LLM (generate_draft,
    and possibly its own content-safety check_text) for a plain post request -
    that is unrelated to Planner and must keep happening exactly as before.
    What Stage 3.1 must guarantee is that NONE of that comes from Planner:
    plan() is never called, and analyze_source (which nothing in the old
    router ever calls) is never touched."""
    planner_provider = _CountingPlannerProvider(plan_result=_plan([_step("s", "list_competitors")]))
    llm_provider = FakeLLMProvider(draft=ContentDraft(text="draft", warnings=()))
    message = Message("Напиши пост про раннее бронирование отеля в Турции для инстаграма")

    _call_on_free_text(message, planner_provider=planner_provider, llm_provider=llm_provider)

    assert planner_provider.calls == 0
    llm_provider.analyze_source.assert_not_called()


# ── B: competitor by label -> exactly 2 paid LLM calls (plan + synthesis) ──


def test_B_competitor_by_label_costs_exactly_two_llm_calls(monkeypatch):
    monkeypatch.setattr(
        executors_module, "fetch_public_source_sync",
        lambda url: FetchedPublicSource(
            url=url, final_url=url, title="ТурКлуб", text="ТурКлуб продаёт дешёвые туры.", content_type="text/html",
        ),
    )
    plan = _plan([
        _step("step_1", "list_competitors"),
        _step("step_2", "fetch_public_source", input={"competitor_label": "ТурКлуб"}, depends_on=["step_1"]),
    ])
    planner_provider = _CountingPlannerProvider(plan_result=plan)
    llm_provider = FakeLLMProvider(draft=ContentDraft(text="Актуальный разбор конкурента.", warnings=()))
    competitor_repository = _FakeCompetitorRepository([_Competitor(1, "https://turclub.example/", "ТурКлуб")])

    message = Message("Проанализируй конкурента ТурКлуб и скажи, что мне делать лучше него")
    _call_on_free_text(
        message, planner_provider=planner_provider, llm_provider=llm_provider,
        competitor_repository=competitor_repository,
    )

    assert planner_provider.calls == 1  # plan()
    llm_provider.generate_draft.assert_called_once()  # synthesis, reusing business LLM
    llm_provider.analyze_source.assert_not_called()  # no redundant intermediate step
    assert any("Актуальный разбор конкурента." in text for text, _ in message.answers)


# ── C: competitor by direct URL -> exactly 2 paid LLM calls ─────────────────


def test_C_competitor_by_direct_url_costs_exactly_two_llm_calls(monkeypatch):
    monkeypatch.setattr(
        executors_module, "fetch_public_source_sync",
        lambda url: FetchedPublicSource(
            url=url, final_url=url, title="Example", text="Example page content about pricing.", content_type="text/html",
        ),
    )
    plan = _plan([_step("step_1", "fetch_public_source", input={"url": "https://example.com"})])
    planner_provider = _CountingPlannerProvider(plan_result=plan)
    llm_provider = FakeLLMProvider(draft=ContentDraft(text="Отстройка от example.com готова.", warnings=()))

    message = Message("Проанализируй https://example.com и предложи, как нам отстроиться")
    _call_on_free_text(message, planner_provider=planner_provider, llm_provider=llm_provider)

    assert planner_provider.calls == 1
    llm_provider.generate_draft.assert_called_once()
    llm_provider.analyze_source.assert_not_called()


# ── D: ordinary complex content task -> at most 2 in the safe case ─────────


def test_D_ordinary_content_task_costs_at_most_two_llm_calls():
    plan = _plan([_step("step_1", "generate_content", input={"task_text": "Напиши пост про акцию"})])
    planner_provider = _CountingPlannerProvider(plan_result=plan)
    llm_provider = FakeLLMProvider(draft=ContentDraft(text="Готовый пост про акцию.", warnings=()))

    message = Message("Напиши пост про акцию, затем проверь его на риски и подготовь комплект для партнёра")
    _call_on_free_text(message, planner_provider=planner_provider, llm_provider=llm_provider)

    total_business_calls = (
        llm_provider.generate_draft.call_count
        + llm_provider.analyze_source.call_count
        + llm_provider.check_text.call_count
    )
    assert planner_provider.calls == 1
    assert total_business_calls <= 1  # generate_content only - synthesis skipped (ready prose)
    assert planner_provider.calls + total_business_calls <= 2


# ── E: research task -> at most 2 in the standard scenario ──────────────────


def test_E_research_task_costs_at_most_two_llm_calls(monkeypatch):
    monkeypatch.setattr(executors_module, "fetch_signals_sync", lambda config, *, limit: [])

    from app.domain.work import DailyActions, NextAction
    import app.handlers.tasks as tasks_module

    class _FakeDailyActionsService:
        def __init__(self, *a, **kw):
            pass

        async def build(self, workspace_id, *, now=None):
            return DailyActions(
                actions=(NextAction(source="cold_contact_fallback", headline="h", detail="d"),),
                waiting=(), recent_resolved_content=(),
            )

    monkeypatch.setattr(tasks_module, "DailyActionsService", _FakeDailyActionsService)

    from app.services.lead_radar import LeadRadarConfig

    plan = _plan([_step("step_1", "rank_signals"), _step("step_2", "next_best_action")])
    planner_provider = _CountingPlannerProvider(plan_result=plan)
    llm_provider = FakeLLMProvider(draft=ContentDraft(text="Выводы по рынку готовы.", warnings=()))

    message = Message("Проведи многошаговое исследование рынка Турции по нескольким источникам и сделай выводы")
    result = run(on_free_text(
        message, journal(), llm_provider, context(), profile_repository(),
        planner_llm_provider=planner_provider, planner_enabled=True,
        planner_allowed_telegram_user_ids=frozenset({100}),
        lead_radar_config=LeadRadarConfig(db_path=Path("unused.db")),
        work_repository=object(), artifact_repository=object(),
    ))

    total_business_calls = (
        llm_provider.generate_draft.call_count
        + llm_provider.analyze_source.call_count
        + llm_provider.check_text.call_count
    )
    assert planner_provider.calls == 1
    assert total_business_calls <= 1
    assert planner_provider.calls + total_business_calls <= 2


# ── F: hard cap - a 5th LLM call is physically impossible ───────────────────


def test_F_fifth_llm_call_is_physically_impossible_at_default_cap():
    budget = LLMCallBudget(max_calls=DEFAULT_MAX_LLM_CALLS_PER_PLANNER_RUN)
    for label in ("plan", "a", "b", "c"):
        budget.consume(label=label)
    with pytest.raises(PlannerExecutionError):
        budget.consume(label="one_too_many")
    assert budget.used == DEFAULT_MAX_LLM_CALLS_PER_PLANNER_RUN


def test_F_plan_exceeding_the_configured_cap_fails_closed_end_to_end():
    """5 LLM-calling steps + plan() = 6 > default cap (4): the run must fail
    closed (fall back to the router) rather than silently exceed the budget."""
    plan = _plan([
        _step("step_1", "generate_content", input={"task_text": "a"}),
        _step("step_2", "check_safety", input={"text": "a"}),
        _step("step_3", "generate_content", input={"task_text": "b"}),
        _step("step_4", "check_safety", input={"text": "b"}),
        _step("step_5", "generate_content", input={"task_text": "c"}),
    ])
    planner_provider = _CountingPlannerProvider(plan_result=plan)
    llm_provider = FakeLLMProvider(draft=ContentDraft(text="draft", warnings=()))

    message = Message("Проанализируй конкурента ТурКлуб и скажи, что мне делать лучше него")
    _call_on_free_text(message, planner_provider=planner_provider, llm_provider=llm_provider)

    # Old router still answered (fallback), and the business LLM was never
    # called more than the configured cap allows minus the plan() call.
    assert llm_provider.generate_draft.call_count <= DEFAULT_MAX_LLM_CALLS_PER_PLANNER_RUN - 1


# ── G: synthesis is never called after a ready generate_content draft ──────


def test_G_no_synthesis_call_after_ready_generate_content_draft():
    plan = _plan([_step("step_1", "generate_content", input={"task_text": "Напиши пост"})])
    planner_provider = _CountingPlannerProvider(plan_result=plan)
    llm_provider = FakeLLMProvider(draft=ContentDraft(text="Готовый пост.", warnings=()))

    message = Message("Проанализируй конкурента ТурКлуб и скажи, что мне делать лучше него")
    _call_on_free_text(message, planner_provider=planner_provider, llm_provider=llm_provider)

    assert llm_provider.generate_draft.call_count == 1  # generate_content only, no extra synthesis call


# ── H: user outside allowlist -> zero Planner LLM calls ─────────────────────


def test_H_user_outside_allowlist_makes_zero_planner_llm_calls():
    planner_provider = _CountingPlannerProvider(plan_result=_plan([_step("s", "list_competitors")]))
    llm_provider = FakeLLMProvider()
    message = Message("Проанализируй конкурента ТурКлуб и скажи, что мне делать лучше него", telegram_user_id=999)

    run(on_free_text(
        message, journal(), llm_provider, context(), profile_repository(),
        planner_llm_provider=planner_provider, planner_enabled=True,
        planner_allowed_telegram_user_ids=frozenset({100}),  # 999 not in it
    ))

    assert planner_provider.calls == 0
    llm_provider.generate_draft.assert_not_called()
    llm_provider.analyze_source.assert_not_called()
    llm_provider.check_text.assert_not_called()
