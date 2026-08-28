"""Conversation Core Foundation (F1) - domain types.

These types describe a persisted "working state" for a single
(workspace_id, telegram_user_id) - what the user and bot are currently doing,
what was last offered, and what question is still waiting for an answer.

F1 is infrastructure only: no handler reads or writes these types yet (see
app.repositories.conversation_state_repository and the F1 report). There is
no chat_id field anywhere - WorkspaceContext (app.domain.partners) already
has none, tenant resolution is telegram_user_id-only, and this codebase has
no group-chat support (no ChatType filters anywhere), so adding chat_id here
would be a field for a scenario that does not exist.

current_artifact_id is the only artifact reference kept here. Version
numbers (current/previous) are deliberately NOT cached in this module -
ArtifactRepository.get_current_artifact_version /
list_artifact_versions remain the single source of truth for "what version
is current". Caching a version number here would create a second, driftable
source of truth for something ArtifactRepository already answers correctly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

# Small, closed set of domain nouns a subject_ref can point at - mirrors
# app.domain.work.WORK_ITEM_REF_TYPES (also a small closed frozenset of
# existing nouns, not a prediction of future ones).
CONVERSATION_SUBJECT_REF_TYPES = frozenset({"work_subject", "competitor", "artifact"})

_TASK_MAX_LEN = 500
_MODULE_MAX_LEN = 100
_LAST_ACTION_MAX_LEN = 100
_OFFER_TYPE_MAX_LEN = 100
_QUESTION_TYPE_MAX_LEN = 100
_PROMPT_TEXT_MAX_LEN = 1000
_OFFER_ITEM_ID_MAX_LEN = 64
_OFFER_ITEM_LABEL_MAX_LEN = 200
_MAX_OFFER_ITEMS = 20


class ConversationStateValidationError(ValueError):
    """Raised when raw ConversationState fields do not satisfy the contract."""


class OfferValidationError(ValueError):
    """Raised when a raw OfferItem/PendingOffer does not satisfy the contract."""


class PendingQuestionValidationError(ValueError):
    """Raised when raw PendingQuestion fields do not satisfy the contract."""


def _freeze_json_value(value: Any, *, error: type[ValueError]) -> Any:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise error("payload/slot float values must be finite")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise error("payload/slot keys must be strings")
            frozen[key] = _freeze_json_value(item, error=error)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_value(item, error=error) for item in value)
    raise error(f"unsupported payload/slot value type: {type(value).__name__}")


def _require_positive_int(value: Any, field: str, *, error: type[ValueError]) -> None:
    if type(value) is not int or isinstance(value, bool) or value <= 0:
        raise error(f"{field} must be a positive integer")


def _require_optional_positive_int(
    value: Any, field: str, *, error: type[ValueError]
) -> None:
    if value is None:
        return
    _require_positive_int(value, field, error=error)


def _require_optional_bounded_str(
    value: Any, field: str, *, max_len: int, error: type[ValueError]
) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise error(f"{field} must be a non-empty string or None")
    if len(value) > max_len:
        raise error(f"{field} exceeds max length of {max_len}")


def _require_bounded_str(
    value: Any, field: str, *, max_len: int, error: type[ValueError]
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise error(f"{field} must be a non-empty string")
    stripped = value.strip()
    if len(stripped) > max_len:
        raise error(f"{field} exceeds max length of {max_len}")
    return stripped


def _require_ref_pair(
    ref_type: Any, ref_id: Any, *, error: type[ValueError]
) -> None:
    if (ref_type is None) != (ref_id is None):
        raise error(
            "subject ref type/id must both be set or both be None"
        )


@dataclass(frozen=True)
class ConversationState:
    """One row of persisted working state per (workspace_id, telegram_user_id)."""

    workspace_id: int
    telegram_user_id: int
    active_module: str | None
    current_task: str | None
    current_subject_ref_type: str | None
    current_subject_ref_id: int | None
    current_artifact_id: int | None
    last_action: str | None
    updated_at: str

    def __post_init__(self) -> None:
        error = ConversationStateValidationError
        _require_positive_int(self.workspace_id, "workspace_id", error=error)
        _require_positive_int(self.telegram_user_id, "telegram_user_id", error=error)
        _require_optional_bounded_str(
            self.active_module, "active_module", max_len=_MODULE_MAX_LEN, error=error
        )
        _require_optional_bounded_str(
            self.current_task, "current_task", max_len=_TASK_MAX_LEN, error=error
        )
        _require_ref_pair(
            self.current_subject_ref_type, self.current_subject_ref_id, error=error
        )
        if self.current_subject_ref_type is not None:
            if self.current_subject_ref_type not in CONVERSATION_SUBJECT_REF_TYPES:
                raise error(
                    "current_subject_ref_type must be one of "
                    f"{sorted(CONVERSATION_SUBJECT_REF_TYPES)}, "
                    f"got {self.current_subject_ref_type!r}"
                )
            _require_positive_int(
                self.current_subject_ref_id, "current_subject_ref_id", error=error
            )
        _require_optional_positive_int(
            self.current_artifact_id, "current_artifact_id", error=error
        )
        _require_optional_bounded_str(
            self.last_action, "last_action", max_len=_LAST_ACTION_MAX_LEN, error=error
        )
        if not isinstance(self.updated_at, str) or not self.updated_at.strip():
            raise error("updated_at must be a non-empty string")


@dataclass(frozen=True)
class OfferItem:
    """A single selectable item within a PendingOffer (e.g. one of 3 topics).

    ``id`` is the stable reference a future resolver will match against
    ("third" -> the item whose id is "3") - never the item's position in a
    re-rendered Telegram message.
    """

    id: str
    label: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        error = OfferValidationError
        object.__setattr__(
            self,
            "id",
            _require_bounded_str(self.id, "id", max_len=_OFFER_ITEM_ID_MAX_LEN, error=error),
        )
        object.__setattr__(
            self,
            "label",
            _require_bounded_str(
                self.label, "label", max_len=_OFFER_ITEM_LABEL_MAX_LEN, error=error
            ),
        )
        if not isinstance(self.payload, Mapping):
            raise error("payload must be a JSON object")
        object.__setattr__(self, "payload", _freeze_json_value(self.payload, error=error))


@dataclass(frozen=True)
class PendingOffer:
    """A structured, workspace/user-scoped offer (e.g. a list of 3 topics).

    Always represents a persisted row - ``id`` comes from
    ConversationStateRepository.create_offer, never constructed ahead of
    insertion (same convention as Competitor/Artifact elsewhere in this
    codebase).
    """

    id: int
    workspace_id: int
    telegram_user_id: int
    offer_type: str
    items: tuple[OfferItem, ...]
    created_at: str
    expires_at: str | None
    consumed_at: str | None

    def __post_init__(self) -> None:
        error = OfferValidationError
        _require_positive_int(self.id, "id", error=error)
        _require_positive_int(self.workspace_id, "workspace_id", error=error)
        _require_positive_int(self.telegram_user_id, "telegram_user_id", error=error)
        object.__setattr__(
            self,
            "offer_type",
            _require_bounded_str(
                self.offer_type, "offer_type", max_len=_OFFER_TYPE_MAX_LEN, error=error
            ),
        )
        if not isinstance(self.items, tuple) or not all(
            isinstance(item, OfferItem) for item in self.items
        ):
            raise error("items must be a tuple of OfferItem")
        if not self.items:
            raise error("a PendingOffer must contain at least one item")
        if len(self.items) > _MAX_OFFER_ITEMS:
            raise error(f"a PendingOffer must not exceed {_MAX_OFFER_ITEMS} items")
        item_ids = [item.id for item in self.items]
        if len(set(item_ids)) != len(item_ids):
            raise error("OfferItem ids must be unique within one PendingOffer")
        if not isinstance(self.created_at, str) or not self.created_at.strip():
            raise error("created_at must be a non-empty string")
        _require_optional_bounded_str(
            self.expires_at, "expires_at", max_len=64, error=error
        )
        _require_optional_bounded_str(
            self.consumed_at, "consumed_at", max_len=64, error=error
        )


@dataclass(frozen=True)
class PendingQuestion:
    """A question the bot asked that is still waiting for an answer.

    Deliberately separate from recent_turns/history: this is a single,
    typed, expiring slot, not a transcript. Foundation (F1) has no producer
    for this type yet - handlers do not create/read it.
    """

    id: int
    workspace_id: int
    telegram_user_id: int
    question_type: str
    subject_ref_type: str | None
    subject_ref_id: int | None
    prompt_text: str
    created_at: str
    expires_at: str | None
    answered_at: str | None

    def __post_init__(self) -> None:
        error = PendingQuestionValidationError
        _require_positive_int(self.id, "id", error=error)
        _require_positive_int(self.workspace_id, "workspace_id", error=error)
        _require_positive_int(self.telegram_user_id, "telegram_user_id", error=error)
        object.__setattr__(
            self,
            "question_type",
            _require_bounded_str(
                self.question_type, "question_type",
                max_len=_QUESTION_TYPE_MAX_LEN, error=error,
            ),
        )
        _require_ref_pair(self.subject_ref_type, self.subject_ref_id, error=error)
        if self.subject_ref_type is not None:
            object.__setattr__(
                self,
                "subject_ref_type",
                _require_bounded_str(
                    self.subject_ref_type, "subject_ref_type", max_len=64, error=error
                ),
            )
            _require_positive_int(
                self.subject_ref_id, "subject_ref_id", error=error
            )
        object.__setattr__(
            self,
            "prompt_text",
            _require_bounded_str(
                self.prompt_text, "prompt_text",
                max_len=_PROMPT_TEXT_MAX_LEN, error=error,
            ),
        )
        if not isinstance(self.created_at, str) or not self.created_at.strip():
            raise error("created_at must be a non-empty string")
        _require_optional_bounded_str(
            self.expires_at, "expires_at", max_len=64, error=error
        )
        _require_optional_bounded_str(
            self.answered_at, "answered_at", max_len=64, error=error
        )
