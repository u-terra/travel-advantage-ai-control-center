"""Subscription state per workspace - the single source of truth for
whether a workspace's access is granted, in BOTH Telegram and Web (see
app/services/access_state.py for the shared pure grant/deny reduction and
app/repositories/subscription_repository.py for the read/write side).

Not a payment integration on its own - RoboKassa (or any real payment
provider) still has no webhook wired to this table; external_payment_id/
payment_provider/paid_until exist so that integration point is ready when
it's built (see SubscriptionRepository.mark_paid), same as before.

status vs plan are two separate axes, on purpose:
- status: where THIS workspace is in the access lifecycle right now
  (trial/beta/active/past_due/expired/suspended) - the input to
  app.services.access_state.compute_access_state's grant/deny decision.
- plan: which tier of the product this workspace is entitled to
  (beta/standard) - not enforced anywhere yet (no per-plan limits exist),
  kept as its own column so a future limits check (via
  app.repositories.usage_ledger_repository) has something to key off of
  without another schema change later.

partner_workspaces.access_status/access_expires_at (app/domain/partners.py)
is the PREVIOUS Stage 3A mechanism this table replaces as the live gate -
kept in the schema only as a deprecated, read-once migration seed (see
SubscriptionRepository.init()'s backfill), never written to again and
never read by AccessStateMiddleware or the Web gate anymore.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SubscriptionStatus(str, Enum):
    TRIAL = "trial"
    BETA = "beta"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    EXPIRED = "expired"
    SUSPENDED = "suspended"


class SubscriptionPlan(str, Enum):
    BETA = "beta"
    STANDARD = "standard"


@dataclass(frozen=True)
class Subscription:
    workspace_id: int
    status: SubscriptionStatus
    plan: SubscriptionPlan
    started_at: str
    trial_until: str | None
    paid_until: str | None
    external_payment_id: str | None
    payment_provider: str | None
    updated_at: str
