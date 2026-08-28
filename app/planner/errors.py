"""Shared Planner exception types.

Split out from app.planner.executors so app.planner.cost (the LLM call
budget guard, consumed BY executors) does not need to import executors.py
itself - avoids a circular import between the two.
"""

from __future__ import annotations


class PlannerExecutionError(RuntimeError):
    """Controlled failure during Planner execution: a missing context
    dependency, a missing/malformed dependency-result field, an underlying
    service reporting failure, or an exhausted LLM call budget. The runner
    (see app.planner.runner) turns this into a failed PlannerRunResult - it
    is never allowed to surface as a raw, unhandled exception."""
