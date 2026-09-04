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
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _workspace(db_path)
    service, orders, _ = _service(db_path)

    result = run(service.create_payment(workspace_id))

    assert result.order.workspace_id == workspace_id
    assert result.order.plan == "standard"
    assert result.order.amount == "999.00"
    assert result.is_test is True

    query = parse_qs(urlsplit(result.payment_url).query)
    assert query["MerchantLogin"][0] == "orchestravel-test"
    assert query["OutSum"][0] == "999.00"
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


def test_create_payment_price_comes_only_from_config_not_a_parameter(tmp_path: Path):
    """create_payment() takes only workspace_id - there is no amount/plan
    parameter for a caller to influence at all."""
    import inspect
    assert list(inspect.signature(BillingService.create_payment).parameters) == [
        "self", "workspace_id",
    ]


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
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _workspace(db_path)
    service, orders, subscriptions = _service(db_path, subscription_days=10)

    far_future = (datetime.now(timezone.utc) + timedelta(days=20)).isoformat()
    run(subscriptions.mark_paid(
        workspace_id, external_payment_id="prior", payment_provider="robokassa",
        paid_until=far_future, plan=SubscriptionPlan.STANDARD,
    ))

    order = run(orders.create_order(workspace_id=workspace_id, plan="standard", amount="999.00"))
    run(service.process_result_callback(**_signed_callback(order, "pw2-test")))

    updated = run(subscriptions.get_for_workspace(workspace_id))
    new_paid_until = datetime.fromisoformat(updated.paid_until)
    old_paid_until = datetime.fromisoformat(far_future)
    # extended BY the new period from the existing future date, not reset
    # to "now + subscription_days".
    assert new_paid_until > old_paid_until
    assert (new_paid_until - old_paid_until) == timedelta(days=10)


def test_result_callback_rejected_when_not_configured(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    _workspace(db_path)
    service, _, _ = _service(db_path, password2="")

    outcome = run(service.process_result_callback(
        out_sum="999.00", inv_id=1, signature="anything",
    ))

    assert outcome.ok is False
    assert outcome.reason == "not_configured"
