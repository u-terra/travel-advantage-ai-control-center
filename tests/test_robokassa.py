from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.services.robokassa import (
    RoboKassaConfig,
    build_payment_signature,
    build_payment_url,
    build_result_signature,
    compute_extended_paid_until,
    format_amount,
    verify_result_signature,
)


# ── format_amount ────────────────────────────────────────────────────────

def test_format_amount_uses_two_decimals_and_dot_separator():
    assert format_amount(Decimal("999")) == "999.00"
    assert format_amount(Decimal("1490.5")) == "1490.50"


# ── payment creation signature (Password1) ──────────────────────────────

def test_build_payment_signature_matches_official_formula():
    # MD5("shop:999.00:42:pw1") - independently computable, pins the exact
    # RoboKassa formula (MerchantLogin:OutSum:InvId:Password1).
    signature = build_payment_signature(
        merchant_login="shop", out_sum="999.00", inv_id=42, password1="pw1",
    )
    import hashlib
    expected = hashlib.md5(b"shop:999.00:42:pw1").hexdigest()
    assert signature == expected


def test_payment_signature_changes_with_any_input():
    base = build_payment_signature(
        merchant_login="shop", out_sum="999.00", inv_id=42, password1="pw1",
    )
    assert base != build_payment_signature(
        merchant_login="shop", out_sum="999.01", inv_id=42, password1="pw1",
    )
    assert base != build_payment_signature(
        merchant_login="shop", out_sum="999.00", inv_id=43, password1="pw1",
    )
    assert base != build_payment_signature(
        merchant_login="shop", out_sum="999.00", inv_id=42, password1="pw2",
    )


# ── payment URL ──────────────────────────────────────────────────────────

def test_build_payment_url_includes_is_test_when_test_mode():
    url = build_payment_url(
        merchant_login="shop", out_sum="999.00", inv_id=42,
        description="ORCHESTRAVEL", signature="abc123", is_test=True,
    )
    assert url.startswith("https://auth.robokassa.ru/Merchant/Index.aspx?")
    assert "IsTest=1" in url
    assert "MerchantLogin=shop" in url
    assert "InvId=42" in url
    assert "SignatureValue=abc123" in url


def test_build_payment_url_omits_is_test_in_live_mode():
    url = build_payment_url(
        merchant_login="shop", out_sum="999.00", inv_id=42,
        description="ORCHESTRAVEL", signature="abc123", is_test=False,
    )
    assert "IsTest" not in url


# ── ResultURL signature (Password2) - the real security boundary ────────

def test_verify_result_signature_accepts_a_correctly_signed_callback():
    signature = build_result_signature(out_sum="999.00", inv_id=42, password2="pw2")
    assert verify_result_signature(
        out_sum="999.00", inv_id=42, password2="pw2", signature=signature,
    ) is True


def test_verify_result_signature_is_case_insensitive():
    signature = build_result_signature(out_sum="999.00", inv_id=42, password2="pw2")
    assert verify_result_signature(
        out_sum="999.00", inv_id=42, password2="pw2", signature=signature.upper(),
    ) is True


def test_verify_result_signature_rejects_wrong_password():
    signature = build_result_signature(out_sum="999.00", inv_id=42, password2="pw2")
    assert verify_result_signature(
        out_sum="999.00", inv_id=42, password2="WRONG-PASSWORD", signature=signature,
    ) is False


def test_verify_result_signature_rejects_tampered_out_sum():
    signature = build_result_signature(out_sum="999.00", inv_id=42, password2="pw2")
    assert verify_result_signature(
        out_sum="1.00", inv_id=42, password2="pw2", signature=signature,
    ) is False


def test_verify_result_signature_rejects_tampered_inv_id():
    signature = build_result_signature(out_sum="999.00", inv_id=42, password2="pw2")
    assert verify_result_signature(
        out_sum="999.00", inv_id=99, password2="pw2", signature=signature,
    ) is False


def test_verify_result_signature_rejects_empty_or_garbage_signature():
    assert verify_result_signature(
        out_sum="999.00", inv_id=42, password2="pw2", signature="",
    ) is False
    assert verify_result_signature(
        out_sum="999.00", inv_id=42, password2="pw2", signature="not-a-real-hash",
    ) is False


# ── RoboKassaConfig.is_configured ────────────────────────────────────────

def _config(**overrides) -> RoboKassaConfig:
    defaults = dict(
        merchant_login="shop", password1="pw1", password2="pw2", is_test=True,
        standard_price_rub=Decimal("999.00"), subscription_days=30,
        public_base_url="https://app.orchestravel.ru",
    )
    defaults.update(overrides)
    return RoboKassaConfig(**defaults)


def test_config_is_configured_when_everything_is_present():
    assert _config().is_configured is True


def test_config_is_not_configured_when_any_secret_is_missing():
    assert _config(merchant_login="").is_configured is False
    assert _config(password1="").is_configured is False
    assert _config(password2="").is_configured is False


def test_config_is_not_configured_without_a_price():
    assert _config(standard_price_rub=None).is_configured is False
    assert _config(standard_price_rub=Decimal("0")).is_configured is False


def test_config_is_not_configured_without_subscription_days():
    assert _config(subscription_days=0).is_configured is False


# ── compute_extended_paid_until: the renewal rule ────────────────────────

def test_extension_from_no_existing_period_counts_from_now():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = compute_extended_paid_until(
        current_paid_until=None, subscription_days=30, now=now,
    )
    assert result == (now + timedelta(days=30)).isoformat()


def test_extension_from_an_expired_period_counts_from_now_not_the_old_date():
    now = datetime(2026, 3, 1, tzinfo=timezone.utc)
    expired = "2026-01-01T00:00:00+00:00"
    result = compute_extended_paid_until(
        current_paid_until=expired, subscription_days=30, now=now,
    )
    assert result == (now + timedelta(days=30)).isoformat()


def test_extension_from_a_future_period_adds_to_it_not_to_now():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    future = (now + timedelta(days=10)).isoformat()
    result = compute_extended_paid_until(
        current_paid_until=future, subscription_days=30, now=now,
    )
    expected = now + timedelta(days=10) + timedelta(days=30)
    assert result == expected.isoformat()


def test_extension_from_a_malformed_date_counts_from_now():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = compute_extended_paid_until(
        current_paid_until="not-a-date", subscription_days=30, now=now,
    )
    assert result == (now + timedelta(days=30)).isoformat()


def test_extension_boundary_exactly_now_counts_from_now():
    """paid_until == now (not strictly in the future) is treated as already
    expired - the boundary belongs to "count from now", matching
    compute_access_state's own >= comparison for expiry."""
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = compute_extended_paid_until(
        current_paid_until=now.isoformat(), subscription_days=30, now=now,
    )
    assert result == (now + timedelta(days=30)).isoformat()
