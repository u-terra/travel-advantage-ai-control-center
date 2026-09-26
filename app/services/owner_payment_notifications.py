"""Telegram notification to ORCHESTRAVEL's owner/admin after a real,
confirmed successful payment.

Reuses the SAME bot (settings.bot_token) and the SAME admin identity
(settings.admin_telegram_id, app.config.Settings - already required at
startup and already used to bootstrap the owner workspace, see
app.main._async_main) this project already has - no second bot, no second
admin-id concept, no polling loop. Sending is a single one-off Bot API call
(aiogram.Bot.send_message), safe to make from the Web process
(app/web_api.py, which has no long-lived Bot/Dispatcher of its own).

Fire-and-forget by design: BillingService.process_result_callback (the only
caller) has already verified the RoboKassa signature, confirmed the amount,
and atomically transitioned the order from 'created' to 'paid' and extended
the subscription BEFORE this is ever invoked - a Telegram failure here must
never undo any of that, never turn the ResultURL response into anything but
200 "OKxxx", and never block/delay subscription activation. See notify()'s
own contract: it always returns a bool, never raises.
"""

from __future__ import annotations

import logging

from aiogram import Bot

from app.domain.billing import PaymentOrder
from app.services.plan_limits import plan_display_name

log = logging.getLogger(__name__)

_FULL_PLAN_CODE = "full"
_FULL_ONBOARDING_NOTE = "Нужна первоначальная персональная настройка клиента."


def build_owner_payment_notification_text(
    order: PaymentOrder, *, business_name: str | None, email: str | None,
) -> str:
    lines = [
        "Новая оплата ORCHESTRAVEL",
        "",
        f"Тариф: {plan_display_name(order.plan)}",
        f"Сумма: {order.amount} ₽",
        f"Workspace: {order.workspace_id}",
    ]
    if business_name:
        lines.append(f"Бизнес: {business_name}")
    if email:
        lines.append(f"Email: {email}")
    lines.append(f"Период: {order.duration_days} дней")
    lines.append(f"Order ID: {order.id}")
    if order.plan == _FULL_PLAN_CODE:
        lines.append("")
        lines.append(_FULL_ONBOARDING_NOTE)
    return "\n".join(lines)


class OwnerPaymentNotifier:
    def __init__(self, *, bot_token: str, admin_telegram_id: int) -> None:
        self._bot_token = bot_token
        self._admin_telegram_id = admin_telegram_id

    async def notify(
        self, order: PaymentOrder, *, business_name: str | None, email: str | None,
    ) -> bool:
        """True only when the Telegram API call actually succeeded - the
        caller (BillingService) uses this, and only this, to decide whether
        to persist owner_notified_at. Never raises - payment/subscription
        activation must never depend on Telegram being reachable."""
        text = build_owner_payment_notification_text(
            order, business_name=business_name, email=email,
        )
        try:
            # Bot(...) itself can raise (e.g. TokenValidationError for a
            # malformed BOT_TOKEN) - kept inside the try so a bad token is
            # exactly as non-fatal as a failed send, never an unhandled
            # exception out of notify().
            bot = Bot(self._bot_token)
            try:
                await bot.send_message(self._admin_telegram_id, text)
            finally:
                await bot.session.close()
        except Exception:
            log.warning(
                "owner_payment_notifications: failed to notify owner for order %s",
                order.id, exc_info=True,
            )
            return False
        return True
