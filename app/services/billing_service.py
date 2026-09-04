"""Orchestrates payment creation and RoboKassa's ResultURL callback -
the only place that ties together PaymentOrderRepository (audit/
idempotency ledger, app/domain/billing.py) and SubscriptionRepository
(the single access-state source of truth, app/domain/subscription.py).
Neither repository is changed in shape by this module - this is glue, not
a rewrite of either layer.

Web (app/web_api.py) is the only caller. Telegram never creates or
processes payments directly - see app/handlers/lobby.py for the
(link-only, no new auth) pointer back to the web billing page.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from app.domain.billing import PaymentOrder
from app.domain.subscription import SubscriptionPlan
from app.repositories.payment_order_repository import PaymentOrderRepository
from app.repositories.subscription_repository import SubscriptionRepository
from app.services.robokassa import (
    CURRENCY_RUB,
    STANDARD_PLAN,
    RoboKassaConfig,
    build_payment_signature,
    build_payment_url,
    compute_extended_paid_until,
    format_amount,
    verify_result_signature,
)

log = logging.getLogger(__name__)


class BillingNotConfigured(RuntimeError):
    """RoboKassa env vars are incomplete - see RoboKassaConfig.is_configured.
    Deliberately not a crash at import/startup time (test/dev environments
    routinely run without real payment secrets - see the task notes), only
    raised when someone actually tries to create a payment."""


@dataclass(frozen=True)
class PaymentCreationResult:
    order: PaymentOrder
    payment_url: str
    is_test: bool


@dataclass(frozen=True)
class ResultOutcome:
    ok: bool
    inv_id: int
    reason: str = ""


class BillingService:
    def __init__(
        self,
        *,
        config: RoboKassaConfig,
        payment_order_repository: PaymentOrderRepository,
        subscription_repository: SubscriptionRepository,
    ) -> None:
        self._config = config
        self._orders = payment_order_repository
        self._subscriptions = subscription_repository

    async def create_payment(self, workspace_id: int) -> PaymentCreationResult:
        """workspace_id must already be resolved server-side from the
        caller's authenticated session (see app.web_api.create_payment_endpoint) -
        this method never accepts or trusts anything else about who's
        paying or how much; amount/plan/description are entirely
        server-side (RoboKassaConfig), never client input."""
        if not self._config.is_configured:
            raise BillingNotConfigured("RoboKassa не настроен (ENV не заполнены).")

        amount = self._config.standard_price_rub
        order = await self._orders.create_order(
            workspace_id=workspace_id, plan=STANDARD_PLAN,
            amount=format_amount(amount), currency=CURRENCY_RUB,
            provider="robokassa",
        )
        out_sum = format_amount(amount)
        signature = build_payment_signature(
            merchant_login=self._config.merchant_login, out_sum=out_sum,
            inv_id=order.id, password1=self._config.password1,
        )
        payment_url = build_payment_url(
            merchant_login=self._config.merchant_login, out_sum=out_sum,
            inv_id=order.id,
            description=f"ORCHESTRAVEL, тариф standard (workspace {workspace_id})",
            signature=signature, is_test=self._config.is_test,
        )
        return PaymentCreationResult(
            order=order, payment_url=payment_url, is_test=self._config.is_test,
        )

    async def process_result_callback(
        self, *, out_sum: str, inv_id: int, signature: str,
    ) -> ResultOutcome:
        """RoboKassa's ResultURL handler - see app.web_api's
        POST /api/billing/robokassa/result. Never trusts anything from the
        request except as a lookup key, cross-checked against what THIS
        service already committed to when create_payment() ran:
        - InvId -> looks up the order WE created (never accepts a
          client-invented one).
        - SignatureValue -> verified against Password2 (never accepted at
          face value).
        - OutSum -> compared numerically against the order's own stored
          amount (never trusted as "however much was paid").
        Idempotent by construction: a replayed notification for an
        already-'paid' order reaches PaymentOrderRepository.mark_paid(),
        which returns transitioned_now=False, so the subscription is never
        extended twice for one payment.
        """
        if not self._config.is_configured:
            log.warning("billing: result callback received while not configured")
            return ResultOutcome(ok=False, inv_id=inv_id, reason="not_configured")

        if not verify_result_signature(
            out_sum=out_sum, inv_id=inv_id, password2=self._config.password2,
            signature=signature,
        ):
            log.warning("billing: result callback rejected - bad signature (InvId=%s)", inv_id)
            return ResultOutcome(ok=False, inv_id=inv_id, reason="bad_signature")

        order = await self._orders.get_order(inv_id)
        if order is None:
            log.warning("billing: result callback rejected - unknown InvId=%s", inv_id)
            return ResultOutcome(ok=False, inv_id=inv_id, reason="unknown_order")

        try:
            received_amount = Decimal(out_sum)
        except (InvalidOperation, ValueError):
            log.warning("billing: result callback rejected - unparseable OutSum (InvId=%s)", inv_id)
            return ResultOutcome(ok=False, inv_id=inv_id, reason="bad_amount")

        try:
            expected_amount = Decimal(order.amount)
        except (InvalidOperation, ValueError):
            log.error("billing: order %s has an unparseable stored amount", inv_id)
            return ResultOutcome(ok=False, inv_id=inv_id, reason="bad_amount")

        if received_amount != expected_amount:
            log.warning(
                "billing: result callback rejected - OutSum mismatch (InvId=%s)", inv_id,
            )
            return ResultOutcome(ok=False, inv_id=inv_id, reason="amount_mismatch")

        updated_order, transitioned_now = await self._orders.mark_paid(inv_id)
        if updated_order is None:
            # Vanished between get_order() and mark_paid() - can't happen
            # under normal operation, fail closed rather than activate
            # anything.
            log.error("billing: order %s vanished mid-callback", inv_id)
            return ResultOutcome(ok=False, inv_id=inv_id, reason="order_vanished")

        if transitioned_now:
            await self._extend_subscription(order)
        else:
            log.info("billing: replayed ResultURL for already-paid InvId=%s - no-op", inv_id)

        return ResultOutcome(ok=True, inv_id=inv_id)

    async def _extend_subscription(self, order: PaymentOrder) -> None:
        existing = await self._subscriptions.get_for_workspace(order.workspace_id)
        now = datetime.now(timezone.utc)
        new_paid_until = compute_extended_paid_until(
            current_paid_until=existing.paid_until if existing is not None else None,
            subscription_days=self._config.subscription_days,
            now=now,
        )
        await self._subscriptions.mark_paid(
            order.workspace_id,
            external_payment_id=str(order.id),
            payment_provider="robokassa",
            paid_until=new_paid_until,
            plan=SubscriptionPlan.STANDARD,
        )
