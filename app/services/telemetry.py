"""Single call-site helper for recording an operational_events row - same
best-effort convention as app.services.usage_recorder.record_llm_call: a
telemetry failure must never break the user-facing flow it's
instrumenting.

Guardrail for callers: metadata is for SAFE TECHNICAL fields only -
counts, provider/model names, status codes, module names, boolean flags.
NEVER pass user-authored text (prompts, messages, feedback comments),
credentials, tokens/signatures, or file paths/contents. safe_message is
the same rule: a short, human-written technical summary you compose
yourself (e.g. "content_factory timeout"), never an f-string embedding
raw exception text that might contain user input or a URL with a token in
it.
"""

from __future__ import annotations

import logging

from app.domain.telemetry import EventSeverity
from app.repositories.operational_event_repository import OperationalEventRepository

log = logging.getLogger(__name__)


async def record_event(
    repository: OperationalEventRepository | None,
    *,
    module: str,
    event_type: str,
    success: bool,
    workspace_id: int | None = None,
    telegram_user_id: int | None = None,
    web_user_id: int | None = None,
    severity: EventSeverity = EventSeverity.INFO,
    latency_ms: int | None = None,
    request_id: str | None = None,
    error_code: str | None = None,
    safe_message: str | None = None,
    metadata: dict[str, object] | None = None,
) -> None:
    if repository is None:
        return
    try:
        await repository.record(
            module=module, event_type=event_type, success=success,
            workspace_id=workspace_id, telegram_user_id=telegram_user_id,
            web_user_id=web_user_id, severity=severity, latency_ms=latency_ms,
            request_id=request_id, error_code=error_code, safe_message=safe_message,
            metadata=metadata,
        )
    except Exception:
        log.warning("telemetry: failed to persist operational event", exc_info=True)
