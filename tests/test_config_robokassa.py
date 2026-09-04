from __future__ import annotations

from decimal import Decimal

from app.config import load_settings

_ROBOKASSA_ENV_VARS = (
    "ROBOKASSA_MERCHANT_LOGIN", "ROBOKASSA_PASSWORD1", "ROBOKASSA_PASSWORD2",
    "ROBOKASSA_IS_TEST", "ORCHESTRAVEL_STANDARD_PRICE_RUB",
    "ORCHESTRAVEL_SUBSCRIPTION_DAYS", "ORCHESTRAVEL_PUBLIC_BASE_URL",
)


def _base_env(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "dummy-token")
    monkeypatch.setenv("ADMIN_TELEGRAM_ID", "586249067")
    for name in _ROBOKASSA_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_robokassa_defaults_to_unconfigured_and_test_mode(monkeypatch):
    """No secret is required to just start the app / run the test suite -
    billing is simply "not configured" (see RoboKassaConfig.is_configured),
    and the one thing that IS defaulted (is_test) defaults to the SAFE
    value: test mode, never live payments by accident."""
    _base_env(monkeypatch)
    settings = load_settings()

    assert settings.robokassa_merchant_login == ""
    assert settings.robokassa_password1 == ""
    assert settings.robokassa_password2 == ""
    assert settings.robokassa_is_test is True
    assert settings.robokassa_standard_price_rub is None
    assert settings.orchestravel_subscription_days == 30
    assert settings.orchestravel_public_base_url == "https://app.orchestravel.ru"


def test_robokassa_is_test_requires_explicit_false_to_go_live(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("ROBOKASSA_IS_TEST", "false")
    settings = load_settings()
    assert settings.robokassa_is_test is False


def test_robokassa_is_test_stays_true_for_garbage_values(monkeypatch):
    """"Не включать реальные платежи автоматически" - anything that isn't
    a clear, explicit "false" keeps test mode on."""
    _base_env(monkeypatch)
    for garbage in ("true", "TRUE", "1", "yes", "banana", " "):
        monkeypatch.setenv("ROBOKASSA_IS_TEST", garbage)
        assert load_settings().robokassa_is_test is True


def test_robokassa_price_parses_a_valid_decimal(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("ORCHESTRAVEL_STANDARD_PRICE_RUB", "999.00")
    settings = load_settings()
    assert settings.robokassa_standard_price_rub == Decimal("999.00")


def test_robokassa_price_invalid_or_non_positive_falls_back_to_none(monkeypatch):
    """Never invents a price - an invalid/zero/negative value must leave
    billing unconfigured, not silently default to some number."""
    _base_env(monkeypatch)
    for bad in ("not-a-number", "0", "-10", ""):
        monkeypatch.setenv("ORCHESTRAVEL_STANDARD_PRICE_RUB", bad)
        assert load_settings().robokassa_standard_price_rub is None


def test_robokassa_full_config_round_trips(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("ROBOKASSA_MERCHANT_LOGIN", "orchestravel-test")
    monkeypatch.setenv("ROBOKASSA_PASSWORD1", "pw1-test-only")
    monkeypatch.setenv("ROBOKASSA_PASSWORD2", "pw2-test-only")
    monkeypatch.setenv("ROBOKASSA_IS_TEST", "true")
    monkeypatch.setenv("ORCHESTRAVEL_STANDARD_PRICE_RUB", "1490")
    monkeypatch.setenv("ORCHESTRAVEL_SUBSCRIPTION_DAYS", "30")

    settings = load_settings()

    assert settings.robokassa_merchant_login == "orchestravel-test"
    assert settings.robokassa_password1 == "pw1-test-only"
    assert settings.robokassa_password2 == "pw2-test-only"
    assert settings.robokassa_is_test is True
    assert settings.robokassa_standard_price_rub == Decimal("1490")
    assert settings.orchestravel_subscription_days == 30


def test_subscription_days_invalid_falls_back_to_default(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("ORCHESTRAVEL_SUBSCRIPTION_DAYS", "not-a-number")
    assert load_settings().orchestravel_subscription_days == 30


def test_public_base_url_is_overridable(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("ORCHESTRAVEL_PUBLIC_BASE_URL", "http://localhost:8000")
    assert load_settings().orchestravel_public_base_url == "http://localhost:8000"
