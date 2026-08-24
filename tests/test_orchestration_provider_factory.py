from __future__ import annotations

import pytest

from app.orchestration.factory import (
    DEFAULT_ORCHESTRATION_LLM_PROVIDER,
    SUPPORTED_ORCHESTRATION_LLM_PROVIDERS,
    UnknownOrchestrationLLMProviderError,
    create_orchestration_llm_provider,
    normalize_orchestration_provider_name,
)
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
