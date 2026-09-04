"""RoboKassa integration primitives - signature building/verification,
payment URL construction, and the pure subscription-extension rule. No
network calls of any kind (RoboKassa is a redirect + server-to-server
callback flow, not an API this process calls out to).

Official signature scheme (https://docs.robokassa.ru):
- Payment creation: SignatureValue = MD5(MerchantLogin:OutSum:InvId:Password1)
- ResultURL (server-to-server): SignatureValue = MD5(OutSum:InvId:Password2)

Password1/Password2 are read from ENV only (see app.config.Settings) and
never stored, logged, or returned to any client - see RoboKassaConfig
below and app.services.billing_service for the only two call sites that
touch them (build a signature in, verify a signature out).
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlencode

ROBOKASSA_PAYMENT_URL = "https://auth.robokassa.ru/Merchant/Index.aspx"

# Only plan this product currently sells - see app/domain/subscription.py's
# SubscriptionPlan. Kept as a constant here (not user input) so nothing
# downstream ever needs to trust a client-supplied plan name.
STANDARD_PLAN = "standard"
CURRENCY_RUB = "RUB"


@dataclass(frozen=True)
class RoboKassaConfig:
    """Server-side-only billing configuration - see app.config.Settings
    for where these come from (ROBOKASSA_* / ORCHESTRAVEL_* env vars).
    Password1/Password2 live only on this object and the two functions
    below that consume them; nothing else in the app ever needs them."""
    merchant_login: str
    password1: str
    password2: str
    is_test: bool
    standard_price_rub: Decimal | None
    subscription_days: int
    public_base_url: str

    @property
    def is_configured(self) -> bool:
        return bool(
            self.merchant_login and self.password1 and self.password2
            and self.standard_price_rub is not None and self.standard_price_rub > 0
            and self.subscription_days > 0
        )


def format_amount(amount: Decimal) -> str:
    """Canonical OutSum string - two decimal places, '.' separator, no
    thousands grouping. Used identically when building the payment
    signature/URL and when re-deriving the expected ResultURL signature -
    RoboKassa echoes back the exact OutSum it received, so both sides must
    agree on this format."""
    return f"{amount:.2f}"


def _md5_hex(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def build_payment_signature(
    *, merchant_login: str, out_sum: str, inv_id: int, password1: str,
) -> str:
    return _md5_hex(f"{merchant_login}:{out_sum}:{inv_id}:{password1}")


def build_result_signature(*, out_sum: str, inv_id: int, password2: str) -> str:
    return _md5_hex(f"{out_sum}:{inv_id}:{password2}")


def verify_result_signature(
    *, out_sum: str, inv_id: int, password2: str, signature: str,
) -> bool:
    """Case-insensitive, constant-time comparison - RoboKassa's own MD5
    hex casing is not guaranteed, and this is a security boundary (the
    ONLY thing standing between an attacker and activating a subscription
    for free), so no shortcuts: hmac.compare_digest, not `==`."""
    expected = build_result_signature(out_sum=out_sum, inv_id=inv_id, password2=password2)
    return hmac.compare_digest(expected.lower(), (signature or "").strip().lower())


def build_payment_url(
    *, merchant_login: str, out_sum: str, inv_id: int, description: str,
    signature: str, is_test: bool,
) -> str:
    params = {
        "MerchantLogin": merchant_login,
        "OutSum": out_sum,
        "InvId": str(inv_id),
        "Description": description,
        "SignatureValue": signature,
    }
    if is_test:
        params["IsTest"] = "1"
    return f"{ROBOKASSA_PAYMENT_URL}?{urlencode(params)}"


def _parse_iso(raw: str) -> datetime | None:
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def compute_extended_paid_until(
    *, current_paid_until: str | None, subscription_days: int, now: datetime,
) -> str:
    """Renewal rule: if the existing paid_until is still in the future, the
    new period is ADDED to it (a renewal before expiry never shortens what
    was already paid for); otherwise (expired, malformed, or no paid
    period at all) the new period is counted from `now`. Pure function -
    no IO, easy to test exhaustively; app.services.billing_service is the
    only IO-touching caller.
    """
    base = now
    if current_paid_until:
        parsed = _parse_iso(current_paid_until)
        if parsed is not None and parsed > now:
            base = parsed
    return (base + timedelta(days=subscription_days)).isoformat()
