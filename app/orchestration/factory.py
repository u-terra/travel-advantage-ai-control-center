"""Vendor selection for the orchestration LLM - mirrors
``app.services.llm.factory`` so swapping OpenAI/Yandex/other later means
adding one adapter + one line here, not touching orchestration logic.

Phase 1 only registers the safe no-op default: no real network adapter
exists yet because Content Factory does not currently expose a generic
classification endpoint (see the Phase 1 architecture report). Adding a real
adapter later is additive - this factory is the only place that changes.
"""

from __future__ import annotations

from typing import Callable, Mapping

from app.orchestration.provider import NullOrchestrationLLMProvider, OrchestrationLLMProvider

DEFAULT_ORCHESTRATION_LLM_PROVIDER = NullOrchestrationLLMProvider.name


class UnknownOrchestrationLLMProviderError(ValueError):
    def __init__(self, name: str, supported: tuple[str, ...]) -> None:
        self.name = name
        self.supported = supported
        super().__init__(
            f"Неизвестный ORCHESTRATION_LLM_PROVIDER: {name!r}. "
            f"Доступные значения: {', '.join(supported)}."
        )


_BUILDERS: Mapping[str, Callable[[], OrchestrationLLMProvider]] = {
    NullOrchestrationLLMProvider.name: NullOrchestrationLLMProvider,
}

SUPPORTED_ORCHESTRATION_LLM_PROVIDERS: tuple[str, ...] = tuple(sorted(_BUILDERS))


def normalize_orchestration_provider_name(raw: str | None) -> str:
    name = (raw or "").strip().lower()
    return name or DEFAULT_ORCHESTRATION_LLM_PROVIDER


def create_orchestration_llm_provider(provider_name: str | None) -> OrchestrationLLMProvider:
    name = normalize_orchestration_provider_name(provider_name)
    builder = _BUILDERS.get(name)
    if builder is None:
        raise UnknownOrchestrationLLMProviderError(name, SUPPORTED_ORCHESTRATION_LLM_PROVIDERS)
    return builder()
