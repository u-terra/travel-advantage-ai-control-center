from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from app.domain.subscription import SubscriptionPlan, SubscriptionStatus
from app.repositories.partner_repository import PartnerRepository
from app.repositories.payment_order_repository import PaymentOrderRepository
from app.repositories.subscription_repository import SubscriptionRepository
from app.services.access_state import ACTIVE, is_access_granted
from app.services.billing_service import BillingNotConfigured, BillingService
from app.services.robokassa import RoboKassaConfig, build_result_signature, verify_result_signature


def run(coro):
    return asyncio.run(coro)


def _workspace(db_path: Path, telegram_id: int = 100) -> int:
    partners = PartnerRepository(db_path)
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(telegram_id))
    return workspace.id


def _service(db_path: Path, **config_overrides) -> BillingService:
    defaults = dict(
        merchant_login="orchestravel-test", password1="pw1-test", password2="pw2-test",
        is_test=True, standard_price_rub=Decimal("999.00"), subscription_days=30,
        public_base_url="https://app.orchestravel.ru",
    )
    defaults.update(config_overrides)
    config = RoboKassaConfig(**defaults)
    orders = PaymentOrderRepository(db_path)
    run(orders.init())
    subscriptions = SubscriptionRepository(db_path)
    run(subscriptions.init())
    return BillingService(
        config=config, payment_order_repository=orders,
        subscription_repository=subscriptions,
    ), orders, subscriptions


# ── create_payment ────────────────────────────────────────────────────────

def test_create_payment_builds_a_signed_url_for_the_configured_price(tmp_path: Path):
    """Amount/duration come from app.services.plans.PLAN_CATALOG, not from
    RoboKassaConfig.standard_price_rub/subscription_days anymore (the
    legacy single-tier config fields are still parsed by app.config for
    /api/billing/status's display, but no longer feed create_payment) -
    default plan_code (DEFAULT_PLAN_CODE="standard") prices at 990.00/30d."""
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _workspace(db_path)
    service, orders, _ = _service(db_path)

    result = run(service.create_payment(workspace_id))

    assert result.order.workspace_id == workspace_id
    assert result.order.plan == "standard"
    assert result.order.amount == "990.00"
    assert result.order.duration_days == 30
    assert result.is_test is True

    query = parse_qs(urlsplit(result.payment_url).query)
    assert query["MerchantLogin"][0] == "orchestravel-test"
    assert query["OutSum"][0] == "990.00"
    assert query["InvId"][0] == str(result.order.id)
    assert query["IsTest"][0] == "1"
    assert "SignatureValue" in query
    # the secret itself must never appear anywhere in the URL.
    assert "pw1-test" not in result.payment_url
    assert "pw2-test" not in result.payment_url


def test_create_payment_raises_when_not_configured(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _workspace(db_path)
    service, _, _ = _service(db_path, merchant_login="")

    with pytest.raises(BillingNotConfigured):
        run(service.create_payment(workspace_id))


def test_create_payment_has_no_amount_parameter(tmp_path: Path):
    """create_payment() takes workspace_id and (now) which plan to buy -
    but there is still no amount/price parameter anywhere: a caller can
    pick WHICH catalog entry, never influence what it costs."""
    import inspect
    assert list(inspect.signature(BillingService.create_payment).parameters) == [
        "self", "workspace_id", "plan_code",
    ]


def test_create_payment_rejects_an_unknown_plan_code(tmp_path: Path):
    from app.services.billing_service import UnknownPlanError

    db_path = tmp_path / "db.sqlite3"
    workspace_id = _workspace(db_path)
    service, _, _ = _service(db_path)

    with pytest.raises(UnknownPlanError):
        run(service.create_payment(workspace_id, "made-up-plan"))


def test_create_payment_start_and_full_plans_price_from_the_catalog(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _workspace(db_path)
    service, _, _ = _service(db_path)

    start_result = run(service.create_payment(workspace_id, "start"))
    assert start_result.order.plan == "start"
    assert start_result.order.amount == "490.00"
    assert start_result.order.duration_days == 14

    full_result = run(service.create_payment(workspace_id, "full"))
    assert full_result.order.plan == "full"
    assert full_result.order.amount == "1490.00"
    assert full_result.order.duration_days == 30


# ── process_result_callback: the security-critical path ────────────────

def _signed_callback(order, password2: str) -> dict:
    signature = build_result_signature(
        out_sum=order.amount, inv_id=order.id, password2=password2,
    )
    return {"out_sum": order.amount, "inv_id": order.id, "signature": signature}


def test_valid_callback_activates_the_subscription(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _workspace(db_path)
    service, orders, subscriptions = _service(db_path)
    order = run(orders.create_order(workspace_id=workspace_id, plan="standard", amount="999.00"))

    outcome = run(service.process_result_callback(**_signed_callback(order, "pw2-test")))

    assert outcome.ok is True
    assert outcome.inv_id == order.id

    updated_order = run(orders.get_order(order.id))
    assert updated_order.status.value == "paid"

    subscription = run(subscriptions.get_for_workspace(workspace_id))
    assert subscription.status is SubscriptionStatus.ACTIVE
    assert subscription.plan is SubscriptionPlan.STANDARD
    assert subscription.paid_until is not None

    state = run(subscriptions.resolve_access_state(workspace_id))
    assert state == ACTIVE
    assert is_access_granted(state)


def test_invalid_signature_is_rejected_and_nothing_activates(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _workspace(db_path)
    service, orders, subscriptions = _service(db_path)
    order = run(orders.create_order(workspace_id=workspace_id, plan="standard", amount="999.00"))

    outcome = run(service.process_result_callback(
        out_sum=order.amount, inv_id=order.id, signature="0" * 32,
    ))

    assert outcome.ok is False
    assert outcome.reason == "bad_signature"
    assert run(orders.get_order(order.id)).status.value == "created"
    # Backfill already grandfathered this workspace in as 'beta' (its own,
    # unrelated grant - see SubscriptionRepository.init()) - the real
    # assertion is that the REJECTED payment never touched the row at all:
    # still 'beta', never 'standard', never an external_payment_id.
    subscription = run(subscriptions.get_for_workspace(workspace_id))
    assert subscription.plan is not SubscriptionPlan.STANDARD
    assert subscription.external_payment_id is None


def test_wrong_password2_is_rejected_even_with_correct_out_sum_and_inv_id(tmp_path: Path):
    """The exact scenario a forged ResultURL request would look like: right
    OutSum/InvId, but a signature computed with the wrong (attacker's own
    guessed) Password2."""
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _workspace(db_path)
    service, orders, subscriptions = _service(db_path)
    order = run(orders.create_order(workspace_id=workspace_id, plan="standard", amount="999.00"))
    forged_signature = build_result_signature(
        out_sum=order.amount, inv_id=order.id, password2="attacker-guessed-password",
    )

    outcome = run(service.process_result_callback(
        out_sum=order.amount, inv_id=order.id, signature=forged_signature,
    ))

    assert outcome.ok is False
    assert outcome.reason == "bad_signature"
    subscription = run(subscriptions.get_for_workspace(workspace_id))
    assert subscription is None or subscription.status is not SubscriptionStatus.ACTIVE


def test_tampered_out_sum_is_rejected_even_with_a_validly_computed_signature(tmp_path: Path):
    """An attacker who ALSO knows Password2 (e.g. a leaked/guessed value)
    but tries to pay a smaller amount than the order was created for -
    the amount cross-check against the STORED order.amount must still
    reject this independent of signature validity."""
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _workspace(db_path)
    service, orders, subscriptions = _service(db_path)
    order = run(orders.create_order(workspace_id=workspace_id, plan="standard", amount="999.00"))
    fake_amount = "1.00"
    signature = build_result_signature(out_sum=fake_amount, inv_id=order.id, password2="pw2-test")

    outcome = run(service.process_result_callback(
        out_sum=fake_amount, inv_id=order.id, signature=signature,
    ))

    assert outcome.ok is False
    assert outcome.reason == "amount_mismatch"
    assert run(orders.get_order(order.id)).status.value == "created"
    subscription = run(subscriptions.get_for_workspace(workspace_id))
    assert subscription is None or subscription.status is not SubscriptionStatus.ACTIVE


def test_unknown_inv_id_is_rejected(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    _workspace(db_path)
    service, _, _ = _service(db_path)
    signature = build_result_signature(out_sum="999.00", inv_id=999999, password2="pw2-test")

    outcome = run(service.process_result_callback(
        out_sum="999.00", inv_id=999999, signature=signature,
    ))

    assert outcome.ok is False
    assert outcome.reason == "unknown_order"


def test_repeated_callback_does_not_extend_the_subscription_twice(tmp_path: Path):
    """Idempotency end-to-end: RoboKassa is documented to retry ResultURL
    notifications - a second identical, validly-signed callback for the
    same InvId must not add a second billing period."""
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _workspace(db_path)
    service, orders, subscriptions = _service(db_path)
    order = run(orders.create_order(workspace_id=workspace_id, plan="standard", amount="999.00"))
    callback = _signed_callback(order, "pw2-test")

    first_outcome = run(service.process_result_callback(**callback))
    first_paid_until = run(subscriptions.get_for_workspace(workspace_id)).paid_until

    second_outcome = run(service.process_result_callback(**callback))
    second_paid_until = run(subscriptions.get_for_workspace(workspace_id)).paid_until

    assert first_outcome.ok is True
    assert second_outcome.ok is True
    assert first_paid_until == second_paid_until


def test_renewal_extends_a_future_paid_until_instead_of_resetting_it(tmp_path: Path):
    """The renewal period comes from the ORDER's own duration_days (frozen
    at create_order() time from app.services.plans.PLAN_CATALOG) - not
    from RoboKassaConfig.subscription_days, which no longer feeds this
    path at all (see BillingService._extend_subscription)."""
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _workspace(db_path)
    service, orders, subscriptions = _service(db_path)

    far_future = (datetime.now(timezone.utc) + timedelta(days=20)).isoformat()
    run(subscriptions.mark_paid(
        workspace_id, external_payment_id="prior", payment_provider="robokassa",
        paid_until=far_future, plan=SubscriptionPlan.STANDARD,
    ))

    order = run(orders.create_order(
        workspace_id=workspace_id, plan="start", amount="490.00", duration_days=14,
    ))
    run(service.process_result_callback(**_signed_callback(order, "pw2-test")))

    updated = run(subscriptions.get_for_workspace(workspace_id))
    new_paid_until = datetime.fromisoformat(updated.paid_until)
    old_paid_until = datetime.fromisoformat(far_future)
    # extended BY the order's own 14 days from the existing future date,
    # not reset to "now + 14 days".
    assert new_paid_until > old_paid_until
    assert (new_paid_until - old_paid_until) == timedelta(days=14)
    assert updated.plan is SubscriptionPlan.START


def test_result_callback_rejected_when_not_configured(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    _workspace(db_path)
    service, _, _ = _service(db_path, password2="")

    outcome = run(service.process_result_callback(
        out_sum="999.00", inv_id=1, signature="anything",
    ))

    assert outcome.ok is False
    assert outcome.reason == "not_configured"


# ── ORCHESTRAVEL default source pack (stage 1) ──────────────────────────────
#
# BillingService only ever calls source_catalog_repository.assign_default_
# sources(workspace_id) - correctness of that method itself (idempotency,
# isolation, etc.) is covered in tests/test_source_catalog_repository.py.
# These tests cover WHEN it is (and is not) called - a plain recording fake
# is enough for that, no real SQLite-backed SourceCatalogRepository needed.

class _RecordingSourceCatalog:
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def assign_default_sources(self, workspace_id: int, *args, **kwargs) -> None:
        self.calls.append(workspace_id)


def _pending_signup_workspace(db_path: Path, subscriptions: SubscriptionRepository) -> int:
    """Mirrors the real self-service signup flow (app.repositories.
    partner_repository.provision_self_service_workspace + immediately
    SubscriptionRepository.create_pending - see app.web_api's POST
    /api/auth/signup) rather than _workspace()'s CLI/owner path above,
    which grandfathers straight in as 'beta' via SubscriptionRepository.
    init()'s backfill - NOT the "never paid yet" state stage 1 cares about.

    Ordering matters: the workspace is provisioned AFTER `subscriptions`
    already exists (so its init()'s backfill-for-existing-workspaces has
    nothing to grab yet), then create_pending() sets the real 'pending'
    status explicitly - exactly the sequence a live signup goes through.
    """
    partners = PartnerRepository(db_path)
    run(partners.init())
    provisioned = run(partners.provision_self_service_workspace(
        "Test Business", base_slug=f"test-biz-{id(db_path)}",
    ))
    run(subscriptions.create_pending(provisioned.workspace.id))
    return provisioned.workspace.id


def _service_with_source_catalog(db_path: Path):
    # partner_workspaces must exist BEFORE subscriptions.init()'s backfill
    # query runs against it - and must still have zero rows at that point,
    # so _pending_signup_workspace()'s later provision_self_service_
    # workspace() is free to set the real 'pending' status itself instead
    # of being grandfathered as 'beta' by the backfill (see that helper's
    # docstring for why the ordering matters).
    run(PartnerRepository(db_path).init())
    config = RoboKassaConfig(
        merchant_login="orchestravel-test", password1="pw1-test", password2="pw2-test",
        is_test=True, standard_price_rub=Decimal("999.00"), subscription_days=30,
        public_base_url="https://app.orchestravel.ru",
    )
    orders = PaymentOrderRepository(db_path)
    run(orders.init())
    subscriptions = SubscriptionRepository(db_path)
    run(subscriptions.init())
    source_catalog = _RecordingSourceCatalog()
    service = BillingService(
        config=config, payment_order_repository=orders,
        subscription_repository=subscriptions, source_catalog_repository=source_catalog,
    )
    return service, orders, subscriptions, source_catalog


def test_first_successful_payment_for_a_new_signup_assigns_default_sources(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    service, orders, subscriptions, source_catalog = _service_with_source_catalog(db_path)
    workspace_id = _pending_signup_workspace(db_path, subscriptions)
    order = run(orders.create_order(workspace_id=workspace_id, plan="standard", amount="999.00"))

    outcome = run(service.process_result_callback(**_signed_callback(order, "pw2-test")))

    assert outcome.ok is True
    assert source_catalog.calls == [workspace_id]


def test_pending_subscription_never_assigns_sources_before_payment(tmp_path: Path):
    """Requirement: sources are never assigned before a successful
    payment - a pending signup alone must not trigger anything."""
    db_path = tmp_path / "db.sqlite3"
    _, _, subscriptions, source_catalog = _service_with_source_catalog(db_path)
    _pending_signup_workspace(db_path, subscriptions)

    assert source_catalog.calls == []


def test_rejected_callback_never_assigns_default_sources(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    service, orders, subscriptions, source_catalog = _service_with_source_catalog(db_path)
    workspace_id = _pending_signup_workspace(db_path, subscriptions)
    order = run(orders.create_order(workspace_id=workspace_id, plan="standard", amount="999.00"))

    outcome = run(service.process_result_callback(
        out_sum=order.amount, inv_id=order.id, signature="0" * 32,
    ))

    assert outcome.ok is False
    assert source_catalog.calls == []


def test_replayed_webhook_assigns_default_sources_only_once(tmp_path: Path):
    """Requirement: a replayed RoboKassa ResultURL notification for the
    same already-paid order must not call assign_default_sources twice."""
    db_path = tmp_path / "db.sqlite3"
    service, orders, subscriptions, source_catalog = _service_with_source_catalog(db_path)
    workspace_id = _pending_signup_workspace(db_path, subscriptions)
    order = run(orders.create_order(workspace_id=workspace_id, plan="standard", amount="999.00"))
    callback = _signed_callback(order, "pw2-test")

    run(service.process_result_callback(**callback))
    run(service.process_result_callback(**callback))

    assert source_catalog.calls == [workspace_id]


def test_grandfathered_beta_workspace_first_payment_does_not_assign_default_sources(tmp_path: Path):
    """Requirement: existing/legacy workspaces must not be changed by this
    patch - a CLI/owner-provisioned workspace (grandfathered in as 'beta'
    by SubscriptionRepository.init()'s backfill, never 'pending') making
    its first tracked payment here must NOT get the default pack."""
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _workspace(db_path)
    service, orders, subscriptions, source_catalog = _service_with_source_catalog(db_path)
    assert run(subscriptions.get_for_workspace(workspace_id)).status is SubscriptionStatus.BETA
    order = run(orders.create_order(workspace_id=workspace_id, plan="standard", amount="999.00"))

    run(service.process_result_callback(**_signed_callback(order, "pw2-test")))

    assert source_catalog.calls == []


def test_renewal_of_an_already_active_workspace_does_not_assign_default_sources(tmp_path: Path):
    """Requirement: a second/renewal payment for a workspace that is
    already 'active' (not a first-ever activation) must not assign
    default sources again."""
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _workspace(db_path)
    service, orders, subscriptions, source_catalog = _service_with_source_catalog(db_path)
    far_future = (datetime.now(timezone.utc) + timedelta(days=20)).isoformat()
    run(subscriptions.mark_paid(
        workspace_id, external_payment_id="prior", payment_provider="robokassa",
        paid_until=far_future, plan=SubscriptionPlan.STANDARD,
    ))
    order = run(orders.create_order(workspace_id=workspace_id, plan="standard", amount="999.00"))

    run(service.process_result_callback(**_signed_callback(order, "pw2-test")))

    assert source_catalog.calls == []


def test_billing_service_without_source_catalog_repository_still_activates_payment(tmp_path: Path):
    """Backward compatibility: source_catalog_repository defaults to None -
    every pre-existing caller/test in this file (which never passes it)
    keeps working exactly as before this feature existed."""
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _workspace(db_path)
    service, orders, subscriptions = _service(db_path)
    order = run(orders.create_order(workspace_id=workspace_id, plan="standard", amount="999.00"))

    outcome = run(service.process_result_callback(**_signed_callback(order, "pw2-test")))

    assert outcome.ok is True
    assert run(subscriptions.get_for_workspace(workspace_id)).status is SubscriptionStatus.ACTIVE


# ── owner payment notification ──────────────────────────────────────────────
# A fake OwnerPaymentNotifier throughout - never a real aiogram.Bot, so none
# of these tests can ever make a real network call or send a real Telegram
# message, regardless of ambient BOT_TOKEN/ADMIN_TELEGRAM_ID env.


class _RecordingOwnerNotifier:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list = []
        self._fail = fail

    async def notify(self, order, *, business_name, email) -> bool:
        self.calls.append((order.id, order.plan, business_name, email))
        return not self._fail


def _service_with_owner_notifier(db_path: Path, *, notifier_fails: bool = False):
    run(PartnerRepository(db_path).init())
    config = RoboKassaConfig(
        merchant_login="orchestravel-test", password1="pw1-test", password2="pw2-test",
        is_test=True, standard_price_rub=Decimal("999.00"), subscription_days=30,
        public_base_url="https://app.orchestravel.ru",
    )
    orders = PaymentOrderRepository(db_path)
    run(orders.init())
    subscriptions = SubscriptionRepository(db_path)
    run(subscriptions.init())
    notifier = _RecordingOwnerNotifier(fail=notifier_fails)
    service = BillingService(
        config=config, payment_order_repository=orders,
        subscription_repository=subscriptions, owner_notifier=notifier,
    )
    return service, orders, subscriptions, notifier


def test_successful_start_payment_sends_exactly_one_owner_notification(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    service, orders, _, notifier = _service_with_owner_notifier(db_path)
    workspace_id = _workspace(db_path)
    order = run(orders.create_order(
        workspace_id=workspace_id, plan="start", amount="490.00", duration_days=14,
    ))

    outcome = run(service.process_result_callback(**_signed_callback(order, "pw2-test")))

    assert outcome.ok is True
    assert notifier.calls == [(order.id, "start", None, None)]


def test_successful_standard_payment_sends_exactly_one_owner_notification(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    service, orders, _, notifier = _service_with_owner_notifier(db_path)
    workspace_id = _workspace(db_path)
    order = run(orders.create_order(
        workspace_id=workspace_id, plan="standard", amount="990.00", duration_days=30,
    ))

    run(service.process_result_callback(**_signed_callback(order, "pw2-test")))

    assert notifier.calls == [(order.id, "standard", None, None)]


def test_successful_full_payment_sends_one_notification_flagged_full(tmp_path: Path):
    """The FULL-specific "personal onboarding" note is rendered by
    build_owner_payment_notification_text (see
    tests/test_owner_payment_notifications.py) - this only asserts the
    dedup/call-count contract and that order.plan=="full" reaches the
    notifier, which is what the message-building test depends on."""
    db_path = tmp_path / "db.sqlite3"
    service, orders, _, notifier = _service_with_owner_notifier(db_path)
    workspace_id = _workspace(db_path)
    order = run(orders.create_order(
        workspace_id=workspace_id, plan="full", amount="1490.00", duration_days=30,
    ))

    run(service.process_result_callback(**_signed_callback(order, "pw2-test")))

    assert notifier.calls == [(order.id, "full", None, None)]


def test_replayed_callback_never_sends_a_second_owner_notification(tmp_path: Path):
    """RoboKassa is documented to retry ResultURL - the exact scenario this
    feature must never double-fire for."""
    db_path = tmp_path / "db.sqlite3"
    service, orders, _, notifier = _service_with_owner_notifier(db_path)
    workspace_id = _workspace(db_path)
    order = run(orders.create_order(workspace_id=workspace_id, plan="standard", amount="999.00"))
    callback = _signed_callback(order, "pw2-test")

    first = run(service.process_result_callback(**callback))
    second = run(service.process_result_callback(**callback))

    assert first.ok is True
    assert second.ok is True
    assert len(notifier.calls) == 1


def test_rejected_payment_never_sends_owner_notification(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    service, orders, _, notifier = _service_with_owner_notifier(db_path)
    workspace_id = _workspace(db_path)
    order = run(orders.create_order(workspace_id=workspace_id, plan="standard", amount="999.00"))

    outcome = run(service.process_result_callback(
        out_sum=order.amount, inv_id=order.id, signature="0" * 32,
    ))

    assert outcome.ok is False
    assert notifier.calls == []


def test_pending_unpaid_order_never_sends_owner_notification(tmp_path: Path):
    """create_payment() (link creation) never calls process_result_callback
    at all - a created-but-not-yet-paid order must never have triggered a
    notification in the first place."""
    db_path = tmp_path / "db.sqlite3"
    service, orders, _, notifier = _service_with_owner_notifier(db_path)
    workspace_id = _workspace(db_path)
    run(orders.create_order(workspace_id=workspace_id, plan="standard", amount="999.00"))

    assert notifier.calls == []


def test_owner_notification_failure_does_not_break_payment_or_activation(tmp_path: Path):
    """Telegram send failure must never fail the RoboKassa callback, the
    payment record, or subscription activation - payment matters more than
    the notification."""
    db_path = tmp_path / "db.sqlite3"
    service, orders, subscriptions, notifier = _service_with_owner_notifier(
        db_path, notifier_fails=True,
    )
    workspace_id = _workspace(db_path)
    order = run(orders.create_order(workspace_id=workspace_id, plan="standard", amount="999.00"))

    outcome = run(service.process_result_callback(**_signed_callback(order, "pw2-test")))

    assert outcome.ok is True
    assert run(orders.get_order(order.id)).status.value == "paid"
    assert run(subscriptions.get_for_workspace(workspace_id)).status is SubscriptionStatus.ACTIVE
    assert len(notifier.calls) == 1  # attempted exactly once


def test_owner_notification_failure_leaves_owner_notified_at_unset_for_retry(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    service, orders, _, notifier = _service_with_owner_notifier(db_path, notifier_fails=True)
    workspace_id = _workspace(db_path)
    order = run(orders.create_order(workspace_id=workspace_id, plan="standard", amount="999.00"))

    run(service.process_result_callback(**_signed_callback(order, "pw2-test")))

    assert run(orders.get_order(order.id)).owner_notified_at is None


def test_owner_notification_success_persists_owner_notified_at(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    service, orders, _, notifier = _service_with_owner_notifier(db_path)
    workspace_id = _workspace(db_path)
    order = run(orders.create_order(workspace_id=workspace_id, plan="standard", amount="999.00"))

    run(service.process_result_callback(**_signed_callback(order, "pw2-test")))

    assert run(orders.get_order(order.id)).owner_notified_at is not None


def test_owner_notifier_raising_unexpectedly_does_not_break_payment(tmp_path: Path):
    """Defense in depth: even if a notifier implementation violates its own
    contract and raises, the payment/activation must still succeed."""
    class _RaisingNotifier:
        async def notify(self, order, *, business_name, email):
            raise RuntimeError("boom")

    db_path = tmp_path / "db.sqlite3"
    run(PartnerRepository(db_path).init())
    config = RoboKassaConfig(
        merchant_login="orchestravel-test", password1="pw1-test", password2="pw2-test",
        is_test=True, standard_price_rub=Decimal("999.00"), subscription_days=30,
        public_base_url="https://app.orchestravel.ru",
    )
    orders = PaymentOrderRepository(db_path)
    run(orders.init())
    subscriptions = SubscriptionRepository(db_path)
    run(subscriptions.init())
    service = BillingService(
        config=config, payment_order_repository=orders,
        subscription_repository=subscriptions, owner_notifier=_RaisingNotifier(),
    )
    workspace_id = _workspace(db_path)
    order = run(orders.create_order(workspace_id=workspace_id, plan="standard", amount="999.00"))

    outcome = run(service.process_result_callback(**_signed_callback(order, "pw2-test")))

    assert outcome.ok is True
    assert run(subscriptions.get_for_workspace(workspace_id)).status is SubscriptionStatus.ACTIVE


def test_billing_service_without_owner_notifier_still_activates_payment(tmp_path: Path):
    """Backward compatibility: owner_notifier defaults to None - every
    pre-existing caller/test in this file keeps working unaffected, and
    no notification is ever attempted."""
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _workspace(db_path)
    service, orders, subscriptions = _service(db_path)
    order = run(orders.create_order(workspace_id=workspace_id, plan="standard", amount="999.00"))

    outcome = run(service.process_result_callback(**_signed_callback(order, "pw2-test")))

    assert outcome.ok is True
    assert run(subscriptions.get_for_workspace(workspace_id)).status is SubscriptionStatus.ACTIVE


def test_owner_notification_includes_business_name_and_email_when_available(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    run(PartnerRepository(db_path).init())
    config = RoboKassaConfig(
        merchant_login="orchestravel-test", password1="pw1-test", password2="pw2-test",
        is_test=True, standard_price_rub=Decimal("999.00"), subscription_days=30,
        public_base_url="https://app.orchestravel.ru",
    )
    orders = PaymentOrderRepository(db_path)
    run(orders.init())
    subscriptions = SubscriptionRepository(db_path)
    run(subscriptions.init())
    notifier = _RecordingOwnerNotifier()

    class _FakePartnerRepository:
        async def get_business_profile(self, workspace_id: int):
            from types import SimpleNamespace
            return SimpleNamespace(business_name="Тревел Клуб")

    class _FakeWebAuthRepository:
        async def get_primary_email_for_workspace(self, workspace_id: int):
            return "owner@example.com"

    service = BillingService(
        config=config, payment_order_repository=orders,
        subscription_repository=subscriptions, owner_notifier=notifier,
        partner_repository=_FakePartnerRepository(),
        web_auth_repository=_FakeWebAuthRepository(),
    )
    workspace_id = _workspace(db_path)
    order = run(orders.create_order(workspace_id=workspace_id, plan="standard", amount="999.00"))

    run(service.process_result_callback(**_signed_callback(order, "pw2-test")))

    assert notifier.calls == [(order.id, "standard", "Тревел Клуб", "owner@example.com")]
