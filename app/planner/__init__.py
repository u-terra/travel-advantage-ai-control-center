"""Planner Layer (Phase 1, MVP) - structured multi-step TaskPlan on top of
the existing keyword router (see ``app.routing.router``) and the existing
orchestration shadow layer (see ``app.orchestration``).

Phase 1 scope is contracts only:

- ``app.planner.plan`` - ``TaskPlan``/``PlanStep`` dataclasses and
  ``validate_task_plan``, a strict fail-closed validator in the same style as
  ``app.orchestration.decision.parse_orchestration_decision``.
- ``app.planner.eligibility`` - ``is_planner_eligible``, a narrow
  deterministic gate for exactly three scenarios: competitor analysis,
  explicitly multi-action content tasks, and multi-step research.
- ``app.planner.provider`` - ``PlannerLLMProvider`` contract and the inert
  ``NullPlannerLLMProvider`` default.

Phase 2 adds a real, executable sequential runtime on top of those
contracts:

- ``app.planner.context`` - ``PlannerExecutionContext``, the narrow set of
  services/repositories executors are allowed to touch.
- ``app.planner.executors`` - ``PLANNER_EXECUTORS``, the closed registry of
  thin adapters (one per ``ALLOWED_EXECUTORS`` id) over existing services;
  ``PlannerExecutionError`` for controlled failures.
- ``app.planner.fetch`` - the SSRF-hardened ``fetch_public_source`` URL
  fetch + HTML text extraction (stdlib only).
- ``app.planner.runner`` - ``run_planner_plan``/``PlannerRunResult``, the
  sequential executor that turns a raw LLM plan into step results.

Nothing in this package is wired into ``app.handlers.tasks`` or any other
production flow yet - see the Phase 1/2/3 rollout plan. There is no OpenAI
Planner provider, no system prompt, no feature flag, and no Telegram
delivery of a plan's result; that is Phase 3 scope.
"""

from __future__ import annotations
