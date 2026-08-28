"""Unified Action Contract (F1) - infrastructural type only.

ActionContract is a typed, validated envelope for "what the user or a button
meant" - intent + which executor/business flow to call + which object it
refers to + free-form slots. It carries no business logic.

F1 explicitly does NOT include:
  - a resolver that turns Telegram text into an ActionContract (no ordinal
    word lists like "third"/"another"/"shorter" - that is a later stage);
  - a callback_data adapter that turns a button press into an ActionContract;
  - any handler wiring.

``intent``/``action`` are deliberately open (bounded, validated strings), not
a closed enum: F1 has no producers yet, so hard-coding the future set of
intents here would be exactly the kind of "layer for the sake of a layer"
the design review rejected. ``source`` IS a closed set, because it only ever
describes update provenance (text vs. button), which is fixed by Telegram's
own update model, not by future business decisions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

# Update provenance only - not an intent taxonomy. Telegram updates are
# either a text message or a callback button; there is no third case.
ACTION_CONTRACT_SOURCES = frozenset({"text", "button"})

_INTENT_MAX_LEN = 100
_ACTION_MAX_LEN = 100
_SUBJECT_REF_TYPE_MAX_LEN = 64


class ActionContractValidationError(ValueError):
    """Raised when raw ActionContract fields do not satisfy the contract."""


def _freeze_json_value(value: Any) -> Any:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ActionContractValidationError("slot float values must be finite")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ActionContractValidationError("slot keys must be strings")
            frozen[key] = _freeze_json_value(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_value(item) for item in value)
    raise ActionContractValidationError(
        f"unsupported slot value type: {type(value).__name__}"
    )


def _require_bounded_str(value: Any, field: str, *, max_len: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ActionContractValidationError(f"{field} must be a non-empty string")
    stripped = value.strip()
    if len(stripped) > max_len:
        raise ActionContractValidationError(f"{field} exceeds max length of {max_len}")
    return stripped


@dataclass(frozen=True)
class ActionContract:
    """Infrastructural, validated description of a decided intent.

    Both a button press and matching free text are expected to eventually
    produce an equal (or equivalent) ActionContract - but F1 ships only the
    validated envelope, not the code that builds one from either source.
    """

    intent: str
    action: str
    subject_ref_type: str | None
    subject_ref_id: int | None
    slots: Mapping[str, Any]
    source: str
    confidence: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "intent", _require_bounded_str(self.intent, "intent", max_len=_INTENT_MAX_LEN)
        )
        object.__setattr__(
            self, "action", _require_bounded_str(self.action, "action", max_len=_ACTION_MAX_LEN)
        )
        if self.source not in ACTION_CONTRACT_SOURCES:
            raise ActionContractValidationError(
                f"source must be one of {sorted(ACTION_CONTRACT_SOURCES)}, "
                f"got {self.source!r}"
            )
        if (
            not isinstance(self.confidence, (int, float))
            or isinstance(self.confidence, bool)
            or not math.isfinite(float(self.confidence))
        ):
            raise ActionContractValidationError("confidence must be a finite number")
        confidence = float(self.confidence)
        if not (0.0 <= confidence <= 1.0):
            raise ActionContractValidationError("confidence must be within [0.0, 1.0]")
        object.__setattr__(self, "confidence", confidence)

        if (self.subject_ref_type is None) != (self.subject_ref_id is None):
            raise ActionContractValidationError(
                "subject_ref_type and subject_ref_id must both be set or both be None"
            )
        if self.subject_ref_type is not None:
            object.__setattr__(
                self,
                "subject_ref_type",
                _require_bounded_str(
                    self.subject_ref_type, "subject_ref_type",
                    max_len=_SUBJECT_REF_TYPE_MAX_LEN,
                ),
            )
            if (
                type(self.subject_ref_id) is not int
                or isinstance(self.subject_ref_id, bool)
                or self.subject_ref_id <= 0
            ):
                raise ActionContractValidationError(
                    "subject_ref_id must be a positive integer"
                )

        if not isinstance(self.slots, Mapping):
            raise ActionContractValidationError("slots must be a JSON object")
        object.__setattr__(self, "slots", _freeze_json_value(self.slots))
