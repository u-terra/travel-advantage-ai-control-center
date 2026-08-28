from __future__ import annotations

import pytest

from app.planner.factory import (
    DEFAULT_PLANNER_LLM_PROVIDER,
    SUPPORTED_PLANNER_LLM_PROVIDERS,
    UnknownPlannerLLMProviderError,
    create_planner_llm_provider,
    normalize_planner_provider_name,
)
from app.planner.openai_provider import OpenAIPlannerProvider, PlannerOpenAIConfig
from app.planner.provider import NullPlannerLLMProvider
from app.planner.request import build_planner_request

_REQUEST = build_planner_request("Проанализируй конкурента X")


def test_default_provider_is_null_and_inert():
    provider = create_planner_llm_provider(None)
    assert isinstance(provider, NullPlannerLLMProvider)
    assert provider.is_configured is False
    assert provider.plan(request=_REQUEST) is None


def test_empty_string_falls_back_to_default():
    assert normalize_planner_provider_name("") == DEFAULT_PLANNER_LLM_PROVIDER
    assert normalize_planner_provider_name("   ") == DEFAULT_PLANNER_LLM_PROVIDER


def test_name_normalization_is_case_and_whitespace_insensitive():
    assert normalize_planner_provider_name(" NULL ") == "null"


def test_unknown_provider_name_fails_fast_not_silently():
    with pytest.raises(UnknownPlannerLLMProviderError):
        create_planner_llm_provider("yandex")


def test_supported_providers_lists_null():
    assert "null" in SUPPORTED_PLANNER_LLM_PROVIDERS


def test_supported_providers_lists_openai():
    assert "openai" in SUPPORTED_PLANNER_LLM_PROVIDERS


def test_openai_provider_selected_with_explicit_config():
    config = PlannerOpenAIConfig("key", "gpt-4o-mini", 5.0)
    provider = create_planner_llm_provider("openai", openai_config=config)
    assert isinstance(provider, OpenAIPlannerProvider)
    assert provider.is_configured is True


def test_openai_provider_without_config_is_safely_unconfigured():
    provider = create_planner_llm_provider("openai")
    assert isinstance(provider, OpenAIPlannerProvider)
    assert provider.is_configured is False
    assert provider.plan(request=_REQUEST) is None


def test_openai_provider_with_empty_config_is_unconfigured():
    config = PlannerOpenAIConfig("", "", 0.0)
    provider = create_planner_llm_provider("openai", openai_config=config)
    assert provider.is_configured is False


def test_default_provider_ignores_openai_config():
    config = PlannerOpenAIConfig("key", "gpt-4o-mini", 5.0)
    provider = create_planner_llm_provider(None, openai_config=config)
    assert isinstance(provider, NullPlannerLLMProvider)
