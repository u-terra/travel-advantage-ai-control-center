from __future__ import annotations

import asyncio


def run(value):
    return asyncio.run(value)


class State:
    def __init__(self, data=None):
        self.data = data or {}

    async def get_data(self):
        return self.data

    async def update_data(self, **kwargs):
        self.data.update(kwargs)


class BrokenState:
    """Simulates a storage failure - record_turn/recent_turns must degrade
    to a no-op rather than break the caller's actual flow."""

    async def get_data(self):
        raise RuntimeError("storage unavailable")

    async def update_data(self, **kwargs):
        raise RuntimeError("storage unavailable")


def test_record_and_recall_single_turn():
    from app.orchestration.context import record_turn, recent_turns

    state = State()
    run(record_turn(state, role="user", text="Перепиши этот пост"))
    turns = run(recent_turns(state))
    assert len(turns) == 1
    assert turns[0].role == "user"
    assert turns[0].text == "Перепиши этот пост"
    assert turns[0].module is None


def test_record_turn_stores_module_for_assistant_turns():
    from app.orchestration.context import record_turn, recent_turns

    state = State()
    run(record_turn(
        state, role="assistant", text="Radar предложил пост-идею: X",
        module="Travel Content Factory",
    ))
    turns = run(recent_turns(state))
    assert turns[0].module == "Travel Content Factory"


def test_window_keeps_only_last_six_turns_oldest_first():
    from app.orchestration.context import record_turn, recent_turns

    state = State()
    for i in range(10):
        run(record_turn(state, role="user", text=f"turn {i}"))
    turns = run(recent_turns(state))
    assert len(turns) == 6
    assert [t.text for t in turns] == [f"turn {i}" for i in range(4, 10)]


def test_long_turn_text_is_truncated():
    from app.orchestration.context import record_turn, recent_turns

    state = State()
    run(record_turn(state, role="user", text="x" * 500))
    turns = run(recent_turns(state))
    assert len(turns[0].text) <= 300
    assert turns[0].text.endswith("…")


def test_blank_text_is_not_recorded():
    from app.orchestration.context import record_turn, recent_turns

    state = State()
    run(record_turn(state, role="user", text="   "))
    assert run(recent_turns(state)) == ()


def test_none_state_is_a_safe_no_op():
    from app.orchestration.context import record_turn, recent_turns

    run(record_turn(None, role="user", text="Привет"))  # must not raise
    assert run(recent_turns(None)) == ()


def test_broken_state_degrades_to_no_op_without_raising():
    from app.orchestration.context import record_turn, recent_turns

    state = BrokenState()
    run(record_turn(state, role="user", text="Привет"))  # must not raise
    assert run(recent_turns(state)) == ()


def test_recent_turns_ignores_malformed_stored_entries():
    from app.orchestration.context import recent_turns

    state = State(data={"orchestration_recent_turns": [
        {"role": "user", "text": "valid"},
        {"role": "unknown_role", "text": "bad role"},
        {"role": "user", "text": ""},
        "not even a dict",
        None,
        {"role": "assistant", "text": "also valid", "module": "Safety Layer"},
    ]})
    turns = run(recent_turns(state))
    assert [t.text for t in turns] == ["valid", "also valid"]


def test_recent_turns_with_non_list_stored_value_is_empty():
    from app.orchestration.context import recent_turns

    state = State(data={"orchestration_recent_turns": "corrupted"})
    assert run(recent_turns(state)) == ()
