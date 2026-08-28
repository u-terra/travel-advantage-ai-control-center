"""TaskPlan v1 - structured output contract for the LLM Planner.

Mirrors the fail-closed validation style of
``app.orchestration.decision.parse_orchestration_decision``: a plan we cannot
fully trust is not a plan, it is a signal to fall back to the existing
router. There is no lenient/partial-parse path.

The executor id set (``ALLOWED_EXECUTORS``) is intentionally closed - the
Planner LLM decides *which* registered executor to call and *what* to pass
it, never invents a new capability. ``fetch_public_source`` is included from
Phase 1 onward even though no live implementation exists yet (see the module
docstring in ``app.planner``): this lets Phase 2 add a real URL-fetch
executor without changing the TaskPlan contract/schema again.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

MAX_STEPS = 5

# Closed set: the Planner LLM may only reference executors already wired to
# an existing service/provider (or, for fetch_public_source, reserved for
# Phase 2). Adding a new executor id is a deliberate, reviewed contract
# change, not something the model can introduce on its own.
ALLOWED_EXECUTORS: frozenset[str] = frozenset(
    {
        # Phase 2: URL -> extracted public text/content, for a subsequent
        # analyze_source step. No network fetch in Phase 1 - see module
        # docstring.
        "fetch_public_source",
        "analyze_source",
        "generate_content",
        "check_safety",
        "list_competitors",
        "rank_signals",
        "next_best_action",
    }
)

# Short, factual per-executor descriptions for the Planner LLM's prompt (see
# app.planner.request.build_planner_request) - mirrors
# app.routing.modules.MODULE_DESCRIPTION living next to Module. Also used to
# note which executors are free (repository/service reads) vs LLM-calling
# (cost control - see app.planner.cost).
EXECUTOR_CATALOG: dict[str, str] = {
    "list_competitors": (
        "Lists competitors already saved by this workspace (id, url, label). "
        "No LLM call."
    ),
    "fetch_public_source": (
        "Fetches a public URL - either given directly, or a saved "
        "competitor selected via competitor_label (exact name match) - and "
        "extracts readable text. Plain HTTP GET, no LLM call."
    ),
    "analyze_source": (
        "Analyzes extracted text into a structured summary, key facts, "
        "audience, and content angles. One LLM call."
    ),
    "generate_content": (
        "Generates a content draft (post/answer) from a task description or "
        "a prior analyze_source result. One LLM call."
    ),
    "check_safety": (
        "Checks a text for risky/unverified claims. One LLM call."
    ),
    "rank_signals": (
        "Ranks recent Lead Radar market/lead signals already collected for "
        "this workspace. No LLM call."
    ),
    "next_best_action": (
        "Computes this workspace's ranked next-best-actions from its own "
        "open work items and signals. No LLM call."
    ),
}

_GOAL_MAX_LEN = 500
_REASON_MAX_LEN = 500
_ACTION_MAX_LEN = 200
_FINAL_OUTPUT_MAX_LEN = 2000

# Plain snake_case-ish token, no spaces/punctuation: step ids are used as
# dependency references, not prose - same reasoning as reason_code in
# app.orchestration.decision.
_STEP_ID_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,64}$")


class InvalidTaskPlanError(ValueError):
    """Raised when raw LLM planner output does not satisfy the TaskPlan
    contract.

    Callers must treat this as a fail-closed signal: never guess/repair a
    partial plan, never execute a step from an invalid plan, and fall back to
    the existing router for this turn.
    """


@dataclass(frozen=True)
class PlanStep:
    id: str
    action: str
    executor: str
    input: Mapping[str, Any]
    depends_on: tuple[str, ...]


@dataclass(frozen=True)
class TaskPlan:
    goal: str
    reason: str
    steps: tuple[PlanStep, ...]
    final_output: str


def _require_dict(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise InvalidTaskPlanError("task plan must be a JSON object")
    return raw


def _require_nonempty_str(raw: Mapping[str, Any], key: str, *, max_len: int) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidTaskPlanError(f"{key} must be a non-empty string")
    value = value.strip()
    if len(value) > max_len:
        raise InvalidTaskPlanError(f"{key} exceeds max length of {max_len}")
    return value


def _require_step_id(raw: Mapping[str, Any]) -> str:
    value = raw.get("id")
    if not isinstance(value, str) or not _STEP_ID_PATTERN.match(value):
        raise InvalidTaskPlanError(
            "step id must be a non-empty token of letters/digits/underscore"
        )
    return value


def _require_executor(raw: Mapping[str, Any]) -> str:
    value = raw.get("executor")
    if not isinstance(value, str) or value not in ALLOWED_EXECUTORS:
        raise InvalidTaskPlanError(
            f"executor must be one of {sorted(ALLOWED_EXECUTORS)}, got {value!r}"
        )
    return value


def _freeze_step_input_value(value: Any) -> Any:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise InvalidTaskPlanError("step input float must be finite")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidTaskPlanError("step input keys must be strings")
            frozen[key] = _freeze_step_input_value(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_step_input_value(item) for item in value)
    raise InvalidTaskPlanError(
        f"unsupported step input value type: {type(value).__name__}"
    )


def _require_step_input(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    value = raw.get("input", {})
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise InvalidTaskPlanError("step input must be a JSON object")
    return _freeze_step_input_value(value)


def _require_depends_on(raw: Mapping[str, Any]) -> tuple[str, ...]:
    value = raw.get("depends_on", [])
    if value is None:
        value = []
    if not isinstance(value, list):
        raise InvalidTaskPlanError("depends_on must be a list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise InvalidTaskPlanError("depends_on entries must be non-empty strings")
        result.append(item)
    if len(set(result)) != len(result):
        raise InvalidTaskPlanError("depends_on must not contain duplicate entries")
    return tuple(result)


def _all_declared_step_ids(raw_steps: list[Any]) -> set[str]:
    ids: set[str] = set()
    for raw_step in raw_steps:
        if isinstance(raw_step, dict):
            value = raw_step.get("id")
            if isinstance(value, str):
                ids.add(value)
    return ids


def validate_task_plan(raw: Any) -> TaskPlan:
    """Strictly validates and converts raw (already JSON-decoded) LLM output.

    Raises :class:`InvalidTaskPlanError` on any deviation from the contract:
    wrong types, missing fields, an unknown executor, more than
    :data:`MAX_STEPS` steps, a duplicate step id, or a ``depends_on`` entry
    that is a self-reference, a forward reference, or refers to a step id
    that does not exist anywhere in the plan.
    """
    data = _require_dict(raw)
    goal = _require_nonempty_str(data, "goal", max_len=_GOAL_MAX_LEN)
    reason = _require_nonempty_str(data, "reason", max_len=_REASON_MAX_LEN)
    final_output = _require_nonempty_str(
        data, "final_output", max_len=_FINAL_OUTPUT_MAX_LEN
    )

    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise InvalidTaskPlanError("steps must be a non-empty list")
    if len(raw_steps) > MAX_STEPS:
        raise InvalidTaskPlanError(f"steps must not exceed {MAX_STEPS} entries")

    all_ids = _all_declared_step_ids(raw_steps)

    parsed_steps: list[PlanStep] = []
    seen_ids: set[str] = set()
    for raw_step in raw_steps:
        if not isinstance(raw_step, dict):
            raise InvalidTaskPlanError("each step must be a JSON object")

        step_id = _require_step_id(raw_step)
        if step_id in seen_ids:
            raise InvalidTaskPlanError(f"duplicate step id: {step_id!r}")

        action = _require_nonempty_str(raw_step, "action", max_len=_ACTION_MAX_LEN)
        executor = _require_executor(raw_step)
        step_input = _require_step_input(raw_step)
        depends_on = _require_depends_on(raw_step)

        for dependency in depends_on:
            if dependency == step_id:
                raise InvalidTaskPlanError(
                    f"step {step_id!r} must not depend on itself"
                )
            if dependency in seen_ids:
                continue
            if dependency in all_ids:
                raise InvalidTaskPlanError(
                    f"step {step_id!r} has a forward dependency on "
                    f"{dependency!r} (must depend only on earlier steps)"
                )
            raise InvalidTaskPlanError(
                f"step {step_id!r} depends on unknown step {dependency!r}"
            )

        parsed_steps.append(
            PlanStep(
                id=step_id,
                action=action,
                executor=executor,
                input=step_input,
                depends_on=depends_on,
            )
        )
        seen_ids.add(step_id)

    return TaskPlan(
        goal=goal,
        reason=reason,
        steps=tuple(parsed_steps),
        final_output=final_output,
    )
