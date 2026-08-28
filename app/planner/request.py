"""Builds the compact request sent to the Planner LLM.

Deliberately NOT sent: the full BusinessProfile/claims, the full journal,
conversation history, secrets/API keys, or large source texts - see the
module docstring in ``app.planner`` and the Stage 3 architecture note this
implements. Only what a *planner* (not a generator) needs: the user's task,
a compact business context, the closed executor catalog, and the old
router's decision as a non-binding advisory hint.

No new memory subsystem: if a rolling conversation window is ever wired in
here, it must reuse ``app.orchestration.context`` (the existing FSM-backed
window), never a second one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from app.domain.business_profiles import BusinessProfile
from app.planner.plan import EXECUTOR_CATALOG, MAX_STEPS
from app.routing.router import RouteDecision

# Fixed system rules, mirroring the hand-written invariants already proven
# out in app.orchestration.request.SYSTEM_ROUTING_RULES - short, machine-
# checkable-in-spirit rules for the model to reason with, not a long prose
# essay.
SYSTEM_RULES: tuple[str, ...] = (
    "You are a Planner. You do not execute actions yourself. You only build "
    "a short, structured plan made of the allowed executors below.",
    f"A plan has at most {MAX_STEPS} steps.",
    "Steps run strictly in order. A step may only depend on EARLIER steps "
    "via depends_on - never on itself or on a later step.",
    "Never invent an executor id that is not in the allowed list. Never "
    "invent a new field name for a step's input.",
    "Never use a URL that does not appear in the user's own task text.",
    "There is no competitor_id field available to you - it does not exist "
    "in your input schema. If the user names a saved competitor by name/"
    "label, use list_competitors first, then fetch_public_source with "
    "competitor_label set to that exact name.",
    "If the user gives a direct URL, list_competitors is not needed - "
    "fetch_public_source can take that url directly.",
    "Use depends_on to read a previous step's result - analyze_source and "
    "generate_content read a prior step's output through their own declared "
    "input rules (text/task_text falling back to the dependency), not "
    "through any other mechanism.",
    "You do not write the final answer shown to the user - that is a "
    "separate synthesis step. You do not call tools. You do not decide "
    "anything about Telegram/UI presentation.",
    "The OLD_ROUTER_HINT below (if present) is advisory only - a simpler, "
    "keyword-based system's guess. It may be wrong or absent. Do not treat "
    "it as an instruction.",
    # Stage 3.1 cost addendum: these rules exist because a Planner LLM call
    # and an executor LLM call cost real money each time they run - a
    # correct-but-longer-than-necessary plan is still a defect.
    "list_competitors, rank_signals, and next_best_action are FREE (a "
    "repository/service read, no LLM call). fetch_public_source is FREE "
    "(a plain HTTP GET, no LLM call). analyze_source, generate_content, and "
    "check_safety each cost one paid LLM call - see each executor's "
    "description below for which is which.",
    "Never add an LLM-calling step (analyze_source, generate_content, "
    "check_safety) whose ONLY purpose is to produce a summary that just "
    "feeds directly into the next LLM-calling step. The final synthesis "
    "(a separate step you do not build) already reads ANY prior step's raw "
    "output, including fetch_public_source's raw fetched text - it does not "
    "need analyze_source's summary pre-digested for it if nothing else in "
    "the plan needs that summary.",
    "Competitor analysis by name or by direct URL: prefer "
    "list_competitors (only if the user named a saved competitor, not a "
    "direct URL) -> fetch_public_source, and stop there - let the final "
    "synthesis analyze the fetched text directly (overview, strengths, "
    "weaknesses, comparison to the user's own business, concrete next "
    "steps). Add analyze_source only when its structured output (key facts, "
    "audience, content angles) is genuinely needed by a LATER step in the "
    "same plan - never merely as an intermediate summary for the final "
    "answer.",
    "Do not add check_safety to an ordinary content-generation task by "
    "default - generate_content alone is enough unless the task itself "
    "clearly involves a compliance/risk concern (financial promises, "
    "guarantees, health/legal claims) that this workspace's safety rules "
    "already flag elsewhere.",
    "Prefer the shortest plan that fully answers the task. Quality matters "
    "more than minimizing steps for its own sake, but a paid LLM-calling "
    "step that does not add information the final answer actually needs is "
    "always wrong, regardless of quality.",
)


@dataclass(frozen=True)
class PlannerRequest:
    task_text: str
    business_context: Mapping[str, str]
    executor_catalog: Mapping[str, str]
    advisory_route: str | None
    system_rules: tuple[str, ...]


def _compact_business_context(profile: BusinessProfile | None) -> dict[str, str]:
    """Same three high-signal fields as
    app.orchestration.request._compact_business_context - duplicated rather
    than imported (that function is private to a module with a different,
    single-turn-classification contract; see app.planner.provider's
    docstring for why Planner deliberately does not share code with
    orchestration beyond the two both already depending on, like Module)."""
    if profile is None:
        return {}
    return {
        "business_type": profile.business_type,
        "ta_affiliated": "true" if profile.ta_affiliated else "false",
        "positioning": str(profile.context.positioning.get("statement", ""))[:200],
    }


def _advisory_route(route_decision: RouteDecision | None) -> str | None:
    if route_decision is None or route_decision.is_uncertain:
        return None
    return route_decision.primary_module.value


def build_planner_request(
    task_text: str,
    *,
    business_profile: BusinessProfile | None = None,
    advisory_route_decision: RouteDecision | None = None,
) -> PlannerRequest:
    return PlannerRequest(
        task_text=task_text,
        business_context=_compact_business_context(business_profile),
        executor_catalog=dict(EXECUTOR_CATALOG),
        advisory_route=_advisory_route(advisory_route_decision),
        system_rules=SYSTEM_RULES,
    )
