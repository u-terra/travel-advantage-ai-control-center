from __future__ import annotations

import pytest

from app.orchestration.factory import (
    DEFAULT_ORCHESTRATION_LLM_PROVIDER,
    SUPPORTED_ORCHESTRATION_LLM_PROVIDERS,
    UnknownOrchestrationLLMProviderError,
    create_orchestration_llm_provider,
    normalize_orchestration_provider_name,
)
from app.orchestration.openai_provider import OpenAIOrchestrationProvider, OrchestrationOpenAIConfig
from app.orchestration.provider import NullOrchestrationLLMProvider


def test_default_provider_is_null_and_inert():
    provider = create_orchestration_llm_provider(None)
    assert isinstance(provider, NullOrchestrationLLMProvider)
    assert provider.is_configured is False
    assert provider.classify(request=object()) is None


def test_empty_string_falls_back_to_default():
    assert normalize_orchestration_provider_name("") == DEFAULT_ORCHESTRATION_LLM_PROVIDER
    assert normalize_orchestration_provider_name("   ") == DEFAULT_ORCHESTRATION_LLM_PROVIDER


def test_name_normalization_is_case_and_whitespace_insensitive():
    assert normalize_orchestration_provider_name(" NULL ") == "null"


def test_unknown_provider_name_fails_fast_not_silently():
    with pytest.raises(UnknownOrchestrationLLMProviderError):
        create_orchestration_llm_provider("yandex")


def test_supported_providers_lists_null():
    assert "null" in SUPPORTED_ORCHESTRATION_LLM_PROVIDERS


def test_supported_providers_lists_openai():
    assert "openai" in SUPPORTED_ORCHESTRATION_LLM_PROVIDERS


def test_openai_provider_selected_with_explicit_config():
    config = OrchestrationOpenAIConfig("key", "gpt-4o-mini", 5.0)
    provider = create_orchestration_llm_provider("openai", openai_config=config)
    assert isinstance(provider, OpenAIOrchestrationProvider)
    assert provider.is_configured is True


def test_openai_provider_without_config_is_safely_unconfigured():
    """No openai_config passed - must not raise, must not be is_configured."""
    provider = create_orchestration_llm_provider("openai")
    assert isinstance(provider, OpenAIOrchestrationProvider)
    assert provider.is_configured is False
    assert provider.classify(request=object()) is None


def test_openai_provider_with_empty_config_is_unconfigured():
    config = OrchestrationOpenAIConfig("", "", 0.0)
    provider = create_orchestration_llm_provider("openai", openai_config=config)
    assert provider.is_configured is False


def test_default_provider_ignores_openai_config():
    """Passing openai_config while selecting "null" must still yield the
    inert default - the kwarg is only consumed by the openai builder."""
    config = OrchestrationOpenAIConfig("key", "gpt-4o-mini", 5.0)
    provider = create_orchestration_llm_provider(None, openai_config=config)
    assert isinstance(provider, NullOrchestrationLLMProvider)
