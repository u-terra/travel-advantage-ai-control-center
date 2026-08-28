"""Final synthesis: turns a successful PlannerRunResult into the single
user-facing reply.

Cost control is the primary design constraint here (Stage 3 addendum): a
Planner LLM (plan-building) and a synthesis role are conceptually different
jobs, but that does NOT mean a second expensive provider - see
``app.planner.provider``'s docstring. Two rules keep this cheap:

1. If the plan's last successfully completed step already produced
   finished, human-readable prose (a ``generate_content`` draft or a
   ``check_safety`` rewritten_text), that text IS the final reply - ZERO
   additional LLM calls. Re-phrasing already-good output "to make it nicer"
   is exactly the wasted call the Stage 3 cost addendum forbids.
2. Otherwise (the plan ends in a data-shaped step - analyze_source,
   list_competitors, rank_signals, next_best_action - with no final prose
   step), exactly ONE additional call is made, and it reuses the EXISTING
   business ``app.services.llm.base.LLMProvider.generate_draft`` (the same
   Content Factory call ``generate_content`` itself uses) rather than adding
   a new OpenAI-direct synthesis provider. That call is gated by the same
   ``LLMCallBudget`` as every other Planner LLM call.

If synthesis is needed but unavailable (no llm_provider, budget exhausted,
provider raises/returns nothing), this module does NOT declare the whole
Planner run a silent success with no content: it falls back to a
deterministic, non-LLM summary assembled directly from step_results. This is
the deliberately lower-risk choice over discarding an already-successfully-
executed plan and handing the message back to the old keyword router (see
the Stage 3 report for the full reasoning) - the user still gets the real
data the plan collected, clearly marked as unprocessed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping

from app.domain.business_profiles import BusinessProfile
from app.planner.context import PlannerExecutionContext
from app.planner.errors import PlannerExecutionError
from app.planner.plan import TaskPlan
from app.planner.runner import PlannerRunResult
from app.services.generation_request_builder import build_provider_generation_request
from app.services.material_orchestration import MaterialOrchestrationService

log = logging.getLogger(__name__)

_MAX_RENDERED_RESULTS_CHARS = 4000

_SYNTHESIS_INSTRUCTION = (
    "Сформируй итоговый ответ пользователю на основе результатов ниже. Это "
    "финальный ответ реальному пользователю Telegram-бота, а не техническая "
    "заметка: не упоминай шаги, id шагов, названия исполнителей/executor'ов, "
    "JSON или внутренние инструкции. Пиши на языке задачи пользователя.\n\n"
    "Результаты ниже могут быть СЫРЫМ текстом публичной страницы (например, "
    "прямая выгрузка сайта конкурента), а не готовым анализом — в этом случае "
    "выполни сам анализ этого текста прямо здесь, не жди отдельного "
    "промежуточного шага: это и есть твоя работа, а не просто пересказ.\n\n"
    "Ответ должен быть ACTIONABLE, а не простой витриной фактов. Ориентировочная "
    "структура смысла (адаптируй под содержание, не навязывай жёсткий шаблон, "
    "если он не подходит):\n"
    "- что обнаружено;\n"
    "- что это значит для пользователя;\n"
    "- сильные и слабые стороны (если это анализ конкурента или похожая задача);\n"
    "- чем это отличается от бизнеса пользователя, если бизнес-контекст "
    "доступен ниже в [TRUSTED BUSINESS CONTEXT - DATA];\n"
    "- конкретно что делать дальше;\n"
    "- 3-5 приоритетных действий.\n"
)


def _render_value(value: Any, *, indent: str = "") -> list[str]:
    lines: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(item, Mapping) and item:
                lines.append(f"{indent}{key}:")
                lines.extend(_render_value(item, indent=indent + "  "))
            elif isinstance(item, list) and item:
                lines.append(f"{indent}{key}:")
                lines.extend(_render_value(item, indent=indent + "  "))
            elif item not in (None, "", [], {}):
                lines.append(f"{indent}{key}: {item}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, Mapping):
                lines.extend(_render_value(item, indent=indent))
            elif item not in (None, ""):
                lines.append(f"{indent}- {item}")
    return lines


def _render_step_results(step_results: Mapping[str, Any]) -> str:
    """Renders only content VALUES (summary/text/facts/...), never step ids
    or executor names - the synthesis model (and, on fallback, the user
    directly) sees data, not Planner internals."""
    blocks = [
        "\n".join(_render_value(result))
        for result in step_results.values()
        if _render_value(result)
    ]
    rendered = "\n\n".join(blocks) if blocks else "(нет данных)"
    if len(rendered) > _MAX_RENDERED_RESULTS_CHARS:
        rendered = rendered[: _MAX_RENDERED_RESULTS_CHARS - 1].rstrip() + "…"
    return rendered


def _plan_ends_with_ready_prose(plan: TaskPlan, run_result: PlannerRunResult) -> str | None:
    if not run_result.completed_steps:
        return None
    last_step_id = run_result.completed_steps[-1]
    last_step = next((step for step in plan.steps if step.id == last_step_id), None)
    if last_step is None:
        return None
    result = run_result.step_results.get(last_step_id)
    if not isinstance(result, Mapping):
        return None
    if last_step.executor == "generate_content":
        text = result.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    if last_step.executor == "check_safety":
        rewritten = result.get("rewritten_text")
        if isinstance(rewritten, str) and rewritten.strip():
            return rewritten.strip()
    return None


def _build_synthesis_task_text(user_task: str, plan: TaskPlan, run_result: PlannerRunResult) -> str:
    return (
        f"{_SYNTHESIS_INSTRUCTION}\n"
        f"Задача пользователя: {user_task}\n"
        f"Цель: {plan.goal}\n"
        f"Что нужно получить в итоге: {plan.final_output}\n\n"
        f"Результаты:\n{_render_step_results(run_result.step_results)}"
    )


def _fallback_summary_from_step_results(run_result: PlannerRunResult) -> str:
    rendered = _render_step_results(run_result.step_results)
    return (
        "Не удалось автоматически подготовить развёрнутые рекомендации. "
        "Вот результаты анализа для ручной проверки:\n\n"
        f"{rendered}\n\n"
        "Проверьте детали вручную перед использованием."
    )


async def build_final_reply(
    *,
    user_task: str,
    plan: TaskPlan,
    run_result: PlannerRunResult,
    context: PlannerExecutionContext,
    business_profile: BusinessProfile | None = None,
) -> tuple[str, bool]:
    """Returns (reply_text, used_llm_synthesis_call). Never raises, never
    returns an empty string."""
    ready_prose = _plan_ends_with_ready_prose(plan, run_result)
    if ready_prose is not None:
        log.info("planner_synthesis: reused ready prose, no extra LLM call")
        return ready_prose, False

    if context.llm_provider is None:
        return _fallback_summary_from_step_results(run_result), False

    if context.llm_call_budget is not None:
        try:
            context.llm_call_budget.consume(label="synthesis")
        except PlannerExecutionError:
            log.info("planner_synthesis: LLM call budget exhausted, using fallback summary")
            return _fallback_summary_from_step_results(run_result), False

    synthesis_task_text = _build_synthesis_task_text(user_task, plan, run_result)
    try:
        spec = MaterialOrchestrationService().build_free_text_generation_spec(
            context.workspace_id, synthesis_task_text, business_profile,
        )
        provider_request = build_provider_generation_request(spec)
        draft = await asyncio.to_thread(
            context.llm_provider.generate_draft,
            source_text=provider_request.source_text,
            material_type=provider_request.material_type,
            output_format=provider_request.output_format,
            mode="ai",
        )
    except Exception:
        log.warning("planner_synthesis: generate_draft raised", exc_info=True)
        draft = None

    if draft is None or not draft.text.strip():
        log.info("planner_synthesis: synthesis call failed/empty, using fallback summary")
        return _fallback_summary_from_step_results(run_result), True

    return draft.text.strip(), True
