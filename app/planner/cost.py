"""Hard cost guard for a single Planner run.

Cost control is a first-class MVP requirement, not an afterthought: a
Planner run may make several paid LLM calls (one to build the TaskPlan, one
per LLM-calling step, at most one for final synthesis - see
``app.planner.service``), and a bug or a future executor addition must not
be able to turn that into an unbounded bill.

Two numbers matter here, and they are deliberately different:

- ``ABSOLUTE_MAX_LLM_CALLS_PER_PLANNER_RUN`` is the real theoretical ceiling
  given the current executor set: at most ``MAX_STEPS`` (5) steps run per
  plan, of the 7 registered executors only three ever call an LLM
  (``analyze_source``, ``generate_content``, ``check_safety`` - see
  ``app.planner.executors``; ``list_competitors``/``rank_signals``/
  ``next_best_action`` are pure repository/service reads,
  ``fetch_public_source`` is a plain HTTP GET), so the worst case is 5
  LLM-calling steps + 1 plan-building call + 1 optional synthesis call = 7.
- ``DEFAULT_MAX_LLM_CALLS_PER_PLANNER_RUN`` (Stage 3.1) is the actual
  configured default - deliberately tighter than the theoretical ceiling,
  because the theoretical worst case is not the *desired* case: a normal
  Planner run (see ``app.planner.request.SYSTEM_RULES`` for the prompt-level
  push toward cheap plans) should cost 2 paid calls, not 7. ``4`` leaves
  headroom for one legitimate multi-LLM-step plan while still ruling out
  runaway plans, and is itself operator-configurable via
  ``PLANNER_MAX_LLM_CALLS`` (see ``normalize_max_llm_calls`` and
  ``app.config``) - never hand-wavable past ``ABSOLUTE_MAX_LLM_CALLS_PER_PLANNER_RUN``.

``LLMCallBudget`` enforces whichever configured number is passed to it by
actually counting - every real LLM call in a Planner run must go through
``consume()`` BEFORE the call is made, and a call that would exceed the
budget is refused, not logged-and-allowed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.planner.errors import PlannerExecutionError
from app.planner.plan import MAX_STEPS

# Theoretical worst case given the current executor set - see module
# docstring. Also the hard upper clamp normalize_max_llm_calls() will never
# let an operator-supplied PLANNER_MAX_LLM_CALLS exceed.
ABSOLUTE_MAX_LLM_CALLS_PER_PLANNER_RUN = MAX_STEPS + 2

# Stage 3.1: the actual default budget - tighter than the theoretical
# ceiling by design (see module docstring).
DEFAULT_MAX_LLM_CALLS_PER_PLANNER_RUN = 4


def normalize_max_llm_calls(raw: str | None) -> int:
    """Parses PLANNER_MAX_LLM_CALLS. Falls back to
    DEFAULT_MAX_LLM_CALLS_PER_PLANNER_RUN on anything invalid, missing, or
    out of the sane [1, ABSOLUTE_MAX_LLM_CALLS_PER_PLANNER_RUN] range - a
    misconfigured value must never raise at startup and must never grant
    more headroom than the executor set can theoretically use."""
    text = (raw or "").strip()
    if not text:
        return DEFAULT_MAX_LLM_CALLS_PER_PLANNER_RUN
    try:
        value = int(text)
    except ValueError:
        return DEFAULT_MAX_LLM_CALLS_PER_PLANNER_RUN
    if value < 1 or value > ABSOLUTE_MAX_LLM_CALLS_PER_PLANNER_RUN:
        return DEFAULT_MAX_LLM_CALLS_PER_PLANNER_RUN
    return value


@dataclass
class LLMCallBudget:
    """Mutable, per-run call counter. One instance per Planner run - never
    shared across requests/users (see app.planner.service.run_planner_for_task,
    which creates a fresh instance for every call)."""

    max_calls: int = DEFAULT_MAX_LLM_CALLS_PER_PLANNER_RUN
    used: int = 0
    _log: list[str] = field(default_factory=list)

    def consume(self, *, label: str) -> None:
        """Raises PlannerExecutionError - fail-closed, not a silent skip -
        if this call would exceed the budget. Must be called BEFORE making
        the actual LLM request, never after."""
        if self.used >= self.max_calls:
            raise PlannerExecutionError(
                f"planner LLM call budget exceeded ({self.max_calls}) at {label!r}"
            )
        self.used += 1
        self._log.append(label)

    @property
    def calls(self) -> tuple[str, ...]:
        return tuple(self._log)
