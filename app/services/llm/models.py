"""Нормализованные модели ответа LLM-слоя.

Эти структуры — единый контракт между бизнес-логикой и любым провайдером.
Ни одно поле здесь не повторяет сырой формат ответа конкретного API: адаптер
провайдера обязан привести свой ответ к этим типам. Благодаря этому хендлеры,
репозитории и карточки не знают, какая модель и какой вендор использовались.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContentDraft:
    """Черновик материала: текст плюс предупреждения для ручной проверки."""

    text: str
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class TextSafetyFinding:
    phrase: str
    warning: str


@dataclass(frozen=True)
class TextCheckResult:
    """Результат проверки текста Safety Layer."""

    warnings: tuple[TextSafetyFinding, ...]
    rewritten_text: str | None
    rewrite_warnings: tuple[TextSafetyFinding, ...]
    generation_mode: str | None
    ai_note: str | None


@dataclass(frozen=True)
class SourceAnalysisPayload:
    """Разбор источника на факты, аудитории и возможные форматы материалов."""

    summary: str
    key_facts: tuple[str, ...]
    disputed_claims: tuple[str, ...]
    audience_value: str
    target_audiences: tuple[str, ...]
    content_angles: tuple[str, ...]
    recommended_formats: tuple[str, ...]
    warnings: tuple[str, ...]
