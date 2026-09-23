"""Payment order/invoice - audit + idempotency record for a single billing
attempt, kept deliberately separate from workspace_subscriptions (see
app/domain/subscription.py). workspace_subscriptions answers "is this
workspace's access granted right now"; a PaymentOrder answers "did this
specific payment attempt happen, and did it succeed" - a journal entry, not
the subscription state itself.

id doubles as RoboKassa's InvId: we generate it (AUTOINCREMENT), send it to
RoboKassa when creating the payment, and RoboKassa echoes it back verbatim
on the ResultURL/SuccessURL/FailURL callbacks - so every InvId RoboKassa
ever mentions maps to exactly one (workspace_id, plan, amount) triple we
already committed to before redirecting the browser, never something a
client could still influence.

No external_payment_id field: RoboKassa's base ResultURL scheme (no
Shp_* custom params, no fiscalization add-ons) never sends a second
provider-side transaction id - InvId already is the one correlation key
both sides agree on. Never add a column that would stay permanently NULL.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PaymentOrderStatus(str, Enum):
    CREATED = "created"
    PAID = "paid"


@dataclass(frozen=True)
class PaymentOrder:
    id: int
    workspace_id: int
    plan: str
    amount: str
    currency: str
    provider: str
    status: PaymentOrderStatus
    created_at: str
    paid_at: str | None
    # How many days of access this specific order grants once paid - read
    # from app.services.plans.PLAN_CATALOG at creation time and frozen onto
    # the order, so a later catalog change never changes what an
    # already-created (possibly already-paid) order is worth. Defaults to
    # 30 for rows created before this column existed (see
    # PaymentOrderRepository's additive migration) - the same duration
    # every pre-existing 'standard' order was always sold for.
    duration_days: int = 30
