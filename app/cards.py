from __future__ import annotations

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
    lines.append("Ручное решение Владимира:")
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
