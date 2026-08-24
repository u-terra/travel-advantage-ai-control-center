from __future__ import annotations

from app.domain.content import SourceAnalysis
from app.domain.content_intelligence import (
    ACTION_ADAPT,
    ACTION_GENERATE_TOPICS,
    ACTION_MAKE_POST,
    ACTION_MAKE_SCRIPT,
    ACTION_OBSERVE,
    ACTION_SKIP,
    KIND_CASE_OR_REVIEW,
    KIND_COMPETITOR_SIGNAL,
    KIND_NEWS,
    KIND_NOISE,
    KIND_POST_IDEA,
    KIND_SCRIPT_IDEA,
    MaterialClassification,
)

# Владелец читает человеческие формулировки, а не значения контракта.
MATERIAL_KIND_LABELS: dict[str, str] = {
    KIND_NEWS: "новость",
    KIND_POST_IDEA: "идея для поста",
    KIND_SCRIPT_IDEA: "идея для сценария",
    KIND_CASE_OR_REVIEW: "кейс или отзыв",
    KIND_COMPETITOR_SIGNAL: "сигнал рынка или конкурента",
    KIND_NOISE: "малополезный материал",
}

MATERIAL_ACTION_LABELS: dict[str, str] = {
    ACTION_MAKE_POST: "сделать пост",
    ACTION_MAKE_SCRIPT: "сделать сценарий",
    ACTION_GENERATE_TOPICS: "дать темы",
    ACTION_ADAPT: "адаптировать под Travel Advantage",
    ACTION_OBSERVE: "наблюдать",
    ACTION_SKIP: "пропустить",
}


def _classification_block(classification: MaterialClassification) -> str:
    # Незнакомое значение показывается как есть: без подписи владелец увидит
    # хотя бы контрактное имя, а не пустую строку.
    kind = MATERIAL_KIND_LABELS.get(classification.kind, classification.kind)
    action = MATERIAL_ACTION_LABELS.get(classification.action, classification.action)
    return f"Тип: {kind}\nРекомендация: {action}"


def source_analysis_card(
    analysis: SourceAnalysis,
    limit: int = 3900,
    *,
    classification: MaterialClassification | None = None,
) -> str:
    """Компактная Telegram-карточка (Проблема 6): владелец видит только то,
    что нужно для решения «что делать с этим материалом» — ценность, что
    уточнить и как подать. Полный разбор (audience_value, target_audiences и
    т.д.) никуда не пропадает — он остаётся в SourceAnalysis и в БД, просто
    не дублируется в Telegram-сообщении.
    """
    def clip(value: str, size: int) -> str:
        return value if len(value) <= size else value[: size - 1].rstrip() + "…"

    value_lines: list[str] = []
    if analysis.summary.strip():
        value_lines.append(clip(analysis.summary, 500))
    value_lines.extend(f"• {clip(fact, 200)}" for fact in analysis.key_facts[:4])
    value_block = (
        "💎 Что здесь ценного\n" + "\n".join(value_lines) if value_lines else None
    )

    check_lines = [f"• {clip(claim, 200)}" for claim in analysis.disputed_claims[:4]]
    check_lines.extend(f"• {clip(warning, 200)}" for warning in analysis.warnings[:2])
    check_block = (
        "🔍 Что нужно проверить / уточнить\n" + "\n".join(check_lines)
        if check_lines else None
    )

    angle_lines = [f"• {clip(angle, 200)}" for angle in analysis.content_angles[:3]]
    if analysis.recommended_formats:
        angle_lines.append(
            "Форматы: " + ", ".join(clip(f, 40) for f in analysis.recommended_formats[:4])
        )
    angle_block = (
        "✍️ Как лучше подать\n" + "\n".join(angle_lines) if angle_lines else None
    )

    parts = ["🔎 Анализ источника"]
    # Сразу после заголовка и до текстового разбора: это ответ на вопрос «что
    # с этим делать», и он не должен пострадать от обрезки по лимиту.
    if classification is not None:
        parts.append(_classification_block(classification))
    for block in (value_block, check_block, angle_block):
        if block is None:
            continue
        remaining = limit - len("\n\n".join(parts)) - 2
        if remaining <= 20:
            continue
        parts.append(clip(block, remaining))
    return "\n\n".join(parts)

from app.routing.modules import MODULE_DESCRIPTION, Module
from app.routing.router import RouteDecision
from app.routing.safety import SafetyLevel


_NEXT_STEP: dict[Module, str] = {
    Module.CONTENT_FACTORY: "Открыть Travel Content Factory и подготовить черновик текста.",
    Module.TRAVEL_ASSISTANT: "Подготовить безопасный черновик ответа клиенту и сверить условия вручную.",
    Module.LEAD_RADAR: "Открыть AI Lead Radar, посмотреть список сигналов и выбрать релевантные.",
    Module.SAFETY_LAYER: "Прогнать текст через Safety Layer и собрать список спорных формулировок.",
    Module.PARTNER_PACKAGING: "Открыть материалы Partner Packaging и подготовить документ для партнёра.",
    Module.ORCHESTRATOR: "Уточнить тип задачи кнопкой главного меню.",
}

_EXPECTED_RESULT: dict[Module, str] = {
    Module.CONTENT_FACTORY: "Черновик текста для ручной доработки и публикации.",
    Module.TRAVEL_ASSISTANT: "Черновик ответа клиенту без неподтверждённых обещаний.",
    Module.LEAD_RADAR: "Список сигналов с краткой оценкой релевантности.",
    Module.SAFETY_LAYER: "Список рисков и предложенные безопасные формулировки.",
    Module.PARTNER_PACKAGING: "Черновик инструкции, презентации или коммерческого предложения.",
    Module.ORCHESTRATOR: "Разбиение задачи на отдельные маршруты.",
}

_MANUAL_DECISION: dict[Module, str] = {
    Module.CONTENT_FACTORY: "Выбрать итоговую версию и опубликовать вручную.",
    Module.TRAVEL_ASSISTANT: "Дать человеку личный ответ и подтвердить актуальные условия.",
    Module.LEAD_RADAR: "Решить, стоит ли реагировать на сигнал и кому писать.",
    Module.SAFETY_LAYER: "Принять решение о публикации или переписывании текста.",
    Module.PARTNER_PACKAGING: "Утвердить содержание, условия и поддержку партнёра.",
    Module.ORCHESTRATOR: "Выбрать приоритетную часть задачи и пройти её отдельно.",
}


def build_card(decision: RouteDecision) -> str:
    lines: list[str] = []
    lines.append("📌 Карточка маршрута")
    lines.append("")
    lines.append("Задача:")
    lines.append(decision.task_text.strip() or "—")
    lines.append("")
    lines.append("Основной модуль:")
    lines.append(decision.primary_module.value)
    lines.append(MODULE_DESCRIPTION[decision.primary_module])
    lines.append("")
    lines.append("Дополнительный модуль:")
    if decision.secondary_modules:
        lines.append(", ".join(m.value for m in decision.secondary_modules))
    else:
        lines.append("не требуется")
    lines.append("")
    lines.append("Safety Layer:")
    lines.append(decision.safety_level.value)
    lines.append("")
    lines.append("Следующий шаг:")
    lines.append(_NEXT_STEP[decision.primary_module])
    lines.append("")
    lines.append("Ожидаемый результат:")
    lines.append(_EXPECTED_RESULT[decision.primary_module])
    lines.append("")
    lines.append("Ваше решение:")
    lines.append(_MANUAL_DECISION[decision.primary_module])
    if decision.safety_level is SafetyLevel.MANDATORY:
        lines.append("Перед отправкой обязательно сверить факты, цены, условия и риски вручную.")

    if decision.is_mixed:
        lines.append("")
        lines.append("⚠️ Задача смешанная. Разбиение по маршрутам:")
        modules = (decision.primary_module, *decision.secondary_modules)
        for i, m in enumerate(modules, start=1):
            lines.append(f"{i}. {m.value} — {MODULE_DESCRIPTION[m]}")
        lines.append("Сначала выберите, с какой части задачи начнём.")

    if decision.is_uncertain:
        lines.append("")
        lines.append("⚠️ Маршрут не определён уверенно.")
        lines.append("Выберите категорию задачи кнопкой главного меню.")

    for note in decision.notes:
        lines.append("")
        lines.append(note)

    return "\n".join(lines)
