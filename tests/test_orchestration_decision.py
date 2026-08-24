from __future__ import annotations

import pytest

from app.orchestration.decision import (
    InvalidOrchestrationDecisionError,
    OrchestrationIntent,
    parse_orchestration_decision,
)
from app.routing.modules import Module
from app.routing.safety import SafetyLevel


def _raw(**overrides):
    base = dict(
        intent="rewrite",
        primary_module=Module.CONTENT_FACTORY.value,
        secondary_modules=[],
        safety_required=False,
        uses_previous_turn=False,
        needs_source_analysis=False,
        needs_generation=True,
        needs_clarification=False,
        confidence=0.9,
        reason_code="leading_rewrite_verb",
    )
    base.update(overrides)
    return base


def test_valid_decision_round_trips():
    decision = parse_orchestration_decision(_raw())
    assert decision.intent is OrchestrationIntent.REWRITE
    assert decision.primary_module is Module.CONTENT_FACTORY
    assert decision.secondary_modules == ()
    assert decision.confidence == 0.9
    assert decision.reason_code == "leading_rewrite_verb"
    assert decision.safety_level is SafetyLevel.NOT_REQUIRED


def test_safety_required_projects_to_mandatory_safety_level():
    decision = parse_orchestration_decision(_raw(safety_required=True, intent="check_safety"))
    assert decision.safety_level is SafetyLevel.MANDATORY


def test_secondary_modules_parsed_from_list():
    decision = parse_orchestration_decision(
        _raw(secondary_modules=[Module.SAFETY_LAYER.value])
    )
    assert decision.secondary_modules == (Module.SAFETY_LAYER,)


@pytest.mark.parametrize("raw", [None, "not a dict", 42, ["list"]])
def test_non_dict_input_is_rejected(raw):
    with pytest.raises(InvalidOrchestrationDecisionError):
        parse_orchestration_decision(raw)


def test_unknown_intent_is_rejected():
    with pytest.raises(InvalidOrchestrationDecisionError):
        parse_orchestration_decision(_raw(intent="do_whatever_the_user_wants"))


def test_unknown_module_is_rejected():
    with pytest.raises(InvalidOrchestrationDecisionError):
        parse_orchestration_decision(_raw(primary_module="Some Made Up Module"))


def test_unknown_secondary_module_is_rejected():
    with pytest.raises(InvalidOrchestrationDecisionError):
        parse_orchestration_decision(_raw(secondary_modules=["Not A Real Module"]))


@pytest.mark.parametrize("key", [
    "safety_required", "uses_previous_turn", "needs_source_analysis",
    "needs_generation", "needs_clarification",
])
def test_non_boolean_flags_are_rejected(key):
    with pytest.raises(InvalidOrchestrationDecisionError):
        parse_orchestration_decision(_raw(**{key: "true"}))


@pytest.mark.parametrize("confidence", [-0.1, 1.1, "high", True, None])
def test_confidence_out_of_range_or_wrong_type_is_rejected(confidence):
    with pytest.raises(InvalidOrchestrationDecisionError):
        parse_orchestration_decision(_raw(confidence=confidence))


def test_confidence_boundary_values_are_accepted():
    assert parse_orchestration_decision(_raw(confidence=0.0)).confidence == 0.0
    assert parse_orchestration_decision(_raw(confidence=1.0)).confidence == 1.0


def test_empty_reason_code_is_rejected():
    with pytest.raises(InvalidOrchestrationDecisionError):
        parse_orchestration_decision(_raw(reason_code=""))


def test_missing_reason_code_is_rejected():
    raw = _raw()
    del raw["reason_code"]
    with pytest.raises(InvalidOrchestrationDecisionError):
        parse_orchestration_decision(raw)


def test_multiline_reason_code_is_rejected_as_chain_of_thought():
    """reason_code must be a short machine token, never model prose/CoT."""
    with pytest.raises(InvalidOrchestrationDecisionError):
        parse_orchestration_decision(_raw(
            reason_code="Let me think step by step.\nFirst, I notice that...",
        ))


def test_overly_long_reason_code_is_rejected():
    with pytest.raises(InvalidOrchestrationDecisionError):
        parse_orchestration_decision(_raw(reason_code="x" * 65))


def test_missing_required_field_is_rejected():
    raw = _raw()
    del raw["needs_generation"]
    with pytest.raises(InvalidOrchestrationDecisionError):
        parse_orchestration_decision(raw)
