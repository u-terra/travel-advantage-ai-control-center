"""Beta Control Center telemetry - a minimal, append-heavy technical event
log, deliberately narrow in scope: "did this key beta path succeed, how
long did it take, and if it failed, why (in a SAFE, pre-summarized way)".

This is NOT a logging/observability replacement (no Grafana/Prometheus/ELK
- see the task notes) and NOT a conversation store. Only the module
recording an event decides what safe_message/metadata says - the schema
itself has no room for a password, token, signature, raw prompt, or full
conversation text; callers must never put one in metadata either (see
app.services.telemetry.record_event's docstring for the actual guardrail).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class EventSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass(frozen=True)
class OperationalEvent:
    id: int
    occurred_at: str
    workspace_id: int | None
    telegram_user_id: int | None
    web_user_id: int | None
    module: str
    event_type: str
    severity: EventSeverity
    success: bool
    latency_ms: int | None
    request_id: str | None
    error_code: str | None
    safe_message: str | None
    metadata_json: str | None


@dataclass(frozen=True)
class ErrorGroup:
    """One row of /admin/errors's grouping - "this failed N times across M
    workspaces", not a raw event list."""
    module: str
    event_type: str
    error_code: str | None
    severity: EventSeverity
    occurrences: int
    workspace_count: int
    last_occurred_at: str
    sample_safe_message: str | None
    sample_request_id: str | None
