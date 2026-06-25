from __future__ import annotations

from dataclasses import dataclass

from app.routing.keywords import (
    ASSISTANT_INTENT_KEYWORDS,
    ASSISTANT_TOPIC_KEYWORDS,
    CONTENT_KEYWORDS,
    PACKAGING_KEYWORDS,
    RADAR_KEYWORDS,
    SAFETY_KEYWORDS,
)
from app.routing.modules import Module
from app.routing.safety import SafetyLevel, detect_safety_level


@dataclass(frozen=True)
class RouteDecision:
    task_text: str
    primary_module: Module
    secondary_modules: tuple[Module, ...]
    safety_level: SafetyLevel
    is_mixed: bool
    is_uncertain: bool
    matched_modules: tuple[Module, ...]
    notes: tuple[str, ...]


# Порядок разрешения коллизий при равном счёте.
# Safety раньше остальных: «проверь пост» → Safety, не Content.
MODULE_PRIORITY: tuple[Module, ...] = (
    Module.SAFETY_LAYER,
    Module.PARTNER_PACKAGING,
    Module.LEAD_RADAR,
    Module.CONTENT_FACTORY,
    Module.TRAVEL_ASSISTANT,
)


def _count_matches(text_lower: str, keywords: tuple[str, ...]) -> int:
    return sum(1 for kw in keywords if kw in text_lower)


def _sort_matched(scores: dict[Module, int]) -> tuple[Module, ...]:
    def key(m: Module) -> tuple[int, int]:
        priority_index = MODULE_PRIORITY.index(m) if m in MODULE_PRIORITY else 99
        return (-scores[m], priority_index)
    return tuple(sorted(scores.keys(), key=key))


def _promote_safety(
    current: SafetyLevel,
    scores: dict[Module, int],
    primary: Module,
) -> SafetyLevel:
    if Module.SAFETY_LAYER in scores and primary is not Module.SAFETY_LAYER:
        if current is SafetyLevel.NOT_REQUIRED:
            return SafetyLevel.RECOMMENDED
    return current


def route_text(task_text: str) -> RouteDecision:
    text_lower = task_text.lower()

    content_score = _count_matches(text_lower, CONTENT_KEYWORDS)
    intent_score = _count_matches(text_lower, ASSISTANT_INTENT_KEYWORDS)
    topic_score = _count_matches(text_lower, ASSISTANT_TOPIC_KEYWORDS)
    radar_score = _count_matches(text_lower, RADAR_KEYWORDS)
    safety_kw_score = _count_matches(text_lower, SAFETY_KEYWORDS)
    packaging_score = _count_matches(text_lower, PACKAGING_KEYWORDS)

    # Детерминированное правило приоритета:
    # если есть явное намерение создать контент и нет явного клиентского сигнала,
    # предметные слова (тариф / travel advantage / life experiences / брониров / оплат)
    # — это просто тема контента, а не запрос в AI Travel Assistant.
    if content_score > 0 and intent_score == 0:
        assistant_score = 0
    else:
        assistant_score = intent_score + topic_score

    scores: dict[Module, int] = {}
    if content_score > 0:
        scores[Module.CONTENT_FACTORY] = content_score
    if assistant_score > 0:
        scores[Module.TRAVEL_ASSISTANT] = assistant_score
    if radar_score > 0:
        scores[Module.LEAD_RADAR] = radar_score
    if safety_kw_score > 0:
        scores[Module.SAFETY_LAYER] = safety_kw_score
    if packaging_score > 0:
        scores[Module.PARTNER_PACKAGING] = packaging_score

    safety = detect_safety_level(text_lower)
    notes: list[str] = []

    if not scores:
        return RouteDecision(
            task_text=task_text,
            primary_module=Module.ORCHESTRATOR,
            secondary_modules=(),
            safety_level=safety,
            is_mixed=False,
            is_uncertain=True,
            matched_modules=(),
            notes=("Маршрут не определён уверенно. Выберите категорию задачи кнопкой меню.",),
        )

    matched = _sort_matched(scores)
    workflow_only_text = set(matched) <= {Module.CONTENT_FACTORY, Module.SAFETY_LAYER}

    if len(matched) >= 2 and not workflow_only_text:
        primary = matched[0]
        secondary = tuple(m for m in matched[1:] if m is not Module.SAFETY_LAYER)
        notes.append("Задача содержит несколько направлений. Бот разложил её на части.")
        return RouteDecision(
            task_text=task_text,
            primary_module=primary,
            secondary_modules=secondary,
            safety_level=_promote_safety(safety, scores, primary),
            is_mixed=True,
            is_uncertain=False,
            matched_modules=matched,
            notes=tuple(notes),
        )

    if workflow_only_text and len(matched) >= 2:
        primary = matched[0]
        secondary = (Module.CONTENT_FACTORY,) if primary is Module.SAFETY_LAYER else ()
    else:
        primary = matched[0]
        secondary = ()

    if primary is Module.SAFETY_LAYER:
        safety = SafetyLevel.MANDATORY
    else:
        safety = _promote_safety(safety, scores, primary)

    return RouteDecision(
        task_text=task_text,
        primary_module=primary,
        secondary_modules=secondary,
        safety_level=safety,
        is_mixed=False,
        is_uncertain=False,
        matched_modules=matched,
        notes=tuple(notes),
    )


def route_for_button(button_module: Module, task_text: str) -> RouteDecision:
    """Маршрут, когда владелец нажал кнопку категории и ввёл текст задачи."""
    text_lower = task_text.lower()
    safety = detect_safety_level(text_lower)
    if button_module is Module.SAFETY_LAYER:
        safety = SafetyLevel.MANDATORY
    return RouteDecision(
        task_text=task_text,
        primary_module=button_module,
        secondary_modules=(),
        safety_level=safety,
        is_mixed=False,
        is_uncertain=False,
        matched_modules=(button_module,),
        notes=(),
    )
