from __future__ import annotations

from enum import Enum


class Module(str, Enum):
    CONTENT_FACTORY = "Travel Content Factory"
    TRAVEL_ASSISTANT = "AI Travel Assistant"
    LEAD_RADAR = "AI Lead Radar"
    SAFETY_LAYER = "Safety Layer"
    PARTNER_PACKAGING = "Partner Packaging"
    ORCHESTRATOR = "Orchestrator"


MODULE_DESCRIPTION: dict[Module, str] = {
    Module.CONTENT_FACTORY: "Создание текстов: посты, Reels, Stories, сценарии, ответы на возражения, контент-планы.",
    Module.TRAVEL_ASSISTANT: "Ответы клиентам, заявки, мини-диагностика, вопросы о Travel Advantage / Life Experiences.",
    Module.LEAD_RADAR: "Поиск публичных сигналов интереса, темы спроса, идеи для контента.",
    Module.SAFETY_LAYER: "Проверка рискованных формулировок и неподтверждённых обещаний.",
    Module.PARTNER_PACKAGING: "Инструкции, предложения, презентации и материалы для партнёров.",
    Module.ORCHESTRATOR: "Разбор смешанной или неясной задачи на отдельные маршруты.",
}
