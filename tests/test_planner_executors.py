from __future__ import annotations

from pathlib import Path

import pytest

import app.planner.executors as executors_module
from app.domain.competitors import Competitor
from app.domain.work import DailyActions, NextAction
from app.planner.context import PlannerExecutionContext
from app.planner.cost import LLMCallBudget
from app.planner.executors import PLANNER_EXECUTORS, PlannerExecutionError
from app.planner.fetch import FetchedPublicSource, PublicSourceFetchError
from app.planner.plan import ALLOWED_EXECUTORS
from app.services.lead_radar import LeadRadarConfig, LeadSignal
from app.services.llm.models import (
    ContentDraft,
    SourceAnalysisPayload,
    TextCheckResult,
    TextSafetyFinding,
)
from tests.llm_fakes import FakeLLMProvider


def _run(coro):
    import asyncio

    return asyncio.run(coro)


class _FakeCompetitorRepository:
    def __init__(self, competitors: list[Competitor]) -> None:
        self._competitors = competitors
        self.calls: list[tuple[int, int]] = []

    async def list_for_workspace(self, workspace_id: int, limit: int = 20):
        self.calls.append((workspace_id, limit))
        return self._competitors[:limit]


class _FakeDailyActionsService:
    def __init__(self, result: DailyActions) -> None:
        self._result = result
        self.calls: list[int] = []

    async def build(self, workspace_id: int, *, now: str | None = None) -> DailyActions:
        self.calls.append(workspace_id)
        return self._result


def _context(**overrides) -> PlannerExecutionContext:
    base = dict(workspace_id=1)
    base.update(overrides)
    return PlannerExecutionContext(**base)


def _call(executor, *, step_input=None, dependency_results=None, context=None):
    return _run(
        executor(
            step_input=step_input or {},
            dependency_results=dependency_results or {},
            context=context or _context(),
        )
    )


def test_registry_matches_closed_executor_contract():
    """Not relying on the import-time assert in app.planner.executors (which
    -O would strip) - the registry must exactly match the Phase 1 contract."""
    assert set(PLANNER_EXECUTORS) == ALLOWED_EXECUTORS


# ── list_competitors ────────────────────────────────────────────────────────


def test_list_competitors_returns_serialized_workspace_competitors():
    repo = _FakeCompetitorRepository(
        [Competitor(id=1, workspace_id=7, url="https://a.example", label="https://a.example", created_at="t")]
    )
    result = _call(
        PLANNER_EXECUTORS["list_competitors"],
        context=_context(workspace_id=7, competitor_repository=repo),
    )
    assert result == {"competitors": [{"id": 1, "url": "https://a.example", "label": "https://a.example"}]}
    assert repo.calls == [(7, 20)]


def test_list_competitors_respects_input_limit():
    repo = _FakeCompetitorRepository([])
    _call(
        PLANNER_EXECUTORS["list_competitors"],
        step_input={"limit": 3},
        context=_context(competitor_repository=repo),
    )
    assert repo.calls == [(1, 3)]


def test_list_competitors_missing_repository_is_controlled_error():
    with pytest.raises(PlannerExecutionError, match="competitor_repository"):
        _call(PLANNER_EXECUTORS["list_competitors"], context=_context())


# ── fetch_public_source ─────────────────────────────────────────────────────


def test_fetch_public_source_requires_url_in_input():
    with pytest.raises(PlannerExecutionError, match="url"):
        _call(PLANNER_EXECUTORS["fetch_public_source"], step_input={})


def test_fetch_public_source_success(monkeypatch):
    monkeypatch.setattr(
        executors_module,
        "fetch_public_source_sync",
        lambda url: FetchedPublicSource(
            url=url, final_url=url, title="T", text="some extracted text", content_type="text/html",
        ),
    )
    result = _call(
        PLANNER_EXECUTORS["fetch_public_source"],
        step_input={"url": "https://competitor.example.com/"},
    )
    assert result["text"] == "some extracted text"
    assert result["url"] == "https://competitor.example.com/"


def test_fetch_public_source_wraps_fetch_error(monkeypatch):
    def _raise(url):
        raise PublicSourceFetchError("blocked host")

    monkeypatch.setattr(executors_module, "fetch_public_source_sync", _raise)
    with pytest.raises(PlannerExecutionError, match="blocked host"):
        _call(
            PLANNER_EXECUTORS["fetch_public_source"],
            step_input={"url": "http://localhost/"},
        )


def test_fetch_public_source_resolves_url_from_competitor_id_dependency(monkeypatch):
    """Phase 2.1 fix: the TaskPlan is built BEFORE list_competitors actually
    runs, so the LLM cannot know a real competitor URL ahead of time. This
    proves the URL is genuinely derived from the runtime dependency result,
    not from step_input directly."""
    captured_urls: list[str] = []

    def fake_fetch(url: str) -> FetchedPublicSource:
        captured_urls.append(url)
        return FetchedPublicSource(
            url=url, final_url=url, title="T", text="fetched via competitor_id", content_type="text/html",
        )

    monkeypatch.setattr(executors_module, "fetch_public_source_sync", fake_fetch)
    result = _call(
        PLANNER_EXECUTORS["fetch_public_source"],
        step_input={"competitor_id": 42},
        dependency_results={
            "step_1": {"competitors": [
                {"id": 7, "url": "https://other.example/", "label": "other"},
                {"id": 42, "url": "https://picked.example/", "label": "picked"},
            ]},
        },
    )
    assert captured_urls == ["https://picked.example/"]
    assert result["url"] == "https://picked.example/"
    assert result["text"] == "fetched via competitor_id"


def test_fetch_public_source_url_wins_over_competitor_id_when_both_present(monkeypatch):
    monkeypatch.setattr(
        executors_module, "fetch_public_source_sync",
        lambda url: FetchedPublicSource(url=url, final_url=url, title="T", text="x", content_type="text/html"),
    )
    result = _call(
        PLANNER_EXECUTORS["fetch_public_source"],
        step_input={"url": "https://direct.example/", "competitor_id": 1},
        dependency_results={"step_1": {"competitors": [{"id": 1, "url": "https://ignored.example/", "label": "x"}]}},
    )
    assert result["url"] == "https://direct.example/"


def test_fetch_public_source_competitor_id_not_found_is_controlled_error():
    with pytest.raises(PlannerExecutionError, match="competitor_id"):
        _call(
            PLANNER_EXECUTORS["fetch_public_source"],
            step_input={"competitor_id": 999},
            dependency_results={"step_1": {"competitors": [{"id": 1, "url": "https://x/", "label": "x"}]}},
        )


def test_fetch_public_source_competitor_id_with_missing_url_field_is_controlled_error():
    with pytest.raises(PlannerExecutionError, match="no url"):
        _call(
            PLANNER_EXECUTORS["fetch_public_source"],
            step_input={"competitor_id": 1},
            dependency_results={"step_1": {"competitors": [{"id": 1, "label": "no url here"}]}},
        )


def test_fetch_public_source_competitor_id_without_any_dependency_is_controlled_error():
    with pytest.raises(PlannerExecutionError):
        _call(
            PLANNER_EXECUTORS["fetch_public_source"],
            step_input={"competitor_id": 1},
            dependency_results={},
        )


def test_fetch_public_source_competitor_id_path_ignores_non_list_dependency_shape():
    with pytest.raises(PlannerExecutionError, match="competitors"):
        _call(
            PLANNER_EXECUTORS["fetch_public_source"],
            step_input={"competitor_id": 1},
            dependency_results={"step_1": {"text": "this is a fetch_public_source result, not list_competitors"}},
        )


def test_fetch_public_source_never_touches_repository_for_competitor_id_path(monkeypatch):
    """The competitor_id path must resolve purely from the declared
    dependency result - never fall back to context.competitor_repository,
    which would bypass depends_on entirely. context has no repository
    configured at all here, and the call still succeeds."""
    monkeypatch.setattr(
        executors_module, "fetch_public_source_sync",
        lambda url: FetchedPublicSource(url=url, final_url=url, title="T", text="ok", content_type="text/html"),
    )
    context = _context()  # no competitor_repository set
    assert context.competitor_repository is None
    result = _call(
        PLANNER_EXECUTORS["fetch_public_source"],
        step_input={"competitor_id": 5},
        dependency_results={"step_1": {"competitors": [{"id": 5, "url": "https://ok.example/", "label": "ok"}]}},
        context=context,
    )
    assert result["url"] == "https://ok.example/"


def test_fetch_public_source_resolves_url_from_competitor_label_dependency(monkeypatch):
    """Natural-language scenario: 'проанализируй конкурента ТурКлуб' - the
    Planner LLM has no numeric id, only the name the user typed."""
    captured_urls: list[str] = []

    def fake_fetch(url: str) -> FetchedPublicSource:
        captured_urls.append(url)
        return FetchedPublicSource(url=url, final_url=url, title="T", text="fetched by label", content_type="text/html")

    monkeypatch.setattr(executors_module, "fetch_public_source_sync", fake_fetch)
    result = _call(
        PLANNER_EXECUTORS["fetch_public_source"],
        step_input={"competitor_label": "ТурКлуб"},
        dependency_results={
            "step_1": {"competitors": [
                {"id": 1, "url": "https://other.example/", "label": "Другой конкурент"},
                {"id": 2, "url": "https://turclub.example/", "label": "ТурКлуб"},
            ]},
        },
    )
    assert captured_urls == ["https://turclub.example/"]
    assert result["url"] == "https://turclub.example/"


def test_fetch_public_source_competitor_label_match_is_case_insensitive_and_trimmed(monkeypatch):
    monkeypatch.setattr(
        executors_module, "fetch_public_source_sync",
        lambda url: FetchedPublicSource(url=url, final_url=url, title="T", text="ok", content_type="text/html"),
    )
    result = _call(
        PLANNER_EXECUTORS["fetch_public_source"],
        step_input={"competitor_label": "  турклуб  "},
        dependency_results={"step_1": {"competitors": [{"id": 2, "url": "https://turclub.example/", "label": "ТурКлуб"}]}},
    )
    assert result["url"] == "https://turclub.example/"


def test_fetch_public_source_competitor_label_not_found_is_controlled_error():
    with pytest.raises(PlannerExecutionError, match="competitor_label"):
        _call(
            PLANNER_EXECUTORS["fetch_public_source"],
            step_input={"competitor_label": "Несуществующий"},
            dependency_results={"step_1": {"competitors": [{"id": 1, "url": "https://x/", "label": "ТурКлуб"}]}},
        )


def test_fetch_public_source_competitor_label_ambiguous_match_is_controlled_error():
    with pytest.raises(PlannerExecutionError, match="ambiguous"):
        _call(
            PLANNER_EXECUTORS["fetch_public_source"],
            step_input={"competitor_label": "ТурКлуб"},
            dependency_results={"step_1": {"competitors": [
                {"id": 1, "url": "https://a.example/", "label": "ТурКлуб"},
                {"id": 2, "url": "https://b.example/", "label": "турклуб"},
            ]}},
        )


def test_fetch_public_source_competitor_id_wins_over_competitor_label():
    with pytest.raises(PlannerExecutionError, match="competitor_id"):
        # competitor_id takes precedence and is deliberately unresolvable
        # here, proving competitor_label is not silently used as a fallback.
        _call(
            PLANNER_EXECUTORS["fetch_public_source"],
            step_input={"competitor_id": 999, "competitor_label": "ТурКлуб"},
            dependency_results={"step_1": {"competitors": [{"id": 1, "url": "https://x/", "label": "ТурКлуб"}]}},
        )


def test_fetch_public_source_never_touches_repository_for_competitor_label_path(monkeypatch):
    monkeypatch.setattr(
        executors_module, "fetch_public_source_sync",
        lambda url: FetchedPublicSource(url=url, final_url=url, title="T", text="ok", content_type="text/html"),
    )
    context = _context()
    assert context.competitor_repository is None
    result = _call(
        PLANNER_EXECUTORS["fetch_public_source"],
        step_input={"competitor_label": "ТурКлуб"},
        dependency_results={"step_1": {"competitors": [{"id": 1, "url": "https://ok.example/", "label": "ТурКлуб"}]}},
        context=context,
    )
    assert result["url"] == "https://ok.example/"


# ── analyze_source ──────────────────────────────────────────────────────────


def _analysis_payload(summary: str = "Summary text") -> SourceAnalysisPayload:
    return SourceAnalysisPayload(
        summary=summary,
        key_facts=("fact1",),
        disputed_claims=(),
        audience_value="value",
        target_audiences=("agents",),
        content_angles=("angle",),
        recommended_formats=("post",),
        warnings=(),
    )


def test_analyze_source_uses_step_input_text():
    provider = FakeLLMProvider(analysis=_analysis_payload())
    result = _call(
        PLANNER_EXECUTORS["analyze_source"],
        step_input={"text": "direct text"},
        context=_context(llm_provider=provider),
    )
    provider.analyze_source.assert_called_once_with(source_text="direct text")
    assert result["summary"] == "Summary text"


def test_analyze_source_uses_dependency_text_when_input_missing():
    provider = FakeLLMProvider(analysis=_analysis_payload())
    result = _call(
        PLANNER_EXECUTORS["analyze_source"],
        dependency_results={"step_1": {"text": "fetched page text"}},
        context=_context(llm_provider=provider),
    )
    provider.analyze_source.assert_called_once_with(source_text="fetched page text")
    assert result["summary"] == "Summary text"


def test_analyze_source_missing_dependency_field_is_controlled_error():
    provider = FakeLLMProvider(analysis=_analysis_payload())
    with pytest.raises(PlannerExecutionError, match="text"):
        _call(
            PLANNER_EXECUTORS["analyze_source"],
            dependency_results={"step_1": {"title": "no text field here"}},
            context=_context(llm_provider=provider),
        )


def test_analyze_source_no_input_and_no_dependency_is_controlled_error():
    provider = FakeLLMProvider(analysis=_analysis_payload())
    with pytest.raises(PlannerExecutionError):
        _call(PLANNER_EXECUTORS["analyze_source"], context=_context(llm_provider=provider))


def test_analyze_source_requires_llm_provider():
    with pytest.raises(PlannerExecutionError, match="llm_provider"):
        _call(PLANNER_EXECUTORS["analyze_source"], step_input={"text": "x"}, context=_context())


def test_analyze_source_provider_none_result_is_controlled_error():
    provider = FakeLLMProvider(analysis=None)
    with pytest.raises(PlannerExecutionError, match="no result"):
        _call(
            PLANNER_EXECUTORS["analyze_source"],
            step_input={"text": "x"},
            context=_context(llm_provider=provider),
        )


def test_analyze_source_consumes_one_llm_call_from_the_budget():
    provider = FakeLLMProvider(analysis=_analysis_payload())
    budget = LLMCallBudget(max_calls=5)
    _call(
        PLANNER_EXECUTORS["analyze_source"],
        step_input={"text": "x"},
        context=_context(llm_provider=provider, llm_call_budget=budget),
    )
    assert budget.used == 1
    assert budget.calls == ("analyze_source",)


def test_analyze_source_exhausted_budget_is_a_controlled_error_and_skips_the_call():
    provider = FakeLLMProvider(analysis=_analysis_payload())
    budget = LLMCallBudget(max_calls=0)
    with pytest.raises(PlannerExecutionError, match="budget exceeded"):
        _call(
            PLANNER_EXECUTORS["analyze_source"],
            step_input={"text": "x"},
            context=_context(llm_provider=provider, llm_call_budget=budget),
        )
    provider.analyze_source.assert_not_called()


# ── generate_content ─────────────────────────────────────────────────────────


def test_generate_content_uses_task_text_from_input():
    provider = FakeLLMProvider(draft=ContentDraft(text="draft", warnings=()))
    result = _call(
        PLANNER_EXECUTORS["generate_content"],
        step_input={"task_text": "write about our tours"},
        context=_context(llm_provider=provider),
    )
    assert result == {"text": "draft", "warnings": []}
    provider.generate_draft.assert_called_once()


def test_generate_content_builds_task_text_from_dependency_summary():
    provider = FakeLLMProvider(draft=ContentDraft(text="draft from analysis", warnings=()))
    result = _call(
        PLANNER_EXECUTORS["generate_content"],
        dependency_results={"step_3": {"summary": "Competitor sells cheap tours", "key_facts": ["fact A"]}},
        context=_context(llm_provider=provider),
    )
    assert result["text"] == "draft from analysis"
    _, kwargs = provider.generate_draft.call_args
    assert "Competitor sells cheap tours" in kwargs["source_text"]
    assert "fact A" in kwargs["source_text"]


def test_generate_content_missing_dependency_summary_is_controlled_error():
    provider = FakeLLMProvider(draft=ContentDraft(text="x", warnings=()))
    with pytest.raises(PlannerExecutionError, match="summary"):
        _call(
            PLANNER_EXECUTORS["generate_content"],
            dependency_results={"step_3": {"no_summary_here": True}},
            context=_context(llm_provider=provider),
        )


def test_generate_content_provider_none_draft_is_controlled_error():
    provider = FakeLLMProvider(draft=None)
    with pytest.raises(PlannerExecutionError, match="no draft"):
        _call(
            PLANNER_EXECUTORS["generate_content"],
            step_input={"task_text": "x"},
            context=_context(llm_provider=provider),
        )


def test_generate_content_exhausted_budget_is_a_controlled_error_and_skips_the_call():
    provider = FakeLLMProvider(draft=ContentDraft(text="x", warnings=()))
    budget = LLMCallBudget(max_calls=0)
    with pytest.raises(PlannerExecutionError, match="budget exceeded"):
        _call(
            PLANNER_EXECUTORS["generate_content"],
            step_input={"task_text": "x"},
            context=_context(llm_provider=provider, llm_call_budget=budget),
        )
    provider.generate_draft.assert_not_called()


# ── check_safety ─────────────────────────────────────────────────────────────


def test_check_safety_uses_step_input_text():
    provider = FakeLLMProvider(
        check=TextCheckResult(
            warnings=(TextSafetyFinding(phrase="100%", warning="guaranteed outcome"),),
            rewritten_text=None,
            rewrite_warnings=(),
            generation_mode=None,
            ai_note=None,
        )
    )
    result = _call(
        PLANNER_EXECUTORS["check_safety"],
        step_input={"text": "some draft"},
        context=_context(llm_provider=provider),
    )
    provider.check_text.assert_called_once_with(source_text="some draft")
    assert result["warnings"] == [{"phrase": "100%", "warning": "guaranteed outcome"}]


def test_check_safety_uses_dependency_text_when_input_missing():
    provider = FakeLLMProvider(
        check=TextCheckResult(
            warnings=(), rewritten_text=None, rewrite_warnings=(), generation_mode=None, ai_note=None,
        )
    )
    _call(
        PLANNER_EXECUTORS["check_safety"],
        dependency_results={"step_4": {"text": "generated draft"}},
        context=_context(llm_provider=provider),
    )
    provider.check_text.assert_called_once_with(source_text="generated draft")


def test_check_safety_exhausted_budget_is_a_controlled_error_and_skips_the_call():
    provider = FakeLLMProvider(
        check=TextCheckResult(
            warnings=(), rewritten_text=None, rewrite_warnings=(), generation_mode=None, ai_note=None,
        )
    )
    budget = LLMCallBudget(max_calls=0)
    with pytest.raises(PlannerExecutionError, match="budget exceeded"):
        _call(
            PLANNER_EXECUTORS["check_safety"],
            step_input={"text": "x"},
            context=_context(llm_provider=provider, llm_call_budget=budget),
        )
    provider.check_text.assert_not_called()


# ── rank_signals ─────────────────────────────────────────────────────────────


def test_rank_signals_calls_fetch_signals_sync(monkeypatch):
    signal = LeadSignal(
        id=1, created_at="2026-01-01", source_type="reddit", score=80.0, category="lead_signal",
        title="Someone asking about Turkey", url="https://example.com/thread",
        recommended_action="careful_reply", action_label="Ответить", action_reason="high score",
    )
    calls: list[tuple[object, int]] = []

    def fake_fetch_signals_sync(config, *, limit):
        calls.append((config, limit))
        return [signal]

    monkeypatch.setattr(executors_module, "fetch_signals_sync", fake_fetch_signals_sync)
    config = LeadRadarConfig(db_path=Path("unused.db"))
    result = _call(
        PLANNER_EXECUTORS["rank_signals"],
        step_input={"limit": 2},
        context=_context(lead_radar_config=config),
    )
    assert calls == [(config, 2)]
    assert result["signals"][0]["title"] == "Someone asking about Turkey"


def test_rank_signals_requires_lead_radar_config():
    with pytest.raises(PlannerExecutionError, match="lead_radar_config"):
        _call(PLANNER_EXECUTORS["rank_signals"], context=_context())


def test_rank_signals_unavailable_lead_radar_is_controlled_error(monkeypatch):
    monkeypatch.setattr(executors_module, "fetch_signals_sync", lambda config, *, limit: None)
    with pytest.raises(PlannerExecutionError, match="unavailable"):
        _call(
            PLANNER_EXECUTORS["rank_signals"],
            context=_context(lead_radar_config=LeadRadarConfig(db_path=Path("unused.db"))),
        )


# ── next_best_action ─────────────────────────────────────────────────────────


def test_next_best_action_calls_daily_actions_service():
    daily_actions = DailyActions(
        actions=(NextAction(source="cold_contact_fallback", headline="Найдите нового клиента", detail="d"),),
        waiting=(),
        recent_resolved_content=(),
    )
    service = _FakeDailyActionsService(daily_actions)
    result = _call(
        PLANNER_EXECUTORS["next_best_action"],
        context=_context(workspace_id=9, daily_actions_service=service),
    )
    assert service.calls == [9]
    assert result["actions"][0]["headline"] == "Найдите нового клиента"


def test_next_best_action_requires_daily_actions_service():
    with pytest.raises(PlannerExecutionError, match="daily_actions_service"):
        _call(PLANNER_EXECUTORS["next_best_action"], context=_context())
