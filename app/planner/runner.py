"""Sequential Planner executor runtime (Phase 2/3).

Runs a TaskPlan's steps strictly in declared order, one at a time, feeding
each executor only the dependency results declared in its own
``PlanStep.depends_on`` (see ``app.planner.executors`` for the per-executor
input contract) plus a ``PlannerExecutionContext``.

Deliberately NOT here: Telegram (``message.answer``), FSM mutation, Journal/
Work/Artifact writes, retries, replanning, or final LLM synthesis - see the
module docstring in ``app.planner`` for the Phase boundaries. If an
underlying executor's *own* normal contract happens to write somewhere (none
of the current executors do), that is that service's existing behavior, not
something the runner adds.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Mapping

from app.planner.context import PlannerExecutionContext
from app.planner.executors import PLANNER_EXECUTORS, PlannerExecutionError
from app.planner.plan import MAX_STEPS, InvalidTaskPlanError, TaskPlan, validate_task_plan

log = logging.getLogger(__name__)

# Per-step wall-clock budget. Not a retry mechanism - a single overrun step
# fails the whole run (see module docstring: no retries, no loops).
DEFAULT_STEP_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class PlannerRunResult:
    plan: TaskPlan | None
    step_results: Mapping[str, Any]
    completed_steps: tuple[str, ...]
    failed_step: str | None
    success: bool
    error: str | None


def _failed(
    plan: TaskPlan | None,
    step_results: Mapping[str, Any],
    completed_steps: tuple[str, ...],
    failed_step: str | None,
    error: str,
) -> PlannerRunResult:
    return PlannerRunResult(
        plan=plan,
        step_results=dict(step_results),
        completed_steps=completed_steps,
        failed_step=failed_step,
        success=False,
        error=error,
    )


async def execute_validated_plan(
    plan: TaskPlan,
    *,
    context: PlannerExecutionContext,
    step_timeout_seconds: float = DEFAULT_STEP_TIMEOUT_SECONDS,
) -> PlannerRunResult:
    """Executes an ALREADY-validated TaskPlan, one step at a time.

    Callers are responsible for having produced ``plan`` via
    ``validate_task_plan`` themselves - see ``run_planner_plan`` below for
    the safer default that does this for you from raw LLM output. This
    lower-level entry point exists so an orchestration layer (see
    ``app.planner.service.run_planner_for_task``) can react to "plan
    accepted" - e.g. show a one-time UX acknowledgment - after validation
    but before steps start executing, without re-validating a plan that was
    already validated moments earlier.

    Always returns a ``PlannerRunResult`` - never raises.
    """
    # Defense-in-depth: validate_task_plan already enforces this, but the
    # runner must not rely on that invariant holding forever without its own
    # check (same reasoning as the "unknown executor" guard below).
    if len(plan.steps) > MAX_STEPS:
        return _failed(plan, {}, (), None, f"plan exceeds MAX_STEPS ({MAX_STEPS})")

    step_results: dict[str, Any] = {}
    completed_steps: list[str] = []

    for step in plan.steps:
        executor = PLANNER_EXECUTORS.get(step.executor)
        if executor is None:
            # Unreachable given validate_task_plan + the closed-set assertion
            # in app.planner.executors, kept anyway: the runner must stay
            # fail-closed even if those two ever drift apart.
            log.info(
                "planner_runner: step failed workspace_id=%s id=%s executor=%s "
                "reason=unregistered_executor",
                context.workspace_id, step.id, step.executor,
            )
            return _failed(
                plan, step_results, tuple(completed_steps), step.id,
                f"no executor registered for {step.executor!r}",
            )

        # Only the dependency results this step actually declared - an
        # executor structurally cannot see any other step's result. Safe to
        # index directly: validate_task_plan guarantees depends_on entries
        # reference only earlier steps, and this loop returns immediately on
        # any failure, so every earlier step here has already succeeded and
        # is present in step_results.
        dependency_results = {
            dependency_id: step_results[dependency_id]
            for dependency_id in step.depends_on
        }

        step_started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                executor(
                    step_input=step.input,
                    dependency_results=dependency_results,
                    context=context,
                ),
                timeout=step_timeout_seconds,
            )
        except asyncio.TimeoutError:
            duration_ms = int((time.monotonic() - step_started) * 1000)
            log.info(
                "planner_runner: step failed workspace_id=%s id=%s executor=%s "
                "duration_ms=%d reason=timeout",
                context.workspace_id, step.id, step.executor, duration_ms,
            )
            return _failed(
                plan, step_results, tuple(completed_steps), step.id,
                f"step {step.id!r} timed out after {step_timeout_seconds}s",
            )
        except PlannerExecutionError as exc:
            duration_ms = int((time.monotonic() - step_started) * 1000)
            log.info(
                "planner_runner: step failed workspace_id=%s id=%s executor=%s "
                "duration_ms=%d reason=%s",
                context.workspace_id, step.id, step.executor, duration_ms, exc,
            )
            return _failed(
                plan, step_results, tuple(completed_steps), step.id,
                f"step {step.id!r} failed: {exc}",
            )
        except Exception as exc:  # noqa: BLE001 - fail-closed catch-all, logged
            duration_ms = int((time.monotonic() - step_started) * 1000)
            log.warning(
                "planner_runner: step raised an unexpected error workspace_id=%s "
                "id=%s executor=%s duration_ms=%d",
                context.workspace_id, step.id, step.executor, duration_ms, exc_info=True,
            )
            return _failed(
                plan, step_results, tuple(completed_steps), step.id,
                f"step {step.id!r} raised an unexpected error: {exc}",
            )

        duration_ms = int((time.monotonic() - step_started) * 1000)
        log.info(
            "planner_runner: step ok workspace_id=%s id=%s executor=%s duration_ms=%d",
            context.workspace_id, step.id, step.executor, duration_ms,
        )
        step_results[step.id] = result
        completed_steps.append(step.id)

    return PlannerRunResult(
        plan=plan,
        step_results=step_results,
        completed_steps=tuple(completed_steps),
        failed_step=None,
        success=True,
        error=None,
    )


async def run_planner_plan(
    raw_plan: Any,
    *,
    context: PlannerExecutionContext,
    step_timeout_seconds: float = DEFAULT_STEP_TIMEOUT_SECONDS,
) -> PlannerRunResult:
    """Validates ``raw_plan`` and executes it step by step.

    ``raw_plan`` is treated as untrusted raw (already JSON-decoded) LLM
    output and is ALWAYS revalidated here via ``validate_task_plan`` -
    callers must never pass a hand-built ``TaskPlan`` and expect it to be
    trusted without going through validation first (same fail-closed
    contract as ``app.orchestration.shadow.run_shadow_orchestration``
    re-parsing raw provider output rather than trusting a pre-built
    decision).

    Always returns a ``PlannerRunResult`` - never raises - so a future
    caller can fall back to the existing router on any failure (invalid
    plan, missing dependency, executor error, timeout) without wrapping this
    call in its own try/except.
    """
    try:
        plan = validate_task_plan(raw_plan)
    except InvalidTaskPlanError as exc:
        return _failed(None, {}, (), None, f"invalid_task_plan: {exc}")

    return await execute_validated_plan(
        plan, context=context, step_timeout_seconds=step_timeout_seconds,
    )
