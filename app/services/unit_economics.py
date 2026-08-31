"""Unit economics calculator: per-user cost = LLM/API variable cost +
allocated infrastructure cost + payment fee + tax/support reserve.

Every input below is a PARAMETER, not a hardcoded price - this repo has no
real provider rates, no real infra bill, and no real payment-fee percentage
configured anywhere (confirmed during the Usage Cost & Subscription
Foundation audit). Nothing here invents one. Call compute_unit_cost() with
real figures once they're known; the "light/typical/heavy" helpers below
exist to show the SHAPE of the calculation with clearly-labeled example
numbers, not a claim about actual cost today.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.usage import WorkspaceUsageSummary


@dataclass(frozen=True)
class CostInputs:
    """Every field is a real-world figure this repo does not have today -
    see the Usage Cost & Subscription Foundation report for the exact list
    of what to fill in before trusting compute_unit_cost()'s output."""
    # $/1K tokens is looked up via app.services.usage_pricing for whatever
    # usage actually happened - estimated_llm_cost_usd below is that sum,
    # already computed (or None if unpriced), not re-derived here.
    monthly_infrastructure_cost_usd: float  # VPS/hosting/DB, allocated per workspace
    active_workspaces_for_allocation: int  # denominator for the infra share
    payment_fee_rate: float  # e.g. RoboKassa's percentage, as a fraction (0.035 = 3.5%)
    tax_and_support_reserve_rate: float  # fraction of revenue reserved, e.g. 0.15


@dataclass(frozen=True)
class UnitCostBreakdown:
    workspace_id: int
    llm_variable_cost_usd: float | None  # None if any usage this period was unpriced
    allocated_infrastructure_cost_usd: float
    payment_fee_usd: float | None  # requires a subscription price to size the fee against
    tax_and_support_reserve_usd: float | None
    total_cost_usd: float | None
    revenue_usd: float | None


def compute_unit_cost(
    summary: WorkspaceUsageSummary, inputs: CostInputs, *,
    subscription_price_usd: float | None = None,
) -> UnitCostBreakdown:
    llm_cost = summary.estimated_cost_usd
    infra_share = (
        inputs.monthly_infrastructure_cost_usd / inputs.active_workspaces_for_allocation
        if inputs.active_workspaces_for_allocation > 0 else inputs.monthly_infrastructure_cost_usd
    )
    payment_fee = (
        subscription_price_usd * inputs.payment_fee_rate
        if subscription_price_usd is not None else None
    )
    tax_reserve = (
        subscription_price_usd * inputs.tax_and_support_reserve_rate
        if subscription_price_usd is not None else None
    )
    total = None
    if llm_cost is not None and payment_fee is not None and tax_reserve is not None:
        total = llm_cost + infra_share + payment_fee + tax_reserve
    return UnitCostBreakdown(
        workspace_id=summary.workspace_id,
        llm_variable_cost_usd=llm_cost,
        allocated_infrastructure_cost_usd=infra_share,
        payment_fee_usd=payment_fee,
        tax_and_support_reserve_usd=tax_reserve,
        total_cost_usd=total,
        revenue_usd=subscription_price_usd,
    )
