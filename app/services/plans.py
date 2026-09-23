"""Server-side catalog of the real ORCHESTRAVEL starter tariffs.

Deliberately code, not ENV: amount/plan/duration for a payment must be
something ONLY the server decides (see app.services.billing_service) - a
static Python mapping can't be influenced by a request body, and unlike
ORCHESTRAVEL_STANDARD_PRICE_RUB (a single legacy price, still read by
app.config for the old single-tier billing_status display) it doesn't
require touching production ENV to introduce three real tariffs.

ROBOKASSA_IS_TEST decides whether a payment actually moves money - it
does not care what amount a test-mode order carries, so shipping these
three real prices while the merchant account stays in test mode is safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class PlanDefinition:
    code: str
    label: str
    amount: Decimal
    duration_days: int


PLAN_CATALOG: dict[str, PlanDefinition] = {
    "start": PlanDefinition(
        code="start", label="START / 14 дней",
        amount=Decimal("490.00"), duration_days=14,
    ),
    "standard": PlanDefinition(
        code="standard", label="STANDARD / 30 дней",
        amount=Decimal("990.00"), duration_days=30,
    ),
    "full": PlanDefinition(
        code="full", label="FULL / 30 дней",
        amount=Decimal("1490.00"), duration_days=30,
    ),
}

DEFAULT_PLAN_CODE = "standard"


def get_plan(code: str) -> PlanDefinition | None:
    return PLAN_CATALOG.get(code)


def list_plans() -> list[PlanDefinition]:
    """Stable, deliberate display order (start -> standard -> full) -
    never dict iteration order by accident."""
    return [PLAN_CATALOG["start"], PLAN_CATALOG["standard"], PLAN_CATALOG["full"]]
