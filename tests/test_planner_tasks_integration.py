"""Integration: the Planner path wired into app.handlers.tasks.on_free_text.

Mirrors the fakes/pattern in tests/test_orchestration_integration.py (the
existing shadow-mode integration test) - a minimal fake aiogram Message/
FSMContext, no real Telegram/network/LLM call anywhere.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.planner.executors as executors_module
from app.domain.partners import WorkspaceContext
from app.handlers.tasks import on_free_text
from app.planner.fetch import FetchedPublicSource
from app.planner.provider import NullPlannerLLMProvider, PlannerLLMProvider
from app.services.llm.models import ContentDraft, SourceAnalysisPayload, TextCheckResult, TextSafetyFinding
from tests.llm_fakes import FakeLLMProvider


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

    async def clear(self):
        self.data = {}
        self.state = None


class Message:
    def __init__(self, text: str, *, telegram_user_id: int | None = 100, events: list[str] | None = None):
        self.text = text
        self.from_user = SimpleNamespace(id=telegram_user_id) if telegram_user_id is not None else None
        self.events = events if events is not None else []
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        self.events.append(f"reply:{text[:30]}")


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
        self.calls: list[int] = []

    async def list_for_workspace(self, workspace_id, limit=20):
        self.calls.append(workspace_id)
        return self._competitors[:limit]


class _Competitor:
    def __init__(self, id, url, label):
        self.id = id
        self.url = url
        self.label = label


class _FakePlannerProvider(PlannerLLMProvider):
    name = "fake"

    def __init__(self, *, plan_result=None, raises: Exception | None = None, configured: bool = True):
        self._plan_result = plan_result
        self._raises = raises
        self._configured = configured
        self.calls = 0

    @property
    def is_configured(self) -> bool:
        return self._configured

    def plan(self, *, request):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._plan_result


def _plan(steps, goal="goal", final_output="final output"):
    return {"goal": goal, "reason": "reason", "steps": steps, "final_output": final_output}


def _step(id, executor, input=None, depends_on=None):
    return {"id": id, "action": "do it", "executor": executor, "input": input or {}, "depends_on": depends_on or []}


ALLOWLIST = frozenset({100})


def _call_on_free_text(
    message, *, planner_provider=None, planner_enabled=True,
    allowed_ids=ALLOWLIST, llm_provider=None, competitor_repository=None,
    lead_radar_config=None, work_repository=None, artifact_repository=None,
    state=None,
):
    return run(on_free_text(
        message,
        journal(),
        llm_provider or FakeLLMProvider(),
        context(),
        profile_repository(),
        planner_llm_provider=planner_provider,
        planner_enabled=planner_enabled,
        planner_allowed_telegram_user_ids=allowed_ids,
        competitor_repository=competitor_repository,
        lead_radar_config=lead_radar_config,
        work_repository=work_repository,
        artifact_repository=artifact_repository,
        state=state,
    ))


# ── A/K: eligibility false - Planner never invoked, old flow unaffected ────


def test_A_simple_message_is_not_eligible_planner_never_called():
    planner_provider = _FakePlannerProvider(plan_result=_plan([_step("s", "list_competitors")]))
    message = Message("Напиши пост про раннее бронирование отеля в Турции для инстаграма")
    _call_on_free_text(message, planner_provider=planner_provider)
    assert planner_provider.calls == 0
    assert message.answers  # old router still replied


def test_K_write_a_post_is_not_eligible():
    planner_provider = _FakePlannerProvider(plan_result=_plan([_step("s", "list_competitors")]))
    message = Message("Напиши пост про Турцию")
    _call_on_free_text(message, planner_provider=planner_provider)
    assert planner_provider.calls == 0
    assert message.answers


# ── B: flag false - identical behavior to before Planner existed ──────────


def test_B_flag_disabled_never_calls_planner_even_if_eligible():
    planner_provider = _FakePlannerProvider(plan_result=_plan([_step("s", "list_competitors")]))
    message = Message("Проанализируй конкурента ТурКлуб и скажи, что мне делать лучше него")
    _call_on_free_text(message, planner_provider=planner_provider, planner_enabled=False)
    assert planner_provider.calls == 0
    assert message.answers


def test_B_user_not_in_allowlist_never_calls_planner_no_extra_llm_calls():
    planner_provider = _FakePlannerProvider(plan_result=_plan([_step("s", "list_competitors")]))
    message = Message(
        "Проанализируй конкурента ТурКлуб и скажи, что мне делать лучше него",
        telegram_user_id=999,
    )
    _call_on_free_text(message, planner_provider=planner_provider, allowed_ids=frozenset({100}))
    assert planner_provider.calls == 0
    assert message.answers  # old router still handled it


def test_B_empty_allowlist_denies_everyone_even_with_flag_enabled():
    planner_provider = _FakePlannerProvider(plan_result=_plan([_step("s", "list_competitors")]))
    message = Message("Проанализируй конкурента ТурКлуб и скажи, что мне делать лучше него")
    _call_on_free_text(message, planner_provider=planner_provider, allowed_ids=frozenset())
    assert planner_provider.calls == 0


# ── C: provider unavailable -----------------------------------------------


def test_C_unconfigured_provider_falls_back_without_calling_plan():
    planner_provider = _FakePlannerProvider(plan_result=_plan([_step("s", "list_competitors")]), configured=False)
    message = Message("Проанализируй конкурента ТурКлуб и скажи, что мне делать лучше него")
    _call_on_free_text(message, planner_provider=planner_provider)
    assert planner_provider.calls == 0
    assert message.answers


def test_C_null_provider_falls_back():
    message = Message("Проанализируй конкурента ТурКлуб и скажи, что мне делать лучше него")
    _call_on_free_text(message, planner_provider=NullPlannerLLMProvider())
    assert message.answers


# ── D/E: invalid plan / unknown executor -----------------------------------


def test_D_invalid_planner_json_falls_back_to_router():
    planner_provider = _FakePlannerProvider(plan_result={"garbage": True})
    message = Message("Проанализируй конкурента ТурКлуб и скажи, что мне делать лучше него")
    _call_on_free_text(message, planner_provider=planner_provider)
    assert planner_provider.calls == 1
    assert message.answers  # old router still produced a reply


def test_E_unknown_executor_in_plan_falls_back_to_router():
    plan = _plan([_step("step_1", "not_a_real_executor")])
    planner_provider = _FakePlannerProvider(plan_result=plan)
    message = Message("Проанализируй конкурента ТурКлуб и скажи, что мне делать лучше него")
    _call_on_free_text(message, planner_provider=planner_provider)
    assert message.answers


def test_provider_raising_falls_back_without_crashing_handler():
    planner_provider = _FakePlannerProvider(raises=RuntimeError("boom"))
    message = Message("Проанализируй конкурента ТурКлуб и скажи, что мне делать лучше него")
    _call_on_free_text(message, planner_provider=planner_provider)
    assert message.answers


# ── F: executor failure -----------------------------------------------------


def test_F_executor_failure_falls_back_to_router_without_crashing():
    plan = _plan([
        _step("step_1", "fetch_public_source", input={"url": "http://localhost/admin"}),
    ])
    planner_provider = _FakePlannerProvider(plan_result=plan)
    message = Message("Проанализируй https://example.com и предложи, как нам отстроиться")
    _call_on_free_text(message, planner_provider=planner_provider)
    assert message.answers  # fell back, still replied, no crash


# ── G: successful competitor scenario BY LABEL -------------------------------


def test_G_competitor_analysis_by_label_end_to_end(monkeypatch):
    fetch_calls: list[str] = []

    def fake_fetch(url: str) -> FetchedPublicSource:
        fetch_calls.append(url)
        return FetchedPublicSource(
            url=url, final_url=url, title="ТурКлуб", text="ТурКлуб продаёт дешёвые туры в Турцию.",
            content_type="text/html",
        )

    monkeypatch.setattr(executors_module, "fetch_public_source_sync", fake_fetch)

    plan = _plan([
        _step("step_1", "list_competitors"),
        _step("step_2", "fetch_public_source", input={"competitor_label": "ТурКлуб"}, depends_on=["step_1"]),
        _step("step_3", "analyze_source", depends_on=["step_2"]),
        _step("step_4", "generate_content", depends_on=["step_3"]),
    ])
    planner_provider = _FakePlannerProvider(plan_result=plan)
    llm_provider = FakeLLMProvider(
        analysis=SourceAnalysisPayload(
            summary="ТурКлуб демпингует по цене", key_facts=("дешёвые туры",),
            disputed_claims=(), audience_value="v", target_audiences=(),
            content_angles=(), recommended_formats=(), warnings=(),
        ),
        draft=ContentDraft(text="Вот конкретный план действий против ТурКлуб.", warnings=()),
    )
    competitor_repository = _FakeCompetitorRepository([_Competitor(1, "https://turclub.example/", "ТурКлуб")])

    message = Message("Проанализируй конкурента ТурКлуб и скажи, что мне делать лучше него")
    _call_on_free_text(
        message, planner_provider=planner_provider, llm_provider=llm_provider,
        competitor_repository=competitor_repository,
    )

    assert fetch_calls == ["https://turclub.example/"]  # resolved by label, no id known ahead of time
    assert competitor_repository.calls == [42]
    reply_texts = [text for text, _kwargs in message.answers]
    assert any("конкретный план действий против ТурКлуб" in text for text in reply_texts)
    # N: exactly one ack + one final reply, no technical step spam
    assert len(message.answers) == 2


# ── H: URL scenario - list_competitors not required --------------------------


def test_H_direct_url_scenario_does_not_need_list_competitors(monkeypatch):
    fetch_calls: list[str] = []

    def fake_fetch(url: str) -> FetchedPublicSource:
        fetch_calls.append(url)
        return FetchedPublicSource(
            url=url, final_url=url, title="Example", text="Example competitor page content is here.",
            content_type="text/html",
        )

    monkeypatch.setattr(executors_module, "fetch_public_source_sync", fake_fetch)

    plan = _plan([
        _step("step_1", "fetch_public_source", input={"url": "https://example.com"}),
        _step("step_2", "analyze_source", depends_on=["step_1"]),
        _step("step_3", "generate_content", depends_on=["step_2"]),
    ])
    planner_provider = _FakePlannerProvider(plan_result=plan)
    llm_provider = FakeLLMProvider(
        analysis=SourceAnalysisPayload(
            summary="Example positions on price", key_facts=(), disputed_claims=(),
            audience_value="v", target_audiences=(), content_angles=(), recommended_formats=(), warnings=(),
        ),
        draft=ContentDraft(text="Отстройка от example.com готова.", warnings=()),
    )
    competitor_repository = _FakeCompetitorRepository()

    message = Message("Проанализируй https://example.com и предложи, как нам отстроиться")
    _call_on_free_text(
        message, planner_provider=planner_provider, llm_provider=llm_provider,
        competitor_repository=competitor_repository,
    )

    assert fetch_calls == ["https://example.com"]
    assert competitor_repository.calls == []  # never needed
    reply_texts = [text for text, _kwargs in message.answers]
    assert any("Отстройка от example.com готова." in text for text in reply_texts)


# ── I: complex content task --------------------------------------------------


def test_I_complex_content_task_write_check_package():
    plan = _plan([
        _step("step_1", "generate_content", input={"task_text": "Напиши пост про акцию"}),
        _step("step_2", "check_safety", depends_on=["step_1"]),
    ])
    planner_provider = _FakePlannerProvider(plan_result=plan)
    llm_provider = FakeLLMProvider(
        draft=ContentDraft(text="Черновик поста про акцию.", warnings=()),
        check=TextCheckResult(
            warnings=(TextSafetyFinding(phrase="скидка", warning="проверить условия"),),
            rewritten_text="Безопасная версия поста про акцию.",
            rewrite_warnings=(), generation_mode=None, ai_note=None,
        ),
    )
    message = Message("Напиши пост про акцию, затем проверь его на риски и подготовь комплект для партнёра")
    _call_on_free_text(message, planner_provider=planner_provider, llm_provider=llm_provider)

    reply_texts = [text for text, _kwargs in message.answers]
    assert any("Безопасная версия поста про акцию." in text for text in reply_texts)


# ── J: multi-step research task ----------------------------------------------


def test_J_multistep_research_task_uses_synthesis(monkeypatch):
    plan = _plan([
        _step("step_1", "rank_signals"),
        _step("step_2", "next_best_action"),
    ])
    planner_provider = _FakePlannerProvider(plan_result=plan)
    llm_provider = FakeLLMProvider(draft=ContentDraft(text="Итоговые выводы по рынку Турции.", warnings=()))

    from app.domain.work import DailyActions, NextAction
    import app.handlers.tasks as tasks_module

    class _FakeDailyActionsService:
        def __init__(self, *args, **kwargs):
            pass

        async def build(self, workspace_id, *, now=None):
            return DailyActions(
                actions=(NextAction(source="cold_contact_fallback", headline="h", detail="d"),),
                waiting=(), recent_resolved_content=(),
            )

    monkeypatch.setattr(tasks_module, "DailyActionsService", _FakeDailyActionsService)

    from pathlib import Path

    from app.services.lead_radar import LeadRadarConfig

    monkeypatch.setattr(
        "app.planner.executors.fetch_signals_sync", lambda config, *, limit: [],
    )

    message = Message(
        "Проведи многошаговое исследование рынка Турции по нескольким источникам и сделай выводы"
    )
    _call_on_free_text(
        message, planner_provider=planner_provider, llm_provider=llm_provider,
        lead_radar_config=LeadRadarConfig(db_path=Path("unused.db")),
        work_repository=object(), artifact_repository=object(),
    )

    reply_texts = [text for text, _kwargs in message.answers]
    assert any("Итоговые выводы по рынку Турции." in text for text in reply_texts)


# ── L: synthesis error --------------------------------------------------------


def test_L_synthesis_error_falls_back_without_crashing(monkeypatch):
    plan = _plan([_step("step_1", "list_competitors")])
    planner_provider = _FakePlannerProvider(plan_result=plan)

    async def _boom(**kwargs):
        raise RuntimeError("synthesis exploded")

    monkeypatch.setattr("app.planner.service.build_final_reply", _boom)

    message = Message("Проанализируй конкурента ТурКлуб и скажи, что мне делать лучше него")
    _call_on_free_text(message, planner_provider=planner_provider)
    assert message.answers  # old router still replied, no crash


# ── M: unexpected error deep in execution never crashes the handler ---------


def test_M_unexpected_error_never_crashes_the_handler(monkeypatch):
    def _explode(url):
        raise ValueError("totally unexpected failure")

    monkeypatch.setattr(executors_module, "fetch_public_source_sync", _explode)

    plan = _plan([_step("step_1", "fetch_public_source", input={"url": "https://example.com"})])
    planner_provider = _FakePlannerProvider(plan_result=plan)

    message = Message("Проанализируй https://example.com и предложи, как нам отстроиться")
    _call_on_free_text(message, planner_provider=planner_provider)
    assert message.answers  # controlled fallback, not an unhandled exception
