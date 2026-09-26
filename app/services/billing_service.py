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
from app.domain.subscription import SubscriptionPlan, SubscriptionStatus
from app.repositories.partner_repository import PartnerRepository
from app.repositories.payment_order_repository import PaymentOrderRepository
from app.repositories.source_catalog_repository import SourceCatalogRepository
from app.repositories.subscription_repository import SubscriptionRepository
from app.repositories.web_auth_repository import WebAuthRepository
from app.services.owner_payment_notifications import OwnerPaymentNotifier
from app.services.plans import DEFAULT_PLAN_CODE, get_plan
from app.services.robokassa import (
    CURRENCY_RUB,
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


class UnknownPlanError(RuntimeError):
    """plan_code isn't a key in app.services.plans.PLAN_CATALOG - a client
    can pick WHICH plan to buy, never invent a new one or its price."""


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
        source_catalog_repository: SourceCatalogRepository | None = None,
        owner_notifier: OwnerPaymentNotifier | None = None,
        partner_repository: PartnerRepository | None = None,
        web_auth_repository: WebAuthRepository | None = None,
    ) -> None:
        self._config = config
        self._orders = payment_order_repository
        self._subscriptions = subscription_repository
        # Optional/default-None (ORCHESTRAVEL default source pack, stage 1)
        # - every existing caller/test that doesn't pass this is completely
        # unaffected: no source assignment is attempted, same as before
        # this feature existed. See _extend_subscription().
        self._source_catalog = source_catalog_repository
        # Optional/default-None, same convention as source_catalog_repository
        # above - owner payment notification (see
        # app.services.owner_payment_notifications). Every existing caller/
        # test that doesn't pass these is unaffected: no notification is
        # ever attempted, same as before this feature existed. See
        # _notify_owner_of_payment().
        self._owner_notifier = owner_notifier
        self._partner_repository = partner_repository
        self._web_auth_repository = web_auth_repository

    async def create_payment(
        self, workspace_id: int, plan_code: str = DEFAULT_PLAN_CODE,
    ) -> PaymentCreationResult:
        """workspace_id must already be resolved server-side from the
        caller's authenticated session (see app.web_api.create_payment_endpoint) -
        this method never accepts or trusts anything else about who's
        paying. plan_code selects WHICH tariff from PLAN_CATALOG - amount
        and duration_days are looked up there, never accepted as separate
        parameters, so a caller can pick a plan by name but can never
        smuggle in its own amount."""
        if not self._config.is_configured:
            raise BillingNotConfigured("RoboKassa не настроен (ENV не заполнены).")

        plan = get_plan(plan_code)
        if plan is None:
            raise UnknownPlanError(f"Неизвестный тариф: {plan_code!r}")

        order = await self._orders.create_order(
            workspace_id=workspace_id, plan=plan.code,
            amount=format_amount(plan.amount), currency=CURRENCY_RUB,
            provider="robokassa", duration_days=plan.duration_days,
        )
        out_sum = format_amount(plan.amount)
        signature = build_payment_signature(
            merchant_login=self._config.merchant_login, out_sum=out_sum,
            inv_id=order.id, password1=self._config.password1,
        )
        payment_url = build_payment_url(
            merchant_login=self._config.merchant_login, out_sum=out_sum,
            inv_id=order.id,
            description=f"ORCHESTRAVEL, тариф {plan.label} (workspace {workspace_id})",
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
            # Best-effort, strictly after activation succeeded - a Telegram
            # failure here must never affect the ResultOutcome returned
            # below (see _notify_owner_of_payment's own docstring). Gated on
            # transitioned_now, exactly like _extend_subscription() above:
            # mark_paid()'s atomic created->paid UPDATE already guarantees
            # this branch runs at most once per order, so a replayed
            # RoboKassa notification for an already-paid order can never
            # reach this call - the anti-duplicate guarantee is the same
            # persisted DB transition, not anything kept in process memory.
            await self._notify_owner_of_payment(order)
        else:
            log.info("billing: replayed ResultURL for already-paid InvId=%s - no-op", inv_id)

        return ResultOutcome(ok=True, inv_id=inv_id)

    async def _extend_subscription(self, order: PaymentOrder) -> None:
        """Activates exactly the plan/duration the order itself was
        created with (see create_payment) - never the global
        RoboKassaConfig.subscription_days/STANDARD default, so a $START$
        order can never accidentally grant $FULL$-length access or vice
        versa."""
        existing = await self._subscriptions.get_for_workspace(order.workspace_id)
        now = datetime.now(timezone.utc)
        new_paid_until = compute_extended_paid_until(
            current_paid_until=existing.paid_until if existing is not None else None,
            subscription_days=order.duration_days,
            now=now,
        )
        await self._subscriptions.mark_paid(
            order.workspace_id,
            external_payment_id=str(order.id),
            payment_provider="robokassa",
            paid_until=new_paid_until,
            plan=SubscriptionPlan(order.plan),
        )

        # ORCHESTRAVEL default source pack (stage 1) - only for a
        # workspace's FIRST-EVER successful activation: no subscription row
        # yet, or it was still 'pending' (self-service signup's
        # zero-access state before any payment - see
        # PartnerRepository.provision_self_service_workspace /
        # SubscriptionRepository.create_pending). A renewal or
        # reactivation of an existing/legacy workspace - status was
        # already trial/beta/active/past_due/expired/suspended BEFORE this
        # payment - never triggers this: those workspaces keep exactly
        # whatever sources they already have, untouched.
        is_first_activation = (
            existing is None or existing.status is SubscriptionStatus.PENDING
        )
        if self._source_catalog is not None and is_first_activation:
            try:
                await self._source_catalog.assign_default_sources(order.workspace_id)
            except Exception:
                # Best-effort: a source-catalog hiccup must never fail an
                # already-verified, already-recorded payment.
                log.warning(
                    "billing: assign_default_sources failed for workspace %s",
                    order.workspace_id, exc_info=True,
                )

    async def _notify_owner_of_payment(self, order: PaymentOrder) -> None:
        """Best-effort owner notification - payment/subscription activation
        already fully happened by the time this runs (see the one caller,
        process_result_callback). Every step here is individually guarded:
        a lookup failure or a Telegram send failure only ever prevents the
        notification, never propagates and never affects anything else."""
        if self._owner_notifier is None:
            return

        business_name: str | None = None
        if self._partner_repository is not None:
            try:
                profile = await self._partner_repository.get_business_profile(
                    order.workspace_id
                )
                business_name = profile.business_name if profile is not None else None
            except Exception:
                log.warning(
                    "billing: business profile lookup failed for owner "
                    "notification (order %s)", order.id, exc_info=True,
                )

        email: str | None = None
        if self._web_auth_repository is not None:
            try:
                email = await self._web_auth_repository.get_primary_email_for_workspace(
                    order.workspace_id
                )
            except Exception:
                log.warning(
                    "billing: email lookup failed for owner notification "
                    "(order %s)", order.id, exc_info=True,
                )

        try:
            sent = await self._owner_notifier.notify(
                order, business_name=business_name, email=email,
            )
        except Exception:
            # notify() itself never raises (see its docstring) - this is
            # defense in depth only, same policy as assign_default_sources
            # above: nothing about the payment may ever depend on this.
            log.warning(
                "billing: owner payment notification raised unexpectedly "
                "(order %s)", order.id, exc_info=True,
            )
            return

        if sent:
            try:
                await self._orders.mark_owner_notified(order.id)
            except Exception:
                log.warning(
                    "billing: failed to persist owner_notified_at for "
                    "order %s", order.id, exc_info=True,
                )
