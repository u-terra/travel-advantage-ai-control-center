"""Lightweight 👍/👎 feedback on a single assistant chat message - never a
copy of the conversation itself (see app.repositories.feedback_repository:
only workspace_id/web_user_id/conversation_id/message_id are stored as
reference, plus the rating and a short optional reason/comment)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class FeedbackRating(str, Enum):
    UP = "up"
    DOWN = "down"


class FeedbackStatus(str, Enum):
    NEW = "new"
    REVIEWED = "reviewed"
    RESOLVED = "resolved"


# Fixed vocabulary shown to the user after 👎 - "other" is the only one
# that unlocks the free-text comment field client-side.
FEEDBACK_REASON_CODES = frozenset({
    "not_understood", "wrong_answer", "too_generic", "too_long", "too_short", "other",
})


@dataclass(frozen=True)
class AssistantFeedback:
    id: int
    workspace_id: int
    web_user_id: int
    conversation_id: int
    message_id: int
    rating: FeedbackRating
    reason: str | None
    comment: str | None
    status: FeedbackStatus
    created_at: str
    updated_at: str
