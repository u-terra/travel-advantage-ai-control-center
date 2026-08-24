"""Small rolling window of recent turns, backed by the existing FSM storage.

No new database/table: ``app.main`` already runs aiogram with
``MemoryStorage``, which keeps ``state.get_data()``/``update_data()`` per
(chat, user) for the process lifetime, independent of the current FSM
*state* value. That is exactly what a lightweight "does the LLM router see
the previous assistant result" mechanism needs for Phase 1, without touching
``journal.sqlite3`` (which logs routing decisions, not conversation turns,
and is production-shared).

Known limitation (intentional, called out in the Phase 1 report): this is
ephemeral - lost on process restart, not shared with the SQLite audit trail.
Good enough to prove the mechanism; a persisted version is Phase 2 work if
shadow-mode results justify it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiogram.fsm.context import FSMContext

_TURNS_STATE_KEY = "orchestration_recent_turns"
# 6 entries ~= last 3 user/assistant exchanges - enough for "what did you just
# show me" without shipping a growing chat log to every LLM call.
_MAX_TURNS = 6
# Assistant recaps are short labels ("Radar предложил..."), not full drafts -
# keeps the prompt compact and avoids re-sending generated content back to
# the model as if it were new input.
_MAX_TURN_TEXT_LEN = 300


@dataclass(frozen=True)
class ConversationTurn:
    role: str  # "user" | "assistant"
    text: str
    module: str | None = None


def _serialize(turn: ConversationTurn) -> dict[str, Any]:
    return {"role": turn.role, "text": turn.text, "module": turn.module}


def _deserialize(raw: Any) -> ConversationTurn | None:
    if not isinstance(raw, dict):
        return None
    role, text = raw.get("role"), raw.get("text")
    if role not in ("user", "assistant") or not isinstance(text, str) or not text.strip():
        return None
    module = raw.get("module")
    return ConversationTurn(role=role, text=text, module=module if isinstance(module, str) else None)


async def record_turn(
    state: FSMContext | None,
    *,
    role: str,
    text: str,
    module: str | None = None,
) -> None:
    """Appends one turn to the rolling window. Best-effort: a missing/broken
    state must never break the caller's actual flow, so this never raises."""
    if state is None or not text.strip():
        return
    trimmed = text.strip()
    if len(trimmed) > _MAX_TURN_TEXT_LEN:
        trimmed = trimmed[: _MAX_TURN_TEXT_LEN - 1].rstrip() + "…"
    try:
        data = await state.get_data()
        raw_turns = data.get(_TURNS_STATE_KEY)
        turns = list(raw_turns) if isinstance(raw_turns, list) else []
        turns.append(_serialize(ConversationTurn(role=role, text=trimmed, module=module)))
        await state.update_data(**{_TURNS_STATE_KEY: turns[-_MAX_TURNS:]})
    except Exception:
        return


async def recent_turns(state: FSMContext | None) -> tuple[ConversationTurn, ...]:
    """Returns the stored rolling window, oldest first. Never raises."""
    if state is None:
        return ()
    try:
        data = await state.get_data()
    except Exception:
        return ()
    raw_turns = data.get(_TURNS_STATE_KEY)
    if not isinstance(raw_turns, list):
        return ()
    parsed = (_deserialize(item) for item in raw_turns)
    return tuple(turn for turn in parsed if turn is not None)
