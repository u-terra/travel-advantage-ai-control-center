"""Persisted web-Assistant conversation history - server is the source of
truth, replacing sessionStorage.

Deliberately separate from app.domain.conversation_state:
ConversationState is orchestration working-state (what module/task is
active right now), not a message transcript - see that module's own
docstring. Nothing here reads or writes conversation_state, and nothing
in conversation_state reads or writes this.

Only the plain-text turn content is modeled - no rendered HTML, no
knowledge/business-profile/workspace-memory context payloads, no system
prompts. Those are request-time inputs to the LLM call, not part of the
conversation record.
"""

from __future__ import annotations

from dataclasses import dataclass

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
MESSAGE_ROLES = frozenset({ROLE_USER, ROLE_ASSISTANT})


@dataclass(frozen=True)
class WebConversation:
    id: int
    workspace_id: int
    telegram_user_id: int
    title: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class WebConversationMessage:
    id: int
    conversation_id: int
    role: str
    content: str
    created_at: str
