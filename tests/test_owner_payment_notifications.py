"""app.services.owner_payment_notifications - message content and
send-failure isolation. OwnerPaymentNotifier.notify() is exercised only
with a deliberately invalid token ("dummy-token") - aiogram validates the
token format synchronously at Bot() construction (confirmed: raises
TokenValidationError before any network I/O), so these tests can assert
the real notify() failure path without ever making a real network call or
risking a real Telegram send.
"""

from __future__ import annotations

import asyncio

from app.domain.billing import PaymentOrder, PaymentOrderStatus
from app.services.owner_payment_notifications import (
    OwnerPaymentNotifier,
    build_owner_payment_notification_text,
)


def run(coro):
    return asyncio.run(coro)


def _order(*, plan: str, amount: str, duration_days: int, order_id: int = 123, workspace_id: int = 7) -> PaymentOrder:
    return PaymentOrder(
        id=order_id, workspace_id=workspace_id, plan=plan, amount=amount,
        currency="RUB", provider="robokassa", status=PaymentOrderStatus.PAID,
        created_at="2026-01-01T00:00:00+00:00", paid_at="2026-01-01T00:05:00+00:00",
        duration_days=duration_days,
    )


# ── message content ──────────────────────────────────────────────────────


def test_start_notification_text() -> None:
    order = _order(plan="start", amount="490.00", duration_days=14)
    text = build_owner_payment_notification_text(order, business_name=None, email=None)

    assert "Новая оплата ORCHESTRAVEL" in text
    assert "Тариф: START" in text
    assert "Сумма: 490.00 ₽" in text
    assert "Workspace: 7" in text
    assert "Период: 14 дней" in text
    assert "Order ID: 123" in text
    assert "Бизнес:" not in text
    assert "Email:" not in text
    assert "персональная настройка" not in text


def test_standard_notification_text() -> None:
    order = _order(plan="standard", amount="990.00", duration_days=30)
    text = build_owner_payment_notification_text(order, business_name=None, email=None)

    assert "Тариф: STANDARD" in text
    assert "Сумма: 990.00 ₽" in text
    assert "Период: 30 дней" in text
    assert "персональная настройка" not in text


def test_full_notification_text_includes_onboarding_note() -> None:
    order = _order(plan="full", amount="1490.00", duration_days=30)
    text = build_owner_payment_notification_text(order, business_name=None, email=None)

    assert "Тариф: FULL" in text
    assert "Сумма: 1490.00 ₽" in text
    assert "Нужна первоначальная персональная настройка клиента." in text


def test_non_full_plans_never_include_onboarding_note() -> None:
    for plan, amount, days in (("start", "490.00", 14), ("standard", "990.00", 30)):
        text = build_owner_payment_notification_text(
            _order(plan=plan, amount=amount, duration_days=days),
            business_name=None, email=None,
        )
        assert "первоначальная персональная настройка" not in text


def test_notification_includes_business_name_and_email_when_provided() -> None:
    order = _order(plan="standard", amount="990.00", duration_days=30)
    text = build_owner_payment_notification_text(
        order, business_name="Тревел Клуб", email="owner@example.com",
    )

    assert "Бизнес: Тревел Клуб" in text
    assert "Email: owner@example.com" in text


def test_notification_omits_business_name_and_email_when_absent() -> None:
    order = _order(plan="standard", amount="990.00", duration_days=30)
    text = build_owner_payment_notification_text(order, business_name=None, email=None)

    assert "Бизнес:" not in text
    assert "Email:" not in text


def test_example_message_matches_the_product_spec_shape() -> None:
    """The exact FULL example from the product spec, field by field."""
    order = _order(
        plan="full", amount="1490.00", duration_days=30, order_id=123, workspace_id=7,
    )
    text = build_owner_payment_notification_text(
        order, business_name="Тревел Клуб", email="owner@example.com",
    )
    lines = text.splitlines()

    assert lines[0] == "Новая оплата ORCHESTRAVEL"
    assert "Тариф: FULL" in lines
    assert "Сумма: 1490.00 ₽" in lines
    assert "Workspace: 7" in lines
    assert "Бизнес: Тревел Клуб" in lines
    assert "Email: owner@example.com" in lines
    assert "Период: 30 дней" in lines
    assert "Order ID: 123" in lines
    assert "Нужна первоначальная персональная настройка клиента." in lines


# ── notify(): never raises, no network on an invalid token ────────────────


def test_notify_with_invalid_token_returns_false_without_raising() -> None:
    """Confirms the send-failure isolation contract - a Telegram/config
    failure (here: aiogram's own synchronous token-format validation)
    must never propagate out of notify()."""
    order = _order(plan="standard", amount="990.00", duration_days=30)
    notifier = OwnerPaymentNotifier(bot_token="dummy-token", admin_telegram_id=1)

    result = run(notifier.notify(order, business_name=None, email=None))

    assert result is False
