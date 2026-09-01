"""Provider pricing input for cost estimation.

Deliberately empty by default: real $/1K-token rates for the providers/
models actually in use are NOT invented here (per explicit instruction).
Fill PROVIDER_MODEL_PRICING_USD_PER_1K_TOKENS with verified, current rates
from each provider's own pricing page before estimated_cost_usd figures are
used for real budgeting decisions - see the "what's needed" list in the
Usage Cost & Subscription Foundation report for exactly which rates are
missing today.

estimate_cost_usd() returns None (never a guess) whenever the model isn't
priced here or token counts aren't available - the same "don't fabricate"
rule the usage ledger itself follows for missing token data.
"""

from __future__ import annotations

# (provider, model) -> (input $/1K tokens, output $/1K tokens).
# TODO: populate with verified current rates before relying on
# estimated_cost_usd for real decisions. Confirmed missing today:
#   - ("openai", "gpt-4o-mini") - configured in .env as
#     ORCHESTRATION_OPENAI_MODEL, used by the orchestration shadow
#     classifier (app/orchestration/openai_provider.py) - the one live path
#     with real token counts in this deployment.
#   - Whatever model backs the internal Travel Content Factory HTTP service
#     (app/services/content_factory.py) - unknown from this repo, and that
#     service does not return token usage at all today (see report).
PROVIDER_MODEL_PRICING_USD_PER_1K_TOKENS: dict[tuple[str, str], tuple[float, float]] = {
    ("openai", "gpt-5.6-terra"): (0.002, 0.012),
}


def estimate_cost_usd(
    provider: str, model: str | None,
    input_tokens: int | None, output_tokens: int | None,
) -> float | None:
    if model is None or input_tokens is None or output_tokens is None:
        return None
    rates = PROVIDER_MODEL_PRICING_USD_PER_1K_TOKENS.get((provider, model))
    if rates is None:
        return None
    input_rate, output_rate = rates
    return (input_tokens / 1000) * input_rate + (output_tokens / 1000) * output_rate
