"""Deterministic adapter from an accepted route to ``ActionContract``.

This module does not route, classify text, call an LLM, or retrieve knowledge.
It only represents a decision already made by the existing triage layer in the
common CCF envelope.
"""

from __future__ import annotations

from enum import Enum

from app.domain.action_contract import ACTION_CONTRACT_SOURCES, ActionContract
from app.routing.modules import Module
from app.routing.router import RouteDecision


class ResolvedFlow(str, Enum):
    """Optional flow identity supplied by the already-decided caller.

    Planner and source analysis are not represented by distinct ``Module``
    values.  Their caller must therefore supply this signal; the adapter must
    never infer either flow from message text.
    """

    ROUTED_TEXT = "routed_text"
    PLANNER = "planner"
    SOURCE_ANALYSIS = "source_analysis"


_MODULE_CONTRACT: dict[Module, tuple[str, str, str]] = {
    Module.TRAVEL_ASSISTANT: (
        "answer_client", "answer_travel_question", "travel_advantage_mwr_life",
    ),
    Module.PARTNER_PACKAGING: (
        "package_partner_material", "build_partner_package", "partner_packaging",
    ),
    Module.CONTENT_FACTORY: (
        "create_content", "generate_content", "content_factory",
    ),
    Module.LEAD_RADAR: (
        "inspect_leads", "show_lead_signals", "radar",
    ),
    Module.SAFETY_LAYER: (
        "check_claim", "check_text_safety", "safety",
    ),
    Module.ORCHESTRATOR: (
        "clarify_request", "request_clarification", "generic",
    ),
}

_EXPLICIT_FLOW_CONTRACT: dict[ResolvedFlow, tuple[str, str, str]] = {
    ResolvedFlow.PLANNER: ("execute_plan", "run_planner", "planner"),
    ResolvedFlow.SOURCE_ANALYSIS: (
        "analyze_source", "analyze_source", "source_analysis",
    ),
}


def build_action_contract(
    decision: RouteDecision,
    *,
    flow: ResolvedFlow = ResolvedFlow.ROUTED_TEXT,
    source: str = "text",
    subject_ref_type: str | None = None,
    subject_ref_id: int | None = None,
) -> ActionContract:
    """Translate an existing triage decision without reconsidering it.

    ``decision.task_text`` is deliberately not inspected.  ``flow`` is only
    for a caller that already knows it is executing Planner/source-analysis,
    because those flows have no dedicated production ``Module`` value.
    """

    if not isinstance(decision, RouteDecision):
        raise TypeError("decision must be a RouteDecision")
    if not isinstance(flow, ResolvedFlow):
        raise TypeError("flow must be a ResolvedFlow")
    if source not in ACTION_CONTRACT_SOURCES:
        raise ValueError(f"unsupported action source: {source!r}")

    intent, action, flow_name = (
        _MODULE_CONTRACT[decision.primary_module]
        if flow is ResolvedFlow.ROUTED_TEXT
        else _EXPLICIT_FLOW_CONTRACT[flow]
    )
    slots: dict[str, object] = {
        "flow": flow_name,
        "primary_module": decision.primary_module.value,
        "safety_level": decision.safety_level.value,
        "is_uncertain": decision.is_uncertain,
    }
    if decision.primary_module is Module.TRAVEL_ASSISTANT:
        # This is the narrowest domain justified by the module itself.  The
        # route contains no deterministic signal for TA versus MWR Life.
        slots["knowledge_domain"] = "travel_advantage_mwr_life"

    return ActionContract(
        intent=intent,
        action=action,
        subject_ref_type=subject_ref_type,
        subject_ref_id=subject_ref_id,
        slots=slots,
        source=source,
        confidence=0.0 if decision.is_uncertain else 1.0,
    )
