"""OrchestrationDecision v2 — structured output contract for the LLM router.

Deliberately reuses ``Module``/``SafetyLevel`` from ``app.routing`` instead of
inventing a parallel vocabulary: shadow mode only means something if "old
decision" and "LLM decision" speak the same language (see
``app.orchestration.shadow.compute_agreement``).

The LLM does not execute anything at this stage - it only decides intent and
routing. ``reason_code`` is a short machine token (e.g. "leading_rewrite_verb"),
never a prose explanation / chain-of-thought.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.routing.modules import Module
from app.routing.safety import SafetyLevel


class OrchestrationIntent(StrEnum):
    REWRITE = "rewrite"
    CREATE_CONTENT = "create_content"
    CHECK_SAFETY = "check_safety"
    ANALYZE_SOURCE = "analyze_source"
    ANSWER_CLIENT = "answer_client"
    # Case C: "почему ты предлагаешь этот никчёмный повод..." after a Radar
    # idea - a reaction to a PAST result, not a new content request.
    FEEDBACK_ON_PREVIOUS_RESULT = "feedback_on_previous_result"
    FIND_SIGNALS = "find_signals"
    PACKAGE_FOR_PARTNER = "package_for_partner"
    CLARIFY = "clarify"
    OTHER = "other"


class InvalidOrchestrationDecisionError(ValueError):
    """Raised when the raw LLM output does not satisfy the strict contract.

    Callers must treat this as a fail-closed signal: skip the shadow
    comparison for this turn, never guess a decision, never let it affect the
    user-facing reply (the old router already has).
    """


_REASON_CODE_MAX_LEN = 64
_CONFIDENCE_RANGE = (0.0, 1.0)


@dataclass(frozen=True)
class OrchestrationDecision:
    intent: OrchestrationIntent
    primary_module: Module
    secondary_modules: tuple[Module, ...]
    safety_required: bool
    uses_previous_turn: bool
    needs_source_analysis: bool
    needs_generation: bool
    needs_clarification: bool
    confidence: float
    reason_code: str

    @property
    def safety_level(self) -> SafetyLevel:
        """Comparable projection onto the old router's SafetyLevel scale."""
        return SafetyLevel.MANDATORY if self.safety_required else SafetyLevel.NOT_REQUIRED


def _require_dict(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise InvalidOrchestrationDecisionError("decision must be a JSON object")
    return raw


def _require_enum(raw: dict[str, Any], key: str, enum_cls: type) -> Any:
    value = raw.get(key)
    if not isinstance(value, str):
        raise InvalidOrchestrationDecisionError(f"{key} must be a string")
    try:
        return enum_cls(value)
    except ValueError as exc:
        raise InvalidOrchestrationDecisionError(f"{key} has unknown value {value!r}") from exc


def _require_bool(raw: dict[str, Any], key: str) -> bool:
    value = raw.get(key)
    if not isinstance(value, bool):
        raise InvalidOrchestrationDecisionError(f"{key} must be a boolean")
    return value


def _require_modules(raw: dict[str, Any], key: str) -> tuple[Module, ...]:
    value = raw.get(key)
    if value is None:
        return ()
    if not isinstance(value, list):
        raise InvalidOrchestrationDecisionError(f"{key} must be a list")
    modules: list[Module] = []
    for item in value:
        if not isinstance(item, str):
            raise InvalidOrchestrationDecisionError(f"{key} entries must be strings")
        try:
            modules.append(Module(item))
        except ValueError as exc:
            raise InvalidOrchestrationDecisionError(f"{key} has unknown module {item!r}") from exc
    return tuple(modules)


def _require_confidence(raw: dict[str, Any]) -> float:
    value = raw.get("confidence")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidOrchestrationDecisionError("confidence must be a number")
    value = float(value)
    low, high = _CONFIDENCE_RANGE
    if not (low <= value <= high):
        raise InvalidOrchestrationDecisionError("confidence must be within [0, 1]")
    return value


def _require_reason_code(raw: dict[str, Any]) -> str:
    value = raw.get("reason_code")
    if not isinstance(value, str) or not value.strip():
        raise InvalidOrchestrationDecisionError("reason_code must be a non-empty string")
    value = value.strip()
    # Fail-closed against chain-of-thought / prose leaking in: a reason_code
    # is a short machine token, never a sentence or multi-line explanation.
    if "\n" in value or len(value) > _REASON_CODE_MAX_LEN:
        raise InvalidOrchestrationDecisionError(
            "reason_code must be a single short token, not an explanation"
        )
    return value


def parse_orchestration_decision(raw: Any) -> OrchestrationDecision:
    """Strictly validates and converts raw (already JSON-decoded) LLM output.

    Raises :class:`InvalidOrchestrationDecisionError` on any deviation from
    the contract - unknown enum values, wrong types, missing fields,
    out-of-range confidence, or a reason_code that looks like prose. There is
    no lenient/partial-parse path: a decision we cannot fully trust is not a
    decision, it is a shadow-mode "invalid_output" event (see
    ``app.orchestration.shadow``).
    """
    data = _require_dict(raw)
    return OrchestrationDecision(
        intent=_require_enum(data, "intent", OrchestrationIntent),
        primary_module=_require_enum(data, "primary_module", Module),
        secondary_modules=_require_modules(data, "secondary_modules"),
        safety_required=_require_bool(data, "safety_required"),
        uses_previous_turn=_require_bool(data, "uses_previous_turn"),
        needs_source_analysis=_require_bool(data, "needs_source_analysis"),
        needs_generation=_require_bool(data, "needs_generation"),
        needs_clarification=_require_bool(data, "needs_clarification"),
        confidence=_require_confidence(data),
        reason_code=_require_reason_code(data),
    )
