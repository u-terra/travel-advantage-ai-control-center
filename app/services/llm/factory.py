"""Выбор LLM-провайдера по конфигурации.

Единственное место, где имя из ``LLM_PROVIDER`` превращается в конкретный
адаптер. Чтобы подключить нового вендора, достаточно добавить адаптер и одну
строку в ``_BUILDERS`` — бизнес-логика не меняется.
"""

from __future__ import annotations

from typing import Callable, Mapping

from app.services.content_factory import ContentFactoryConfig
from app.services.llm.base import LLMProvider
from app.services.llm.openai_provider import (
    PROVIDER_NAME as OPENAI_PROVIDER_NAME,
    OpenAIContentFactoryProvider,
)

#: Значение по умолчанию: без LLM_PROVIDER в окружении поведение не меняется.
DEFAULT_LLM_PROVIDER = OPENAI_PROVIDER_NAME


class UnknownLLMProviderError(ValueError):
    """LLM_PROVIDER указывает на провайдера, для которого нет адаптера."""

    def __init__(self, name: str, supported: tuple[str, ...]) -> None:
        self.name = name
        self.supported = supported
        super().__init__(
            f"Неизвестный LLM_PROVIDER: {name!r}. "
            f"Доступные значения: {', '.join(supported)}."
        )


_BUILDERS: Mapping[str, Callable[[ContentFactoryConfig], LLMProvider]] = {
    OPENAI_PROVIDER_NAME: OpenAIContentFactoryProvider,
}

#: Провайдеры, для которых есть рабочий адаптер.
SUPPORTED_LLM_PROVIDERS: tuple[str, ...] = tuple(sorted(_BUILDERS))


def normalize_provider_name(raw: str | None) -> str:
    """Приводит значение LLM_PROVIDER к каноническому виду.

    Пустое значение и отсутствие переменной означают провайдера по умолчанию —
    так сохраняется обратная совместимость с текущим .env.
    """
    name = (raw or "").strip().lower()
    return name or DEFAULT_LLM_PROVIDER


def create_llm_provider(
    provider_name: str | None,
    *,
    content_factory_config: ContentFactoryConfig,
) -> LLMProvider:
    """Создаёт провайдера по имени.

    Бросает :class:`UnknownLLMProviderError` для неподдерживаемого значения:
    падать на старте понятнее, чем молча уходить не к тому вендору.
    """
    name = normalize_provider_name(provider_name)
    builder = _BUILDERS.get(name)
    if builder is None:
        raise UnknownLLMProviderError(name, SUPPORTED_LLM_PROVIDERS)
    return builder(content_factory_config)
