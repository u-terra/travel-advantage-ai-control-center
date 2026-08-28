"""Closed registry of Planner executors - thin adapters over existing
services, never a reimplementation of their business logic.

Each executor has the signature::

    async def executor(
        *, step_input: Mapping[str, Any],
        dependency_results: Mapping[str, Any],
        context: PlannerExecutionContext,
    ) -> Any

``dependency_results`` contains ONLY the results of the step ids listed in
this step's own ``PlanStep.depends_on`` (enforced by the caller, see
``app.planner.runner``) - an executor structurally cannot see a result from
a step it does not depend on. There is no template language (no
``{{step_1.text}}``): each executor below documents its own explicit input
rule - which ``step_input`` key it reads directly, and which single field it
expects from "the" dependency result when chaining (MVP steps have at most
one meaningful upstream dependency; if a future step needs more than one,
that is a reason to extend the rule, not to add templating).

Any missing/None context service or missing/malformed dependency field
raises ``PlannerExecutionError`` - never a bare ``KeyError``/``AttributeError``/
``TypeError`` from a ``None`` value.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Mapping

from app.planner.context import PlannerExecutionContext
from app.planner.errors import PlannerExecutionError
from app.planner.fetch import PublicSourceFetchError, fetch_public_source_sync
from app.planner.plan import ALLOWED_EXECUTORS
from app.services.generation_request_builder import build_provider_generation_request
from app.services.lead_radar import fetch_signals_sync
from app.services.material_orchestration import MaterialOrchestrationService

Executor = Callable[..., Awaitable[Any]]

# Re-exported for backward compatibility - PlannerExecutionError now lives in
# app.planner.errors (so app.planner.cost can raise it without importing this
# module and creating a cycle), but every existing caller/test imports it
# from here, so it stays importable from both places.
__all__ = ["PLANNER_EXECUTORS", "PlannerExecutionError", "Executor"]


def _require_context_service(value: Any, *, executor_name: str, service_name: str) -> Any:
    if value is None:
        raise PlannerExecutionError(
            f"{executor_name}: required context.{service_name} is not configured"
        )
    return value


def _first_dependency_result(
    dependency_results: Mapping[str, Any], *, executor_name: str
) -> Any:
    """Returns the result of the first-declared dependency (insertion order
    of PlanStep.depends_on, preserved by the runner). MVP steps chain at
    most one meaningful upstream result - see module docstring."""
    if not dependency_results:
        raise PlannerExecutionError(
            f"{executor_name}: requires step_input or at least one dependency result"
        )
    return next(iter(dependency_results.values()))


def _require_field(source: Any, field: str, *, executor_name: str) -> Any:
    if not isinstance(source, Mapping) or not source.get(field):
        raise PlannerExecutionError(
            f"{executor_name}: dependency result is missing required field {field!r}"
        )
    return source[field]


def _positive_int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return default
    return value


async def _list_competitors(
    *,
    step_input: Mapping[str, Any],
    dependency_results: Mapping[str, Any],
    context: PlannerExecutionContext,
) -> dict[str, Any]:
    """Input rule: optional step_input['limit'] (int, default 20). No
    dependency required - this is a workspace lookup, not a chained step."""
    repo = _require_context_service(
        context.competitor_repository,
        executor_name="list_competitors",
        service_name="competitor_repository",
    )
    limit = _positive_int(step_input.get("limit"), default=20)
    competitors = await repo.list_for_workspace(context.workspace_id, limit=limit)
    return {
        "competitors": [
            {"id": c.id, "url": c.url, "label": c.label} for c in competitors
        ],
    }


async def _fetch_public_source(
    *,
    step_input: Mapping[str, Any],
    dependency_results: Mapping[str, Any],
    context: PlannerExecutionContext,
) -> dict[str, Any]:
    """Input rule - exactly one of three explicit paths, no template language:

    A. step_input['url'] (non-empty string) - a direct URL, for when the
       user gave a URL and the Planner LLM already decided to fetch it as-is.
    B. step_input['competitor_id'] - the TaskPlan is built BEFORE
       list_competitors actually runs, so the LLM cannot know a real
       competitor's URL/id at plan-construction time by value; it can only
       reference "whichever competitor list_competitors returns" via an
       explicit selector. Resolved from the 'competitors' list of the first
       declared dependency result (i.e. a list_competitors result) ONLY -
       never via context.competitor_repository, which would bypass the
       declared depends_on data flow entirely.
    C. step_input['competitor_label'] - same as B, but by (normalized,
       case-insensitive, trimmed) label, for the natural-language scenario
       where the user names a saved competitor instead of an id ("проанализируй
       конкурента ТурКлуб"). Exact match only - no fuzzy/semantic matching in
       this MVP. Zero matches or more than one match (ambiguous label) is a
       controlled PlannerExecutionError, never a guess.

    Precedence when more than one is present: url > competitor_id >
    competitor_label. A competitor_id/competitor_label with no matching
    entry, or a matching entry with no url, is a controlled
    PlannerExecutionError, never a silent fallback.
    """
    url = step_input.get("url")
    if not isinstance(url, str) or not url.strip():
        url = _resolve_url_from_competitor_dependency(step_input, dependency_results)
    try:
        fetched = await asyncio.to_thread(fetch_public_source_sync, url)
    except PublicSourceFetchError as exc:
        raise PlannerExecutionError(f"fetch_public_source: {exc}") from exc
    return {
        "url": fetched.url,
        "final_url": fetched.final_url,
        "title": fetched.title,
        "text": fetched.text,
        "content_type": fetched.content_type,
    }


def _competitors_from_dependency(dependency_results: Mapping[str, Any]) -> list[Any]:
    dependency = _first_dependency_result(dependency_results, executor_name="fetch_public_source")
    competitors = dependency.get("competitors") if isinstance(dependency, Mapping) else None
    if not isinstance(competitors, list):
        raise PlannerExecutionError(
            "fetch_public_source: dependency result has no 'competitors' list "
            "to resolve competitor_id/competitor_label against"
        )
    return competitors


def _competitor_url(match: Mapping[str, Any], selector_repr: str) -> str:
    url = match.get("url")
    if not isinstance(url, str) or not url.strip():
        raise PlannerExecutionError(f"fetch_public_source: {selector_repr} has no url")
    return url


def _resolve_url_from_competitor_dependency(
    step_input: Mapping[str, Any], dependency_results: Mapping[str, Any],
) -> str:
    competitor_id = step_input.get("competitor_id")
    competitor_label = step_input.get("competitor_label")

    if competitor_id is not None:
        competitors = _competitors_from_dependency(dependency_results)
        match = next(
            (
                entry for entry in competitors
                if isinstance(entry, Mapping) and str(entry.get("id")) == str(competitor_id)
            ),
            None,
        )
        if match is None:
            raise PlannerExecutionError(
                f"fetch_public_source: competitor_id {competitor_id!r} not found "
                "in dependency result"
            )
        return _competitor_url(match, f"competitor_id {competitor_id!r}")

    if isinstance(competitor_label, str) and competitor_label.strip():
        normalized_label = " ".join(competitor_label.strip().split()).lower()
        competitors = _competitors_from_dependency(dependency_results)
        matches = [
            entry for entry in competitors
            if isinstance(entry, Mapping)
            and isinstance(entry.get("label"), str)
            and " ".join(entry["label"].strip().split()).lower() == normalized_label
        ]
        if not matches:
            raise PlannerExecutionError(
                f"fetch_public_source: competitor_label {competitor_label!r} not "
                "found in dependency result"
            )
        if len(matches) > 1:
            raise PlannerExecutionError(
                f"fetch_public_source: competitor_label {competitor_label!r} "
                "matches more than one saved competitor (ambiguous)"
            )
        return _competitor_url(matches[0], f"competitor_label {competitor_label!r}")

    raise PlannerExecutionError(
        "fetch_public_source: step input must include a non-empty 'url', "
        "'competitor_id', or 'competitor_label' selecting an entry from a "
        "dependency's list_competitors result"
    )


async def _analyze_source(
    *,
    step_input: Mapping[str, Any],
    dependency_results: Mapping[str, Any],
    context: PlannerExecutionContext,
) -> dict[str, Any]:
    """Input rule: step_input['text'] if present, else the 'text' field of
    the first dependency result (e.g. a fetch_public_source result)."""
    llm_provider = _require_context_service(
        context.llm_provider, executor_name="analyze_source", service_name="llm_provider",
    )
    source_text = step_input.get("text")
    if not isinstance(source_text, str) or not source_text.strip():
        dependency = _first_dependency_result(dependency_results, executor_name="analyze_source")
        source_text = _require_field(dependency, "text", executor_name="analyze_source")

    if context.llm_call_budget is not None:
        context.llm_call_budget.consume(label="analyze_source")
    payload = await asyncio.to_thread(llm_provider.analyze_source, source_text=source_text)
    if payload is None:
        raise PlannerExecutionError("analyze_source: provider returned no result")
    return {
        "summary": payload.summary,
        "key_facts": list(payload.key_facts),
        "disputed_claims": list(payload.disputed_claims),
        "audience_value": payload.audience_value,
        "target_audiences": list(payload.target_audiences),
        "content_angles": list(payload.content_angles),
        "recommended_formats": list(payload.recommended_formats),
        "warnings": list(payload.warnings),
    }


async def _generate_content(
    *,
    step_input: Mapping[str, Any],
    dependency_results: Mapping[str, Any],
    context: PlannerExecutionContext,
) -> dict[str, Any]:
    """Input rule: step_input['task_text'] if present, else built from the
    first dependency result's 'summary' (+ 'key_facts' if present, e.g. an
    analyze_source result). Reuses MaterialOrchestrationService exactly as
    app.handlers.tasks._maybe_send_draft does for free-text generation - no
    business logic is reimplemented here."""
    llm_provider = _require_context_service(
        context.llm_provider, executor_name="generate_content", service_name="llm_provider",
    )
    task_text = step_input.get("task_text")
    if not isinstance(task_text, str) or not task_text.strip():
        dependency = _first_dependency_result(dependency_results, executor_name="generate_content")
        summary = _require_field(dependency, "summary", executor_name="generate_content")
        key_facts = dependency.get("key_facts") if isinstance(dependency, Mapping) else None
        task_text = summary
        if key_facts:
            task_text = summary + "\n\n" + "\n".join(f"- {fact}" for fact in key_facts)

    profile = None
    if context.partner_repository is not None:
        profile = await context.partner_repository.get_business_profile(context.workspace_id)

    spec = MaterialOrchestrationService().build_free_text_generation_spec(
        context.workspace_id, task_text, profile,
    )
    provider_request = build_provider_generation_request(spec)
    if context.llm_call_budget is not None:
        context.llm_call_budget.consume(label="generate_content")
    draft = await asyncio.to_thread(
        llm_provider.generate_draft,
        source_text=provider_request.source_text,
        material_type=provider_request.material_type,
        output_format=provider_request.output_format,
        mode="ai",
    )
    if draft is None:
        raise PlannerExecutionError("generate_content: provider returned no draft")
    return {"text": draft.text, "warnings": list(draft.warnings)}


async def _check_safety(
    *,
    step_input: Mapping[str, Any],
    dependency_results: Mapping[str, Any],
    context: PlannerExecutionContext,
) -> dict[str, Any]:
    """Input rule: step_input['text'] if present, else the 'text' field of
    the first dependency result (e.g. a generate_content result)."""
    llm_provider = _require_context_service(
        context.llm_provider, executor_name="check_safety", service_name="llm_provider",
    )
    source_text = step_input.get("text")
    if not isinstance(source_text, str) or not source_text.strip():
        dependency = _first_dependency_result(dependency_results, executor_name="check_safety")
        source_text = _require_field(dependency, "text", executor_name="check_safety")

    if context.llm_call_budget is not None:
        context.llm_call_budget.consume(label="check_safety")
    result = await asyncio.to_thread(llm_provider.check_text, source_text=source_text)
    if result is None:
        raise PlannerExecutionError("check_safety: provider returned no result")
    return {
        "warnings": [
            {"phrase": finding.phrase, "warning": finding.warning}
            for finding in result.warnings
        ],
        "rewritten_text": result.rewritten_text,
    }


async def _rank_signals(
    *,
    step_input: Mapping[str, Any],
    dependency_results: Mapping[str, Any],
    context: PlannerExecutionContext,
) -> dict[str, Any]:
    """Input rule: optional step_input['limit'] (int, default 5). No
    dependency required."""
    config = _require_context_service(
        context.lead_radar_config, executor_name="rank_signals", service_name="lead_radar_config",
    )
    limit = _positive_int(step_input.get("limit"), default=5)
    signals = await asyncio.to_thread(fetch_signals_sync, config, limit=limit)
    if signals is None:
        raise PlannerExecutionError("rank_signals: Lead Radar is unavailable")
    return {
        "signals": [
            {
                "id": signal.id,
                "title": signal.title,
                "url": signal.url,
                "category": signal.category,
                "recommended_action": signal.recommended_action,
                "action_label": signal.action_label,
            }
            for signal in signals
        ],
    }


async def _next_best_action(
    *,
    step_input: Mapping[str, Any],
    dependency_results: Mapping[str, Any],
    context: PlannerExecutionContext,
) -> dict[str, Any]:
    """Input rule: none - a full workspace-scoped snapshot, same call
    app.handlers.daily_actions makes via DailyActionsService.build()."""
    service = _require_context_service(
        context.daily_actions_service,
        executor_name="next_best_action",
        service_name="daily_actions_service",
    )
    result = await service.build(context.workspace_id)
    return {
        "actions": [
            {"source": action.source, "headline": action.headline, "detail": action.detail}
            for action in result.actions
        ],
    }


PLANNER_EXECUTORS: Mapping[str, Executor] = {
    "fetch_public_source": _fetch_public_source,
    "analyze_source": _analyze_source,
    "generate_content": _generate_content,
    "check_safety": _check_safety,
    "list_competitors": _list_competitors,
    "rank_signals": _rank_signals,
    "next_best_action": _next_best_action,
}

assert set(PLANNER_EXECUTORS) == ALLOWED_EXECUTORS, (
    "app.planner.executors.PLANNER_EXECUTORS must exactly match "
    "app.planner.plan.ALLOWED_EXECUTORS"
)
