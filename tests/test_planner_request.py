from __future__ import annotations

from app.planner.plan import ALLOWED_EXECUTORS
from app.planner.request import PlannerRequest, build_planner_request
from app.routing.modules import Module
from app.routing.router import route_text


def test_build_planner_request_minimal():
    request = build_planner_request("Проанализируй конкурента ТурКлуб")
    assert isinstance(request, PlannerRequest)
    assert request.task_text == "Проанализируй конкурента ТурКлуб"
    assert request.business_context == {}
    assert request.advisory_route is None
    assert set(request.executor_catalog) == ALLOWED_EXECUTORS
    assert len(request.system_rules) > 0


def test_advisory_route_is_included_when_router_is_confident():
    decision = route_text("Напиши пост про Турцию")
    request = build_planner_request("Напиши пост про Турцию", advisory_route_decision=decision)
    assert request.advisory_route == Module.CONTENT_FACTORY.value


def test_advisory_route_is_none_when_router_is_uncertain():
    decision = route_text("бла бла бла непонятно что")
    assert decision.is_uncertain is True
    request = build_planner_request("бла бла бла непонятно что", advisory_route_decision=decision)
    assert request.advisory_route is None


def test_business_context_is_compact_dict_of_strings():
    from app.domain.business_profiles import BusinessProfile

    class _FakeContext:
        positioning = {"statement": "Лучшие туры в Турцию для всей семьи, доступные цены."}
        claims = ()

    profile = BusinessProfile.__new__(BusinessProfile)  # bypass full construction
    object.__setattr__(profile, "business_type", "travel_agency")
    object.__setattr__(profile, "ta_affiliated", True)
    object.__setattr__(profile, "context", _FakeContext())

    request = build_planner_request("Проанализируй конкурента X", business_profile=profile)
    assert request.business_context["business_type"] == "travel_agency"
    assert request.business_context["ta_affiliated"] == "true"
    assert "Лучшие туры" in request.business_context["positioning"]


def test_system_rules_do_not_leak_secrets_or_full_prompts():
    request = build_planner_request("Проанализируй конкурента X")
    joined = " ".join(request.system_rules)
    assert "api_key" not in joined.lower()
    assert "token" not in joined.lower()


# ── Stage 3.1 cost-conscious prompt guidance ────────────────────────────────


def test_system_rules_mark_free_vs_paid_executors():
    request = build_planner_request("Проанализируй конкурента X")
    joined = " ".join(request.system_rules)
    assert "FREE" in joined
    assert "list_competitors" in joined
    assert "fetch_public_source" in joined


def test_system_rules_discourage_redundant_intermediate_llm_steps():
    request = build_planner_request("Проанализируй конкурента X")
    joined = " ".join(request.system_rules)
    assert "analyze_source" in joined
    assert "synthesis" in joined.lower()


def test_system_rules_discourage_default_check_safety():
    request = build_planner_request("Проанализируй конкурента X")
    joined = " ".join(request.system_rules)
    assert "check_safety" in joined


def test_executor_catalog_marks_llm_cost_per_executor():
    """Cost signal lives on the catalog too, not only in system_rules - the
    model sees it right next to each executor's description."""
    request = build_planner_request("Проанализируй конкурента X")
    assert "no llm call" in request.executor_catalog["list_competitors"].lower()
    assert "no llm call" in request.executor_catalog["fetch_public_source"].lower()
    assert "one llm call" in request.executor_catalog["analyze_source"].lower()
    assert "one llm call" in request.executor_catalog["generate_content"].lower()
    assert "one llm call" in request.executor_catalog["check_safety"].lower()
