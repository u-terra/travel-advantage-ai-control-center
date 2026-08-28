from __future__ import annotations

import pytest

from app.planner.cost import (
    ABSOLUTE_MAX_LLM_CALLS_PER_PLANNER_RUN,
    DEFAULT_MAX_LLM_CALLS_PER_PLANNER_RUN,
    LLMCallBudget,
    normalize_max_llm_calls,
)
from app.planner.errors import PlannerExecutionError
from app.planner.plan import MAX_STEPS


def test_absolute_max_matches_the_documented_theoretical_worst_case():
    """MAX_STEPS LLM-calling steps + one plan() call + one optional
    synthesis call - the real theoretical ceiling, not an arbitrary number."""
    assert ABSOLUTE_MAX_LLM_CALLS_PER_PLANNER_RUN == MAX_STEPS + 2


def test_default_is_tighter_than_the_theoretical_ceiling():
    """Stage 3.1: a normal Planner run should cost far less than the
    theoretical worst case - the default is deliberately conservative."""
    assert DEFAULT_MAX_LLM_CALLS_PER_PLANNER_RUN == 4
    assert DEFAULT_MAX_LLM_CALLS_PER_PLANNER_RUN < ABSOLUTE_MAX_LLM_CALLS_PER_PLANNER_RUN


def test_budget_allows_calls_up_to_the_limit():
    budget = LLMCallBudget(max_calls=3)
    budget.consume(label="a")
    budget.consume(label="b")
    budget.consume(label="c")
    assert budget.used == 3
    assert budget.calls == ("a", "b", "c")


def test_budget_rejects_the_call_that_would_exceed_the_limit():
    budget = LLMCallBudget(max_calls=2)
    budget.consume(label="a")
    budget.consume(label="b")
    with pytest.raises(PlannerExecutionError, match="budget exceeded"):
        budget.consume(label="c")
    # the rejected call must not be counted as used
    assert budget.used == 2
    assert budget.calls == ("a", "b")


def test_budget_default_matches_module_constant():
    budget = LLMCallBudget()
    assert budget.max_calls == DEFAULT_MAX_LLM_CALLS_PER_PLANNER_RUN


def test_each_budget_instance_is_independent():
    a = LLMCallBudget(max_calls=1)
    b = LLMCallBudget(max_calls=1)
    a.consume(label="x")
    assert a.used == 1
    assert b.used == 0


def test_a_fifth_llm_call_is_physically_impossible_at_the_default_budget():
    """Stage 3.1 hard cap requirement: with the default budget (4), a 5th
    call must be refused before it happens."""
    budget = LLMCallBudget(max_calls=DEFAULT_MAX_LLM_CALLS_PER_PLANNER_RUN)
    for label in ("plan", "step_1", "step_2", "step_3"):
        budget.consume(label=label)
    with pytest.raises(PlannerExecutionError, match="budget exceeded"):
        budget.consume(label="step_4")
    assert budget.used == 4


# ── normalize_max_llm_calls (PLANNER_MAX_LLM_CALLS parsing) ─────────────────


def test_normalize_missing_or_empty_falls_back_to_default():
    assert normalize_max_llm_calls(None) == DEFAULT_MAX_LLM_CALLS_PER_PLANNER_RUN
    assert normalize_max_llm_calls("") == DEFAULT_MAX_LLM_CALLS_PER_PLANNER_RUN
    assert normalize_max_llm_calls("   ") == DEFAULT_MAX_LLM_CALLS_PER_PLANNER_RUN


def test_normalize_valid_value_is_used():
    assert normalize_max_llm_calls("4") == 4
    assert normalize_max_llm_calls(" 2 ") == 2


@pytest.mark.parametrize("raw", ["not-a-number", "3.5", "四"])
def test_normalize_invalid_value_falls_back_to_default(raw):
    assert normalize_max_llm_calls(raw) == DEFAULT_MAX_LLM_CALLS_PER_PLANNER_RUN


@pytest.mark.parametrize("raw", ["0", "-1", "-100"])
def test_normalize_non_positive_value_falls_back_to_default(raw):
    assert normalize_max_llm_calls(raw) == DEFAULT_MAX_LLM_CALLS_PER_PLANNER_RUN


def test_normalize_clamps_at_the_absolute_theoretical_ceiling():
    """An operator must never be able to configure MORE headroom than the
    executor set can theoretically use - see module docstring."""
    assert normalize_max_llm_calls("999") == DEFAULT_MAX_LLM_CALLS_PER_PLANNER_RUN
    assert normalize_max_llm_calls(str(ABSOLUTE_MAX_LLM_CALLS_PER_PLANNER_RUN)) == (
        ABSOLUTE_MAX_LLM_CALLS_PER_PLANNER_RUN
    )
