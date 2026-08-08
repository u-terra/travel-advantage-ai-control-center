"""Тесты provider-agnostic LLM-слоя.

Ни один тест не выходит в сеть: транспорт подменяется на уровне
``urllib.request.urlopen`` либо вообще не вызывается.
"""

from __future__ import annotations

import inspect
import json
from unittest.mock import patch

import pytest

from app.services.content_factory import ContentFactoryConfig
from app.services.llm.base import LLMProvider
from app.services.llm.factory import (
    DEFAULT_LLM_PROVIDER,
    SUPPORTED_LLM_PROVIDERS,
    UnknownLLMProviderError,
    create_llm_provider,
    normalize_provider_name,
)
from app.services.llm.models import (
    ContentDraft,
    SourceAnalysisPayload,
    TextCheckResult,
    TextSafetyFinding,
)
from app.services.llm.openai_provider import OpenAIContentFactoryProvider
from tests.llm_fakes import FakeLLMProvider


CONFIG = ContentFactoryConfig("http://factory/internal/generate", "secret-token", 7.5)


class Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return json.dumps(self.value, ensure_ascii=False).encode()


# --- Выбор провайдера -------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_missing_llm_provider_keeps_openai_as_default(raw: str | None) -> None:
    """Обратная совместимость: без LLM_PROVIDER установка работает как раньше."""
    assert normalize_provider_name(raw) == "openai"
    provider = create_llm_provider(raw, content_factory_config=CONFIG)
    assert isinstance(provider, OpenAIContentFactoryProvider)
    assert provider.name == DEFAULT_LLM_PROVIDER == "openai"


@pytest.mark.parametrize("raw", ["openai", "OpenAI", "  OPENAI  "])
def test_explicit_openai_selects_openai_adapter(raw: str) -> None:
    provider = create_llm_provider(raw, content_factory_config=CONFIG)
    assert isinstance(provider, OpenAIContentFactoryProvider)


@pytest.mark.parametrize("raw", ["yandex", "anthropic", "openai-compatible", "-"])
def test_unknown_provider_fails_loudly_and_lists_supported(raw: str) -> None:
    with pytest.raises(UnknownLLMProviderError) as exc:
        create_llm_provider(raw, content_factory_config=CONFIG)
    message = str(exc.value)
    assert raw.lower() in message
    assert "openai" in message
    assert exc.value.supported == SUPPORTED_LLM_PROVIDERS


def test_error_message_never_leaks_the_token() -> None:
    with pytest.raises(UnknownLLMProviderError) as exc:
        create_llm_provider("yandex", content_factory_config=CONFIG)
    assert "secret-token" not in str(exc.value)


# --- Контракт интерфейса ----------------------------------------------------


def test_openai_adapter_implements_the_shared_interface() -> None:
    provider = create_llm_provider("openai", content_factory_config=CONFIG)
    assert isinstance(provider, LLMProvider)
    assert provider.is_configured is True
    assert (
        create_llm_provider(
            "openai", content_factory_config=ContentFactoryConfig("", "", 1)
        ).is_configured
        is False
    )


def test_fake_provider_covers_every_method_of_the_interface() -> None:
    """Заглушка тестов не должна отставать от интерфейса."""
    fake = FakeLLMProvider()
    for name in LLMProvider.__abstractmethods__:
        assert hasattr(fake, name), name


def test_business_methods_take_only_provider_neutral_arguments() -> None:
    """В сигнатурах нет model/temperature/api_key — вендор не протекает наружу."""
    forbidden = {"model", "temperature", "api_key", "base_url", "client", "config"}
    for name in ("generate_draft", "check_text", "analyze_source"):
        signature = inspect.signature(getattr(LLMProvider, name))
        assert forbidden.isdisjoint(signature.parameters), name


# --- Нормализация ответа ----------------------------------------------------


def test_draft_is_normalized_without_raw_vendor_payload() -> None:
    raw = {"ok": True, "text": " Черновик ", "warnings": [" Проверить "], "model": "gpt-x"}
    with patch("urllib.request.urlopen", return_value=Response(raw)):
        draft = create_llm_provider(
            "openai", content_factory_config=CONFIG
        ).generate_draft(
            source_text="Контекст",
            material_type="market_offer",
            output_format="telegram",
            mode="ai",
        )
    assert isinstance(draft, ContentDraft)
    assert draft == ContentDraft(text="Черновик", warnings=("Проверить",))
    assert not hasattr(draft, "model")


def test_check_text_is_normalized() -> None:
    raw = {
        "ok": True, "rewritten_text": " Лучше ", "ai_note": "Note",
        "warnings": [{"phrase": " Цена ", "warning": " Проверить "}],
    }
    with patch("urllib.request.urlopen", return_value=Response(raw)):
        result = create_llm_provider("openai", content_factory_config=CONFIG).check_text(
            source_text="Исходник"
        )
    assert isinstance(result, TextCheckResult)
    assert result.rewritten_text == "Лучше" and result.ai_note == "Note"
    assert result.warnings == (TextSafetyFinding(phrase="Цена", warning="Проверить"),)


def test_analyze_source_is_normalized() -> None:
    raw = {
        "ok": True,
        "analysis": {
            "summary": " Итог ", "key_facts": [" Факт "], "disputed_claims": [],
            "audience_value": " Польза ", "target_audiences": [],
            "content_angles": [], "recommended_formats": [], "warnings": [],
        },
    }
    config = ContentFactoryConfig(
        "http://factory/internal/generate", "secret-token", 7.5,
        "http://factory/internal/analyze-source",
    )
    with patch("urllib.request.urlopen", return_value=Response(raw)):
        payload = create_llm_provider(
            "openai", content_factory_config=config
        ).analyze_source(source_text="Новость")
    assert isinstance(payload, SourceAnalysisPayload)
    assert payload.summary == "Итог" and payload.key_facts == ("Факт",)


def test_transport_failure_is_hidden_behind_none() -> None:
    provider = create_llm_provider("openai", content_factory_config=CONFIG)
    with patch("urllib.request.urlopen", side_effect=TimeoutError("secret-token")):
        assert provider.check_text(source_text="x") is None
        assert provider.analyze_source(source_text="x") is None
        assert provider.generate_draft(
            source_text="x", material_type="market_offer",
            output_format="telegram", mode="ai",
        ) is None
