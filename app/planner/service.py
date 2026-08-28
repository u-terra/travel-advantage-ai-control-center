"""High-level Planner orchestration: build request -> plan() -> validate ->
execute -> synthesize.

Single entry point for ``app.handlers.tasks`` - kept fully transport-
independent (no aiogram ``Message``/``FSMContext`` here) so it can be tested
without Telegram plumbing, and so ``tasks.py``'s own responsibility stays
"gate (feature flag + allowlist + eligibility) + send", never "know how
Planner works internally".

Cost control (Stage 3 addendum, see ``app.planner.cost``): exactly one
``provider.plan()`` call per invocation, no retries, no replanning, no
re-invoking a step. A fresh ``LLMCallBudget`` is attached to the execution
context for every call to this function - never shared across requests.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable

from app.domain.business_profiles import BusinessProfile
from app.planner.context import PlannerExecutionContext
from app.planner.cost import DEFAULT_MAX_LLM_CALLS_PER_PLANNER_RUN, LLMCallBudget
from app.planner.errors import PlannerExecutionError
from app.planner.plan import InvalidTaskPlanError, TaskPlan, validate_task_plan
from app.planner.provider import PlannerLLMProvider
from app.planner.request import build_planner_request
from app.planner.runner import execute_validated_plan
from app.planner.synthesis import build_final_reply
from app.routing.router import RouteDecision

log = logging.getLogger(__name__)

_TASK_TEXT_LOG_PREVIEW_LEN = 80


@dataclass(frozen=True)
class PlannerOutcome:
    success: bool
    reply_text: str | None
    fallback_reason: str | None


def _preview(text: str) -> str:
    text = text.strip()
    if len(text) <= _TASK_TEXT_LOG_PREVIEW_LEN:
        return text
    return text[: _TASK_TEXT_LOG_PREVIEW_LEN - 1].rstrip() + "…"


async def run_planner_for_task(
    task_text: str,
    *,
    provider: PlannerLLMProvider,
    execution_context: PlannerExecutionContext,
    business_profile: BusinessProfile | None = None,
    advisory_route_decision: RouteDecision | None = None,
    on_plan_accepted: Callable[[TaskPlan], Awaitable[None]] | None = None,
    max_llm_calls: int = DEFAULT_MAX_LLM_CALLS_PER_PLANNER_RUN,
) -> PlannerOutcome:
    """Runs the full Planner pipeline for one eligible user task.

    Always returns a ``PlannerOutcome`` - never raises - so the caller can
    fall back to the existing router on ``success=False`` without wrapping
    this call in its own try/except (though callers SHOULD still wrap it as
    defense in depth - see ``app.handlers.tasks``, matching the same
    belt-and-suspenders pattern already used for orchestration shadow mode).

    ``max_llm_calls`` (Stage 3.1, see ``app.planner.cost`` and
    ``PLANNER_MAX_LLM_CALLS``) is the hard per-run budget - callers should
    pass the configured value; the default here only covers direct/test
    callers that do not.
    """
    started = time.monotonic()
    workspace_id = execution_context.workspace_id
    budget = LLMCallBudget(max_calls=max_llm_calls)
    context = replace(execution_context, llm_call_budget=budget)

    request = build_planner_request(
        task_text, business_profile=business_profile,
        advisory_route_decision=advisory_route_decision,
    )

    try:
        budget.consume(label="plan")
    except PlannerExecutionError:
        # Unreachable at a fresh budget (max_calls > 0), kept for symmetry/
        # fail-closed consistency with every other LLM call site.
        return PlannerOutcome(False, None, "llm_budget_exceeded")

    try:
        raw_plan = await asyncio.to_thread(provider.plan, request=request)
    except Exception:
        log.warning(
            "planner_service: provider.plan raised workspace_id=%s", workspace_id, exc_info=True,
        )
        return PlannerOutcome(False, None, "plan_provider_error")

    if raw_plan is None:
        log.info(
            "planner_service: plan rejected workspace_id=%s task=%r reason=provider_returned_none",
            workspace_id, _preview(task_text),
        )
        return PlannerOutcome(False, None, "plan_provider_unavailable")

    try:
        plan = validate_task_plan(raw_plan)
    except InvalidTaskPlanError as exc:
        log.info(
            "planner_service: plan rejected workspace_id=%s task=%r reason=%s",
            workspace_id, _preview(task_text), exc,
        )
        return PlannerOutcome(False, None, "invalid_plan")

    log.info(
        "planner_service: plan accepted workspace_id=%s steps=%d goal=%r",
        workspace_id, len(plan.steps), _preview(plan.goal),
    )

    if on_plan_accepted is not None:
        try:
            await on_plan_accepted(plan)
        except Exception:
            log.debug("planner_service: on_plan_accepted callback failed", exc_info=True)

    run_result = await execute_validated_plan(plan, context=context)
    if not run_result.success:
        reason = (
            f"step_failed:{run_result.failed_step}" if run_result.failed_step
            else "execution_failed"
        )
        log.info(
            "planner_service: run failed workspace_id=%s reason=%s error=%s",
            workspace_id, reason, run_result.error,
        )
        return PlannerOutcome(False, None, reason)

    log.info(
        "planner_service: run succeeded workspace_id=%s completed_steps=%d",
        workspace_id, len(run_result.completed_steps),
    )

    try:
        reply_text, used_llm_synthesis = await build_final_reply(
            user_task=task_text, plan=plan, run_result=run_result,
            context=context, business_profile=business_profile,
        )
    except Exception:
        # Defense in depth: build_final_reply already fails closed internally,
        # but a Planner-caused crash reaching the user is exactly the one
        # outcome that must be structurally impossible - never "usually fine".
        log.warning(
            "planner_service: synthesis raised unexpectedly workspace_id=%s",
            workspace_id, exc_info=True,
        )
        return PlannerOutcome(False, None, "synthesis_error")

    duration_ms = int((time.monotonic() - started) * 1000)
    log.info(
        "planner_service: completed workspace_id=%s duration_ms=%d llm_calls=%d "
        "max_llm_calls=%d used_llm_synthesis=%s",
        workspace_id, duration_ms, budget.used, budget.max_calls, used_llm_synthesis,
    )
    return PlannerOutcome(True, reply_text, None)
