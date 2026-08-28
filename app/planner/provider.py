"""Provider-agnostic contract for the Planner LLM (multi-step TaskPlan
generator).

Separate from ``app.orchestration.provider.OrchestrationLLMProvider`` on
purpose: that contract returns a single-turn intent/routing decision, never a
multi-step plan - conflating the two would mean every plain routing
classification call could accidentally grow into "and also propose steps",
which is exactly the scope creep this MVP avoids (see the Planner
architecture note: Planner sits *on top of* routing, it does not replace or
merge with it). It is also separate from ``app.services.llm.base.LLMProvider``
for the same reason ``OrchestrationLLMProvider`` is: that interface's methods
are hard-wired to specific Content Factory endpoints/contracts, none of which
is "return a structured multi-step plan".

Same conventions as the existing providers: a blocking method (callers run it
via ``asyncio.to_thread``, as the future runner will), ``None`` on any error/
timeout/malformed output, no vendor name inside Planner business logic - the
concrete adapter and vendor-selection factory are later-phase additions, kept
out of Phase 1 scope.

The provider returns the *raw*, already JSON-decoded object (or ``None`` on
transport failure). Turning that into a trusted ``TaskPlan`` is a separate,
provider-independent step - ``app.planner.plan.validate_task_plan`` - so
structured-output validation is not duplicated per vendor.

Cost control (Stage 3, see ``app.planner.cost``): ``plan()`` must make AT
MOST ONE underlying LLM call per invocation - no retries, no follow-up
calls. Synthesis of the final user-facing answer is a SEPARATE concern that
deliberately does NOT live on this contract - see
``app.planner.service.run_planner_for_task``, which reuses the existing
business ``app.services.llm.base.LLMProvider.generate_draft`` for that
(when needed at all) rather than adding a second expensive provider role
here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.planner.request import PlannerRequest


class PlannerLLMProvider(ABC):
    """Provider-agnostic contract for the multi-step Planner."""

    name: str = ""

    @property
    @abstractmethod
    def is_configured(self) -> bool:
        """True if this provider has everything needed to make calls."""

    @abstractmethod
    def plan(self, *, request: PlannerRequest) -> Any | None:
        """Returns the raw decoded JSON TaskPlan object, or None on any
        error (network, timeout, non-2xx, malformed JSON). Must not raise.
        Must make at most one underlying LLM call (see module docstring)."""


class NullPlannerLLMProvider(PlannerLLMProvider):
    """Safe default when no Planner LLM is configured yet.

    Ships as the default provider: Planner is fully inert (``is_configured``
    is False, so any caller must skip planning entirely and fall back to the
    existing router) until a real adapter is explicitly configured - same
    rollout shape as ``app.orchestration.provider.NullOrchestrationLLMProvider``.
    """

    name = "null"

    @property
    def is_configured(self) -> bool:
        return False

    def plan(self, *, request: PlannerRequest) -> Any | None:
        return None
