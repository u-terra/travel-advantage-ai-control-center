from __future__ import annotations

import dataclasses
from types import MappingProxyType

import pytest

from app.domain.action_contract import ActionContract, ActionContractValidationError


def _contract(**overrides: object) -> ActionContract:
    fields = {
        "intent": "select_offer_item",
        "action": "generate_content",
        "subject_ref_type": "artifact",
        "subject_ref_id": 42,
        "slots": {"item_id": "3"},
        "source": "button",
        "confidence": 1.0,
    }
    fields.update(overrides)
    return ActionContract(**fields)  # type: ignore[arg-type]


def test_valid_contract_constructs() -> None:
    contract = _contract()
    assert contract.intent == "select_offer_item"
    assert contract.action == "generate_content"
    assert contract.subject_ref_type == "artifact"
    assert contract.subject_ref_id == 42
    assert contract.slots == {"item_id": "3"}
    assert contract.source == "button"
    assert contract.confidence == 1.0


def test_valid_contract_with_no_subject_ref() -> None:
    contract = _contract(subject_ref_type=None, subject_ref_id=None, slots={})
    assert contract.subject_ref_type is None
    assert contract.subject_ref_id is None


@pytest.mark.parametrize("confidence", [-0.1, 1.1, float("nan"), float("inf")])
def test_invalid_confidence_out_of_range_or_non_finite(confidence: float) -> None:
    with pytest.raises(ActionContractValidationError):
        _contract(confidence=confidence)


@pytest.mark.parametrize("confidence", ["high", None, True, False])
def test_invalid_confidence_wrong_type(confidence: object) -> None:
    with pytest.raises(ActionContractValidationError):
        _contract(confidence=confidence)


def test_confidence_boundary_values_are_valid() -> None:
    assert _contract(confidence=0.0).confidence == 0.0
    assert _contract(confidence=1.0).confidence == 1.0


@pytest.mark.parametrize(
    ("ref_type", "ref_id"),
    [
        ("artifact", None),
        (None, 42),
    ],
)
def test_half_defined_subject_ref_is_rejected(
    ref_type: str | None, ref_id: int | None
) -> None:
    with pytest.raises(ActionContractValidationError):
        _contract(subject_ref_type=ref_type, subject_ref_id=ref_id)


def test_subject_ref_id_must_be_a_positive_int() -> None:
    with pytest.raises(ActionContractValidationError):
        _contract(subject_ref_type="artifact", subject_ref_id=0)
    with pytest.raises(ActionContractValidationError):
        _contract(subject_ref_type="artifact", subject_ref_id=-1)
    with pytest.raises(ActionContractValidationError):
        _contract(subject_ref_type="artifact", subject_ref_id=True)  # bool rejected


def test_non_json_safe_slots_are_rejected() -> None:
    with pytest.raises(ActionContractValidationError):
        _contract(slots={"bad": object()})


def test_non_json_safe_slots_nested_are_rejected() -> None:
    with pytest.raises(ActionContractValidationError):
        _contract(slots={"nested": {"bad": {1, 2, 3}}})


def test_slots_non_finite_float_is_rejected() -> None:
    with pytest.raises(ActionContractValidationError):
        _contract(slots={"score": float("nan")})


def test_slots_must_be_a_mapping() -> None:
    with pytest.raises(ActionContractValidationError):
        _contract(slots=["not", "a", "mapping"])  # type: ignore[arg-type]


def test_slots_are_frozen_after_construction() -> None:
    contract = _contract(slots={"item_id": "3", "nested": {"a": 1}})
    assert isinstance(contract.slots, MappingProxyType)
    assert isinstance(contract.slots["nested"], MappingProxyType)
    with pytest.raises(TypeError):
        contract.slots["item_id"] = "changed"  # type: ignore[index]


@pytest.mark.parametrize("source", ["voice", "system", "", "TEXT", "Button"])
def test_invalid_source_values_are_rejected(source: str) -> None:
    with pytest.raises(ActionContractValidationError):
        _contract(source=source)


@pytest.mark.parametrize("source", ["text", "button"])
def test_valid_source_values_are_accepted(source: str) -> None:
    assert _contract(source=source).source == source


@pytest.mark.parametrize("intent", ["", "   ", None, 123])
def test_invalid_intent_is_rejected(intent: object) -> None:
    with pytest.raises(ActionContractValidationError):
        _contract(intent=intent)


@pytest.mark.parametrize("action", ["", "   ", None, 123])
def test_invalid_action_is_rejected(action: object) -> None:
    with pytest.raises(ActionContractValidationError):
        _contract(action=action)


def test_intent_and_action_are_stripped() -> None:
    contract = _contract(intent="  select_offer_item  ", action="  generate_content  ")
    assert contract.intent == "select_offer_item"
    assert contract.action == "generate_content"


def test_contract_is_frozen_immutable() -> None:
    contract = _contract()
    assert dataclasses.is_dataclass(contract)
    with pytest.raises(dataclasses.FrozenInstanceError):
        contract.intent = "something_else"  # type: ignore[misc]
