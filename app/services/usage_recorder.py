"""Single call-site helper for recording a usage_events row.

Best-effort by design: a usage-tracking failure must never break the actual
user-facing flow it's instrumenting. Call this right after an LLM/provider
call completes (success or failure), from wherever workspace_id/
telegram_user_id are already in scope - see the call sites listed in the
Usage Cost & Subscription Foundation report for the current (intentionally
partial - not every LLM call site is instrumented yet) coverage.
"""

from __future__ import annotations

import logging

from app.domain.usage import LLMUsage, UsageStatus
from app.repositories.usage_ledger_repository import UsageLedgerRepository
from app.services.usage_pricing import estimate_cost_usd

log = logging.getLogger(__name__)


async def record_llm_call(
    repository: UsageLedgerRepository | None, *,
    workspace_id: int, telegram_user_id: int | None, module: str,
    provider: str, model: str | None = None,
    usage: LLMUsage | None = None, status: UsageStatus,
) -> None:
    if repository is None:
        return
    input_tokens = usage.input_tokens if usage is not None else None
    output_tokens = usage.output_tokens if usage is not None else None
    total_tokens = usage.total_tokens if usage is not None else None
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    cost = estimate_cost_usd(provider, model, input_tokens, output_tokens)
    try:
        await repository.record(
            workspace_id=workspace_id, telegram_user_id=telegram_user_id, module=module,
            provider=provider, model=model, input_tokens=input_tokens,
            output_tokens=output_tokens, total_tokens=total_tokens,
            estimated_cost_usd=cost, status=status,
        )
    except Exception:
        log.warning("usage_recorder: failed to persist usage event", exc_info=True)
