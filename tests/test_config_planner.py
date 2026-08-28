from __future__ import annotations

from app.config import load_settings


def _base_env(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "dummy-token")
    monkeypatch.setenv("ADMIN_TELEGRAM_ID", "586249067")


def test_planner_defaults_are_fully_off(monkeypatch):
    _base_env(monkeypatch)
    settings = load_settings()
    assert settings.planner_enabled is False
    assert settings.planner_llm_provider == "null"
    assert settings.planner_allowed_telegram_user_ids == frozenset()
    assert settings.planner_max_llm_calls == 4


def test_planner_max_llm_calls_override(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("PLANNER_MAX_LLM_CALLS", "2")
    settings = load_settings()
    assert settings.planner_max_llm_calls == 2


def test_planner_max_llm_calls_invalid_falls_back_to_default(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("PLANNER_MAX_LLM_CALLS", "not-a-number")
    settings = load_settings()
    assert settings.planner_max_llm_calls == 4


def test_planner_max_llm_calls_out_of_range_falls_back_to_default(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("PLANNER_MAX_LLM_CALLS", "999")
    settings = load_settings()
    assert settings.planner_max_llm_calls == 4


def test_planner_enabled_flag_is_parsed(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("PLANNER_ENABLED", "true")
    settings = load_settings()
    assert settings.planner_enabled is True


def test_planner_openai_api_key_falls_back_to_orchestration_key(monkeypatch):
    """Cost/secret-sprawl control: do not force a second identical secret if
    the operator only wants to reuse the same OpenAI account."""
    _base_env(monkeypatch)
    monkeypatch.setenv("ORCHESTRATION_OPENAI_API_KEY", "shared-key")
    settings = load_settings()
    assert settings.planner_openai_api_key == "shared-key"


def test_planner_openai_api_key_explicit_value_wins(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("ORCHESTRATION_OPENAI_API_KEY", "shared-key")
    monkeypatch.setenv("PLANNER_OPENAI_API_KEY", "planner-only-key")
    settings = load_settings()
    assert settings.planner_openai_api_key == "planner-only-key"


def test_planner_openai_model_default(monkeypatch):
    _base_env(monkeypatch)
    settings = load_settings()
    assert settings.planner_openai_model == "gpt-4o-mini"


def test_planner_openai_timeout_default_and_override(monkeypatch):
    _base_env(monkeypatch)
    settings = load_settings()
    assert settings.planner_openai_timeout_seconds == 20.0

    monkeypatch.setenv("PLANNER_OPENAI_TIMEOUT_SECONDS", "5")
    settings = load_settings()
    assert settings.planner_openai_timeout_seconds == 5.0


def test_planner_openai_timeout_invalid_value_falls_back_to_default(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("PLANNER_OPENAI_TIMEOUT_SECONDS", "not-a-number")
    settings = load_settings()
    assert settings.planner_openai_timeout_seconds == 20.0


def test_planner_allowed_telegram_user_ids_parses_comma_separated_ids(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("PLANNER_ALLOWED_TELEGRAM_USER_IDS", "586249067, 111222333")
    settings = load_settings()
    assert settings.planner_allowed_telegram_user_ids == frozenset({586249067, 111222333})


def test_planner_allowed_telegram_user_ids_empty_is_fail_closed(monkeypatch):
    """Critical staged-rollout property, at the config layer too: unset
    means nobody, not everybody."""
    _base_env(monkeypatch)
    settings = load_settings()
    assert settings.planner_allowed_telegram_user_ids == frozenset()


def test_planner_llm_provider_openai_is_accepted(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("PLANNER_LLM_PROVIDER", "openai")
    settings = load_settings()
    assert settings.planner_llm_provider == "openai"


def test_unknown_planner_llm_provider_fails_fast_when_the_provider_is_built(monkeypatch):
    """load_settings() only normalizes the name (same as
    ORCHESTRATION_LLM_PROVIDER) - actual validation happens when the
    provider is built at startup (see app.main), mirrored here directly via
    the factory to prove an unknown name is rejected, not silently accepted."""
    import pytest

    from app.planner.factory import UnknownPlannerLLMProviderError, create_planner_llm_provider

    _base_env(monkeypatch)
    monkeypatch.setenv("PLANNER_LLM_PROVIDER", "yandex")
    settings = load_settings()
    assert settings.planner_llm_provider == "yandex"
    with pytest.raises(UnknownPlannerLLMProviderError):
        create_planner_llm_provider(settings.planner_llm_provider)
