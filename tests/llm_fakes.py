"""Заглушка LLM-провайдера для тестов.

Тесты бизнес-логики работают через общий интерфейс и никогда не выходят в
сеть: ни к OpenAI, ни к Travel Content Factory. Методы — ``Mock``, поэтому
проверки вида ``provider.check_text.assert_called_once()`` остаются такими же
прямыми, как прежние ``patch(...)`` по имени функции.
"""

from __future__ import annotations

from unittest.mock import Mock

from app.services.llm.models import (
    ContentDraft,
    SourceAnalysisPayload,
    TextCheckResult,
)


class FakeLLMProvider:
    """Duck-typed провайдер: реализует контракт ``LLMProvider`` без сети."""

    name = "fake"
    is_configured = True

    def __init__(
        self,
        *,
        draft: ContentDraft | None = None,
        check: TextCheckResult | None = None,
        analysis: SourceAnalysisPayload | None = None,
    ) -> None:
        self.generate_draft = Mock(return_value=draft)
        self.check_text = Mock(return_value=check)
        self.analyze_source = Mock(return_value=analysis)
