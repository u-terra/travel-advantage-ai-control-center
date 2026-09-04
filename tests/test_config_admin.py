from __future__ import annotations

from app.config import load_settings


def _base_env(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "dummy-token")
    monkeypatch.setenv("ADMIN_TELEGRAM_ID", "586249067")
    monkeypatch.delenv("ORCHESTRAVEL_ADMIN_EMAILS", raising=False)


def test_admin_emails_default_to_empty_fail_closed(monkeypatch):
    _base_env(monkeypatch)
    settings = load_settings()
    assert settings.orchestravel_admin_emails == frozenset()


def test_admin_emails_parses_comma_separated_list(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("ORCHESTRAVEL_ADMIN_EMAILS", "a@example.com,b@example.com")
    settings = load_settings()
    assert settings.orchestravel_admin_emails == {"a@example.com", "b@example.com"}


def test_admin_emails_are_normalized_to_lowercase_and_trimmed(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("ORCHESTRAVEL_ADMIN_EMAILS", " Admin@Example.com , Second@Example.com ")
    settings = load_settings()
    assert settings.orchestravel_admin_emails == {"admin@example.com", "second@example.com"}


def test_admin_emails_ignores_empty_entries(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("ORCHESTRAVEL_ADMIN_EMAILS", "a@example.com,,  ,b@example.com")
    settings = load_settings()
    assert settings.orchestravel_admin_emails == {"a@example.com", "b@example.com"}
