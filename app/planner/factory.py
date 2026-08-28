"""Vendor selection for the Planner LLM - mirrors
``app.orchestration.factory`` so swapping/adding a vendor later means adding
one adapter + one line here, not touching Planner business logic.

Registered providers: the safe no-op default (``null``) and the first real
adapter (``openai``, direct HTTPS call - see ``app.planner.openai_provider``).
"""

from __future__ import annotations

from typing import Callable, Mapping

from app.planner.openai_provider import (
    PROVIDER_NAME as OPENAI_PROVIDER_NAME,
    OpenAIPlannerProvider,
    PlannerOpenAIConfig,
)
from app.planner.provider import NullPlannerLLMProvider, PlannerLLMProvider

DEFAULT_PLANNER_LLM_PROVIDER = NullPlannerLLMProvider.name

#: Safe default for "openai" if no config is passed explicitly - the
#: provider is constructed, but is_configured stays False (like
#: NullPlannerLLMProvider).
_UNCONFIGURED_OPENAI = PlannerOpenAIConfig(api_key="", model="", timeout_seconds=0.0)


class UnknownPlannerLLMProviderError(ValueError):
    def __init__(self, name: str, supported: tuple[str, ...]) -> None:
        self.name = name
        self.supported = supported
        super().__init__(
            f"Неизвестный PLANNER_LLM_PROVIDER: {name!r}. "
            f"Доступные значения: {', '.join(supported)}."
        )


def _build_null(_config: PlannerOpenAIConfig | None) -> PlannerLLMProvider:
    return NullPlannerLLMProvider()


def _build_openai(config: PlannerOpenAIConfig | None) -> PlannerLLMProvider:
    return OpenAIPlannerProvider(config or _UNCONFIGURED_OPENAI)


_BUILDERS: Mapping[str, Callable[[PlannerOpenAIConfig | None], PlannerLLMProvider]] = {
    NullPlannerLLMProvider.name: _build_null,
    OPENAI_PROVIDER_NAME: _build_openai,
}

SUPPORTED_PLANNER_LLM_PROVIDERS: tuple[str, ...] = tuple(sorted(_BUILDERS))


def normalize_planner_provider_name(raw: str | None) -> str:
    name = (raw or "").strip().lower()
    return name or DEFAULT_PLANNER_LLM_PROVIDER


def create_planner_llm_provider(
    provider_name: str | None,
    *,
    openai_config: PlannerOpenAIConfig | None = None,
) -> PlannerLLMProvider:
    name = normalize_planner_provider_name(provider_name)
    builder = _BUILDERS.get(name)
    if builder is None:
        raise UnknownPlannerLLMProviderError(name, SUPPORTED_PLANNER_LLM_PROVIDERS)
    return builder(openai_config)
