"""Общий интерфейс LLM-провайдера.

Бизнес-логика зависит только от этого интерфейса. Конкретный вендор (сейчас —
OpenAI через Travel Content Factory, в будущем YandexGPT) подключается
отдельным адаптером и не влияет на сценарии Оркестратора.

Границы ответственности:
- провайдер отвечает за работу с конкретным API: транспорт, таймауты, разбор
  ответа, приведение к моделям из ``app.services.llm.models``;
- промпты, системные инструкции и сценарии остаются в бизнес-логике и
  приходят сюда уже готовым ``source_text``.

Методы блокирующие: вызывающий код выполняет их через ``asyncio.to_thread``,
как это было с прежними ``*_sync``-функциями. Любая ошибка — сеть, таймаут,
некорректный ответ — возвращает ``None`` и не пробрасывает наружу технические
детали.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.services.llm.models import (
    ContentDraft,
    SourceAnalysisPayload,
    TextCheckResult,
)


class LLMProvider(ABC):
    """Провайдер-агностичный контракт LLM-операций Оркестратора."""

    #: Идентификатор провайдера, совпадающий со значением LLM_PROVIDER.
    name: str = ""

    @property
    @abstractmethod
    def is_configured(self) -> bool:
        """True, если провайдер получил всё необходимое для вызовов."""

    @abstractmethod
    def generate_draft(
        self,
        *,
        source_text: str,
        material_type: str,
        output_format: str,
        mode: str,
    ) -> ContentDraft | None:
        """Сгенерировать черновик материала. None — при любой ошибке."""

    @abstractmethod
    def check_text(self, *, source_text: str) -> TextCheckResult | None:
        """Проверить текст Safety Layer. None — при любой ошибке."""

    @abstractmethod
    def analyze_source(self, *, source_text: str) -> SourceAnalysisPayload | None:
        """Разобрать источник. None — при любой ошибке."""
