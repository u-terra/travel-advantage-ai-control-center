"""Qualify an already-verified candidate pool in batches.

Discovery verifies up to ~10 pages, but the LLM qualifies only a batch of
``batch_size`` at a time. Previously the first five were taken BEFORE
qualification and the rest were dropped unscored. Here the remaining pool
candidates are qualified batch by batch - with the very same enrichment,
thresholds and hard exclusions - until ``max_results`` qualified candidates
are collected, the pool is exhausted, or the time budget runs out. No search
is re-run; nothing is lowered or substituted.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.chat_provider import OpenAIChatProvider
from app.services.lead_qualification import (
    QUALIFICATION_ATTEMPT_TIMEOUT_S,
    QUALIFICATION_MAX_ATTEMPTS,
    QualificationUnavailable,
    qualify_candidates,
)

log = logging.getLogger(__name__)

BATCH_SIZE = 5


@dataclass
class PoolQualification:
    candidates: list[dict]
    checked: int
    total: int
    # None = the whole needed part of the pool was checked. Otherwise why the
    # check stopped early: "budget" or "exception"-style reason.
    incomplete_reason: str | None = None

    @property
    def incomplete(self) -> bool:
        return self.incomplete_reason is not None


async def qualify_pool(
    pool: list[dict],
    llm_provider: OpenAIChatProvider | None,
    *,
    enrich: Callable[[list[dict]], Awaitable[list[dict]]],
    enrich_fallback: Callable[[list[dict]], list[dict]],
    rank: Callable[[list[dict]], list[dict]],
    min_score: int,
    min_confidence: float,
    max_results: int,
    batch_size: int = BATCH_SIZE,
    deadline: float | None = None,        # time.monotonic() value; None = unlimited
    enrich_max_seconds: float = 40.0,
    qualify_min_seconds: float = 20.0,
    margin_seconds: float = 3.0,
) -> PoolQualification:
    """Raises QualificationUnavailable when the FIRST batch cannot be
    qualified (nothing was actually checked). Later-batch failures or budget
    exhaustion return what was found with ``incomplete_reason`` set."""
    qualified: list[dict] = []
    checked = 0
    total = len(pool)
    incomplete_reason: str | None = None

    for start in range(0, total, batch_size):
        if len(qualified) >= max_results:
            break
        batch = pool[start:start + batch_size]
        first = start == 0

        enrich_timeout: float | None = None
        llm_timeout: float | None = None
        if deadline is not None:
            remaining = deadline - time.monotonic()
            # A further batch needs room for enrichment AND qualification.
            needed = qualify_min_seconds + margin_seconds + (0 if first else enrich_max_seconds)
            if remaining < needed:
                if first:
                    log.warning("partner_pool: first batch skipped - only %.0fs of budget left", remaining)
                    raise QualificationUnavailable("budget")
                incomplete_reason = "budget"
                break
            enrich_timeout = max(1.0, min(enrich_max_seconds, remaining - qualify_min_seconds - margin_seconds))

        try:
            if enrich_timeout is not None:
                try:
                    batch = await asyncio.wait_for(enrich(batch), timeout=enrich_timeout)
                except asyncio.TimeoutError:
                    log.warning("partner_pool: enrichment timed out after %.0fs - fallback", enrich_timeout)
                    batch = enrich_fallback(batch)
            else:
                batch = await enrich(batch)
            batch = rank(batch)
            if deadline is not None:
                llm_timeout = deadline - time.monotonic() - margin_seconds
                if llm_timeout < qualify_min_seconds:
                    if first:
                        raise QualificationUnavailable("budget")
                    incomplete_reason = "budget"
                    break
            log.info(
                "partner_pool.batch start=%d size=%d qualified_so_far=%d pool=%d",
                start, len(batch), len(qualified), total,
            )
            result = await qualify_candidates(
                batch, llm_provider,
                min_score=min_score, min_confidence=min_confidence,
                max_results=max_results, require_llm=True,
                attempt_timeout_seconds=QUALIFICATION_ATTEMPT_TIMEOUT_S,
                max_attempts=QUALIFICATION_MAX_ATTEMPTS,
                deadline=None if deadline is None else deadline - margin_seconds,
            )
        except QualificationUnavailable as exc:
            if first:
                raise
            log.warning("partner_pool: later batch failed reason=%s - returning partial", exc.reason)
            incomplete_reason = exc.reason
            break
        checked += len(batch)
        qualified.extend(result)

    qualified.sort(key=lambda c: c["total_score"], reverse=True)
    qualified = qualified[:max_results]
    if incomplete_reason is None and len(qualified) < max_results and checked < total:
        incomplete_reason = "budget"  # defensive: loop ended with unchecked pool
    log.info(
        "partner_pool.done checked=%d/%d returned=%d incomplete=%s",
        checked, total, len(qualified), incomplete_reason,
    )
    return PoolQualification(qualified, checked, total, incomplete_reason)
