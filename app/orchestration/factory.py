"""Vendor selection for the orchestration LLM - mirrors
``app.services.llm.factory`` so swapping OpenAI/Yandex/other later means
adding one adapter + one line here, not touching orchestration logic.

Phase 1 registered only the safe no-op default. Phase 2 adds the first real
adapter (OpenAI, direct HTTPS call - see ``app.orchestration.openai_provider``
for why it does not go through Content Factory). Adding another vendor later
is additive - this factory is the only place that changes.
"""

from __future__ import annotations

from typing import Callable, Mapping

from app.orchestration.openai_provider import (
    PROVIDER_NAME as OPENAI_PROVIDER_NAME,
    OpenAIOrchestrationProvider,
    OrchestrationOpenAIConfig,
)
from app.orchestration.provider import NullOrchestrationLLMProvider, OrchestrationLLMProvider

DEFAULT_ORCHESTRATION_LLM_PROVIDER = NullOrchestrationLLMProvider.name

#: Безопасный дефолт для "openai", если конфиг не передан явно - провайдер
#: создаётся, но ``is_configured`` остаётся False (как и NullOrchestrationLLMProvider).
_UNCONFIGURED_OPENAI = OrchestrationOpenAIConfig(api_key="", model="", timeout_seconds=0.0)


class UnknownOrchestrationLLMProviderError(ValueError):
    def __init__(self, name: str, supported: tuple[str, ...]) -> None:
        self.name = name
        self.supported = supported
        super().__init__(
            f"Неизвестный ORCHESTRATION_LLM_PROVIDER: {name!r}. "
            f"Доступные значения: {', '.join(supported)}."
        )


def _build_null(_config: OrchestrationOpenAIConfig | None) -> OrchestrationLLMProvider:
    return NullOrchestrationLLMProvider()


def _build_openai(config: OrchestrationOpenAIConfig | None) -> OrchestrationLLMProvider:
    return OpenAIOrchestrationProvider(config or _UNCONFIGURED_OPENAI)


_BUILDERS: Mapping[str, Callable[[OrchestrationOpenAIConfig | None], OrchestrationLLMProvider]] = {
    NullOrchestrationLLMProvider.name: _build_null,
    OPENAI_PROVIDER_NAME: _build_openai,
}

SUPPORTED_ORCHESTRATION_LLM_PROVIDERS: tuple[str, ...] = tuple(sorted(_BUILDERS))


def normalize_orchestration_provider_name(raw: str | None) -> str:
    name = (raw or "").strip().lower()
    return name or DEFAULT_ORCHESTRATION_LLM_PROVIDER


def create_orchestration_llm_provider(
    provider_name: str | None,
    *,
    openai_config: OrchestrationOpenAIConfig | None = None,
) -> OrchestrationLLMProvider:
    name = normalize_orchestration_provider_name(provider_name)
    builder = _BUILDERS.get(name)
    if builder is None:
        raise UnknownOrchestrationLLMProviderError(name, SUPPORTED_ORCHESTRATION_LLM_PROVIDERS)
    return builder(openai_config)
