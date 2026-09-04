from __future__ import annotations

import asyncio
from pathlib import Path

from app.domain.billing import PaymentOrderStatus
from app.repositories.partner_repository import PartnerRepository
from app.repositories.payment_order_repository import PaymentOrderRepository
from tests.test_workspace_signal_repository import workspace as _extra_workspace


def run(coro):
    return asyncio.run(coro)


def _workspace(db_path: Path, telegram_id: int = 100) -> int:
    partners = PartnerRepository(db_path)
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(telegram_id))
    return workspace.id


def test_create_order_returns_a_created_order_with_id_as_inv_id(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _workspace(db_path)
    orders = PaymentOrderRepository(db_path)
    run(orders.init())

    order = run(orders.create_order(
        workspace_id=workspace_id, plan="standard", amount="999.00",
    ))

    assert order.workspace_id == workspace_id
    assert order.plan == "standard"
    assert order.amount == "999.00"
    assert order.currency == "RUB"
    assert order.provider == "robokassa"
    assert order.status is PaymentOrderStatus.CREATED
    assert order.paid_at is None
    assert isinstance(order.id, int) and order.id > 0


def test_each_order_gets_a_distinct_id(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _workspace(db_path)
    orders = PaymentOrderRepository(db_path)
    run(orders.init())

    first = run(orders.create_order(workspace_id=workspace_id, plan="standard", amount="999.00"))
    second = run(orders.create_order(workspace_id=workspace_id, plan="standard", amount="999.00"))

    assert first.id != second.id


def test_get_order_returns_none_for_unknown_id(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    _workspace(db_path)
    orders = PaymentOrderRepository(db_path)
    run(orders.init())

    assert run(orders.get_order(999999)) is None


def test_mark_paid_transitions_created_to_paid(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _workspace(db_path)
    orders = PaymentOrderRepository(db_path)
    run(orders.init())
    order = run(orders.create_order(workspace_id=workspace_id, plan="standard", amount="999.00"))

    updated, transitioned = run(orders.mark_paid(order.id))

    assert transitioned is True
    assert updated.status is PaymentOrderStatus.PAID
    assert updated.paid_at is not None


def test_mark_paid_is_idempotent_on_repeat_calls(tmp_path: Path):
    """The core idempotency guarantee: a second mark_paid() for the same
    order must report transitioned_now=False, so a caller (BillingService)
    knows not to extend the subscription again."""
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _workspace(db_path)
    orders = PaymentOrderRepository(db_path)
    run(orders.init())
    order = run(orders.create_order(workspace_id=workspace_id, plan="standard", amount="999.00"))

    first_order, first_transitioned = run(orders.mark_paid(order.id))
    second_order, second_transitioned = run(orders.mark_paid(order.id))

    assert first_transitioned is True
    assert second_transitioned is False
    assert first_order.paid_at == second_order.paid_at
    assert second_order.status is PaymentOrderStatus.PAID


def test_mark_paid_on_unknown_order_returns_none_and_false(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    _workspace(db_path)
    orders = PaymentOrderRepository(db_path)
    run(orders.init())

    order, transitioned = run(orders.mark_paid(999999))

    assert order is None
    assert transitioned is False


def test_order_belongs_to_exactly_the_workspace_it_was_created_for(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    workspace_a = _workspace(db_path, telegram_id=100)
    workspace_b = _extra_workspace(db_path, 200)

    orders = PaymentOrderRepository(db_path)
    run(orders.init())
    order = run(orders.create_order(workspace_id=workspace_a, plan="standard", amount="999.00"))

    fetched = run(orders.get_order(order.id))
    assert fetched.workspace_id == workspace_a
    assert fetched.workspace_id != workspace_b
