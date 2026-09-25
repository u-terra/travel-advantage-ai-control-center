"""settings.orchestravel_bot_username - see app.web_api's POST
/api/telegram/bind-token and GET /demo, both of which read this field
off Settings. Covers normalization (leading "@", surrounding whitespace)
and the fallback to the real production bot when TELEGRAM_BOT_USERNAME
is unset.
"""

from __future__ import annotations

from app.config import load_settings


def _base_env(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "dummy-token")
    monkeypatch.setenv("ADMIN_TELEGRAM_ID", "586249067")
    monkeypatch.delenv("TELEGRAM_BOT_USERNAME", raising=False)


def test_settings_has_orchestravel_bot_username_field(monkeypatch):
    _base_env(monkeypatch)
    settings = load_settings()
    assert hasattr(settings, "orchestravel_bot_username")
    assert isinstance(settings.orchestravel_bot_username, str)


def test_bot_username_defaults_to_the_previously_hardcoded_production_bot(monkeypatch):
    """The username that was hardcoded in demo.html's hero/footer links
    before commit a12c255 - must stay the default so an unset
    TELEGRAM_BOT_USERNAME never silently points at a different bot."""
    _base_env(monkeypatch)
    settings = load_settings()
    assert settings.orchestravel_bot_username == "ta_control_center_vassian_bot"


def test_bot_username_env_override_works(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "some_other_bot")
    settings = load_settings()
    assert settings.orchestravel_bot_username == "some_other_bot"


def test_bot_username_strips_leading_at_and_whitespace(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "  @some_other_bot  ")
    settings = load_settings()
    assert settings.orchestravel_bot_username == "some_other_bot"


def test_bot_username_empty_env_falls_back_to_default(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "   ")
    settings = load_settings()
    assert settings.orchestravel_bot_username == "ta_control_center_vassian_bot"
