"""Shadow-mode comparison: runs the LLM router alongside the old keyword
router WITHOUT ever affecting the user-facing reply.

Call ``run_shadow_orchestration`` AFTER the old router's reply has already
been sent to the user (see app.handlers.tasks.on_free_text). Every failure
mode - provider not configured, network error, timeout, invalid JSON,
unexpected exception - is swallowed here and only recorded as a status; it
never propagates, never blocks, never changes what the user already saw.

No chain-of-thought is logged: ShadowComparisonRecord carries structured
fields only (modules, booleans, a short reason_code, a bounded text
preview) - never the model's raw prose output.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from aiogram.fsm.context import FSMContext

from app.domain.business_profiles import BusinessProfile
from app.orchestration.context import recent_turns
from app.orchestration.decision import (
    InvalidOrchestrationDecisionError,
    OrchestrationDecision,
    parse_orchestration_decision,
)
from app.orchestration.provider import OrchestrationLLMProvider
from app.orchestration.request import build_orchestration_request
from app.routing.router import RouteDecision
from app.routing.safety import SafetyLevel

log = logging.getLogger(__name__)

_TASK_TEXT_PREVIEW_LEN = 120


@dataclass(frozen=True)
class ShadowComparisonRecord:
    workspace_id: int
    task_text_preview: str
    old_primary: str
    old_secondary: tuple[str, ...]
    old_safety: str
    status: str  # "ok" | "not_configured" | "error" | "invalid_output"
    latency_ms: int
    provider: str
    llm_intent: str | None = None
    llm_primary: str | None = None
    llm_secondary: tuple[str, ...] = field(default_factory=tuple)
    llm_safety: bool | None = None
    llm_confidence: float | None = None
    llm_reason_code: str | None = None
    agreement: bool | None = None


class ShadowComparisonLogger:
    """Default sink: one structured log line per comparison.

    Deliberately not a new SQLite table - see the Phase 1 architecture
    report. Swappable for a real repository later without touching callers,
    since it is injected (see run_shadow_orchestration's ``logger`` param).
    """

    def log(self, record: ShadowComparisonRecord) -> None:
        log.info(
            "orchestration_shadow "
            "workspace_id=%s status=%s provider=%s latency_ms=%s "
            "old_primary=%s old_secondary=%s old_safety=%s "
            "llm_intent=%s llm_primary=%s llm_secondary=%s llm_safety=%s "
            "llm_confidence=%s llm_reason_code=%s agreement=%s "
            "task_text_preview=%r",
            record.workspace_id, record.status, record.provider, record.latency_ms,
            record.old_primary, ",".join(record.old_secondary), record.old_safety,
            record.llm_intent, record.llm_primary, ",".join(record.llm_secondary),
            record.llm_safety, record.llm_confidence, record.llm_reason_code,
            record.agreement, record.task_text_preview,
        )


def compute_agreement(old: RouteDecision, new: OrchestrationDecision) -> bool:
    """Primary module matches AND both agree on whether Safety is required.

    Secondary modules are intentionally excluded from agreement: the old
    router's secondary-module set is a routing-collision artifact
    (MODULE_PRIORITY tie-breaks), not a stable signal worth penalizing a
    differently-shaped LLM decision for.
    """
    old_safety_required = old.safety_level is not SafetyLevel.NOT_REQUIRED
    return old.primary_module == new.primary_module and old_safety_required == new.safety_required


def _preview(text: str) -> str:
    text = text.strip()
    if len(text) <= _TASK_TEXT_PREVIEW_LEN:
        return text
    return text[: _TASK_TEXT_PREVIEW_LEN - 1].rstrip() + "…"


async def run_shadow_orchestration(
    *,
    task_text: str,
    old_decision: RouteDecision,
    workspace_id: int,
    provider: OrchestrationLLMProvider,
    state: FSMContext | None = None,
    business_profile: BusinessProfile | None = None,
    logger: ShadowComparisonLogger | None = None,
) -> ShadowComparisonRecord | None:
    """Best-effort; must never raise and must never be awaited before the
    user's own reply has been sent (see module docstring). Returns the
    record for tests' convenience; production callers can ignore it."""
    logger = logger or ShadowComparisonLogger()
    if not provider.is_configured:
        return None  # shadow mode has nothing to compare against yet

    started = time.monotonic()
    old_secondary = tuple(m.value for m in old_decision.secondary_modules)
    base_kwargs = dict(
        workspace_id=workspace_id,
        task_text_preview=_preview(task_text),
        old_primary=old_decision.primary_module.value,
        old_secondary=old_secondary,
        old_safety=old_decision.safety_level.value,
        provider=provider.name,
    )

    try:
        turns = await recent_turns(state)
        fsm_state = None
        if state is not None:
            try:
                fsm_state = await state.get_state()
            except Exception:
                fsm_state = None
        request = build_orchestration_request(
            task_text, turns=turns, business_profile=business_profile, fsm_state=fsm_state,
        )
        raw = await asyncio.to_thread(provider.classify, request=request)
        latency_ms = int((time.monotonic() - started) * 1000)

        if raw is None:
            record = ShadowComparisonRecord(
                **base_kwargs, status="error", latency_ms=latency_ms,
            )
            logger.log(record)
            return record

        try:
            decision = parse_orchestration_decision(raw)
        except InvalidOrchestrationDecisionError:
            record = ShadowComparisonRecord(
                **base_kwargs, status="invalid_output", latency_ms=latency_ms,
            )
            logger.log(record)
            return record

        record = ShadowComparisonRecord(
            **base_kwargs,
            status="ok",
            latency_ms=latency_ms,
            llm_intent=decision.intent.value,
            llm_primary=decision.primary_module.value,
            llm_secondary=tuple(m.value for m in decision.secondary_modules),
            llm_safety=decision.safety_required,
            llm_confidence=decision.confidence,
            llm_reason_code=decision.reason_code,
            agreement=compute_agreement(old_decision, decision),
        )
        logger.log(record)
        return record
    except Exception:
        # Fail-closed and silent by design: shadow mode must never surface
        # to the user or break the request that already succeeded via the
        # old router. Logged at debug, not warning - this path is expected
        # to fire often while providers are experimental.
        log.debug("orchestration_shadow: unexpected failure", exc_info=True)
        latency_ms = int((time.monotonic() - started) * 1000)
        try:
            record = ShadowComparisonRecord(**base_kwargs, status="error", latency_ms=latency_ms)
            logger.log(record)
            return record
        except Exception:
            return None
