from __future__ import annotations

from app.planner.provider import NullPlannerLLMProvider, PlannerLLMProvider
from app.planner.request import build_planner_request


def test_null_provider_is_not_configured():
    provider = NullPlannerLLMProvider()
    assert provider.is_configured is False


def test_null_provider_plan_returns_none():
    provider = NullPlannerLLMProvider()
    request = build_planner_request("Проанализируй конкурента")
    assert provider.plan(request=request) is None


def test_null_provider_is_an_instance_of_the_contract():
    assert isinstance(NullPlannerLLMProvider(), PlannerLLMProvider)
