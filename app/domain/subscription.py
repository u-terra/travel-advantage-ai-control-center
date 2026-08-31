"""Subscription foundation - separate from (and does not modify) the
existing Stage 3A access-state gate (app/services/access_state.py,
partner_workspaces.access_status). That gate keeps operating exactly as
today; this is the billing-state table RoboKassa will eventually write to.
See app/repositories/subscription_repository.py for the integration point.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SubscriptionStatus(str, Enum):
    BETA = "beta"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    EXPIRED = "expired"


@dataclass(frozen=True)
class Subscription:
    workspace_id: int
    status: SubscriptionStatus
    started_at: str
    paid_until: str | None
    external_payment_id: str | None
    payment_provider: str | None
    updated_at: str
