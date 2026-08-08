"""Адаптер OpenAI-провайдера.

Доступ к OpenAI в этом проекте идёт не через SDK, а через внутренний HTTP-API
Travel Content Factory на том же VPS: именно он держит ключ вендора и вызывает
модель. Поэтому «работа с конкретным API» для нас — это HTTP-транспорт в
``app.services.content_factory``, и весь он спрятан за этим адаптером.

Ниже нет ни одного промпта и ни одного продуктового решения: адаптер только
передаёт готовый ``source_text`` дальше и возвращает нормализованные модели.
"""

from __future__ import annotations

from app.services.content_factory import (
    ContentFactoryConfig,
    analyze_source_sync,
    check_text_sync,
    generate_draft_sync,
)
from app.services.llm.base import LLMProvider
from app.services.llm.models import (
    ContentDraft,
    SourceAnalysisPayload,
    TextCheckResult,
)

PROVIDER_NAME = "openai"


class OpenAIContentFactoryProvider(LLMProvider):
    """OpenAI-бэкенд, доступный через внутренний API Travel Content Factory."""

    name = PROVIDER_NAME

    def __init__(self, config: ContentFactoryConfig) -> None:
        self._config = config

    @property
    def is_configured(self) -> bool:
        return self._config.is_configured

    def generate_draft(
        self,
        *,
        source_text: str,
        material_type: str,
        output_format: str,
        mode: str,
    ) -> ContentDraft | None:
        return generate_draft_sync(
            self._config,
            source_text=source_text,
            material_type=material_type,
            output_format=output_format,
            mode=mode,
        )

    def check_text(self, *, source_text: str) -> TextCheckResult | None:
        return check_text_sync(self._config, source_text=source_text)

    def analyze_source(self, *, source_text: str) -> SourceAnalysisPayload | None:
        return analyze_source_sync(self._config, source_text=source_text)
