from __future__ import annotations

import inspect

import pytest

from app.domain.action_contract import ActionContract
from app.routing.modules import Module
from app.routing.router import RouteDecision
from app.routing.safety import SafetyLevel
from app.services.action_contract_adapter import ResolvedFlow, build_action_contract


def decision(
    module: Module,
    *,
    text: str = "arbitrary text that the adapter must not classify",
    uncertain: bool = False,
) -> RouteDecision:
    return RouteDecision(
        task_text=text,
        primary_module=module,
        secondary_modules=(),
        safety_level=SafetyLevel.NOT_REQUIRED,
        is_mixed=False,
        is_uncertain=uncertain,
        matched_modules=() if uncertain else (module,),
        notes=(),
    )


@pytest.mark.parametrize(
    ("module", "intent", "action", "flow"),
    [
        (Module.TRAVEL_ASSISTANT, "answer_client", "answer_travel_question", "travel_advantage_mwr_life"),
        (Module.PARTNER_PACKAGING, "package_partner_material", "build_partner_package", "partner_packaging"),
        (Module.CONTENT_FACTORY, "create_content", "generate_content", "content_factory"),
        (Module.LEAD_RADAR, "inspect_leads", "show_lead_signals", "radar"),
        (Module.SAFETY_LAYER, "check_claim", "check_text_safety", "safety"),
        (Module.ORCHESTRATOR, "clarify_request", "request_clarification", "generic"),
    ],
)
def test_production_module_mapping_is_deterministic(
    module: Module, intent: str, action: str, flow: str,
) -> None:
    route = decision(module)
    first = build_action_contract(route)
    second = build_action_contract(route)

    assert first == second
    assert isinstance(first, ActionContract)
    assert first.intent == intent
    assert first.action == action
    assert first.slots["flow"] == flow
    assert first.slots["primary_module"] == module.value


def test_travel_assistant_uses_only_module_justified_broad_domain() -> None:
    contract = build_action_contract(decision(Module.TRAVEL_ASSISTANT))
    assert contract.slots["knowledge_domain"] == "travel_advantage_mwr_life"
    assert "need_knowledge" not in contract.slots


@pytest.mark.parametrize(
    "module", [Module.CONTENT_FACTORY, Module.LEAD_RADAR, Module.ORCHESTRATOR],
)
def test_non_kb_flows_do_not_become_travel_knowledge(module: Module) -> None:
    contract = build_action_contract(decision(module))
    assert "knowledge_domain" not in contract.slots
    assert "need_knowledge" not in contract.slots
    assert contract.slots["primary_module"] == module.value


def test_uncertain_generic_never_becomes_travel_flow() -> None:
    route = decision(
        Module.ORCHESTRATOR,
        text="Travel Advantage words do not cause reclassification here",
        uncertain=True,
    )
    contract = build_action_contract(route)
    assert contract.intent == "clarify_request"
    assert contract.action == "request_clarification"
    assert contract.slots["flow"] == "generic"
    assert contract.confidence == 0.0
    assert "knowledge_domain" not in contract.slots


@pytest.mark.parametrize(
    ("flow", "intent", "action"),
    [
        (ResolvedFlow.PLANNER, "execute_plan", "run_planner"),
        (ResolvedFlow.SOURCE_ANALYSIS, "analyze_source", "analyze_source"),
    ],
)
def test_explicit_non_module_flow_is_caller_supplied(
    flow: ResolvedFlow, intent: str, action: str,
) -> None:
    route = decision(Module.ORCHESTRATOR)
    contract = build_action_contract(route, flow=flow)
    assert contract.intent == intent
    assert contract.action == action
    assert contract.slots["flow"] == flow.value
    assert contract.slots["primary_module"] == Module.ORCHESTRATOR.value
    assert "knowledge_domain" not in contract.slots


def test_text_content_cannot_change_primary_module_or_contract() -> None:
    one = build_action_contract(decision(Module.CONTENT_FACTORY, text="Travel Advantage"))
    two = build_action_contract(decision(Module.CONTENT_FACTORY, text="Ruby income"))
    assert one == two
    assert one.slots["primary_module"] == Module.CONTENT_FACTORY.value


def test_adapter_has_no_llm_or_knowledge_dependencies() -> None:
    module = inspect.getmodule(build_action_contract)
    assert module is not None
    source = inspect.getsource(module)
    assert "KnowledgeService" not in source
    assert "KnowledgeRepository" not in source
    assert "LLMProvider" not in source
    assert not inspect.iscoroutinefunction(build_action_contract)


def test_subject_reference_and_source_are_passed_without_persistence_logic() -> None:
    contract = build_action_contract(
        decision(Module.LEAD_RADAR),
        source="button",
        subject_ref_type="radar_signal",
        subject_ref_id=42,
    )
    assert contract.source == "button"
    assert contract.subject_ref_type == "radar_signal"
    assert contract.subject_ref_id == 42


def test_invalid_flow_type_is_rejected_instead_of_guessed() -> None:
    with pytest.raises(TypeError):
        build_action_contract(decision(Module.ORCHESTRATOR), flow="planner")  # type: ignore[arg-type]
