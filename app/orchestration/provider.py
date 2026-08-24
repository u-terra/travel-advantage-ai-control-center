"""Provider-agnostic contract for the LLM orchestration/routing classifier.

Separate from ``app.services.llm.base.LLMProvider`` on purpose: that
interface's three methods (generate_draft/check_text/analyze_source) are each
hard-wired to a specific Travel Content Factory HTTP endpoint with a fixed
response contract - none of them is a generic "classify this into a JSON
decision" call, and bolting routing onto analyze_source would mean running a
full source analysis on every free-text message just to get an intent label.

Same conventions as the existing provider: blocking method (caller runs it
via ``asyncio.to_thread``), ``None`` on any error/timeout, no vendor name
inside orchestration/business logic - see ``create_orchestration_llm_provider``
for the vendor-swap seam, mirroring ``app.services.llm.factory``.

Providers return the *raw*, already JSON-decoded object (or None on
transport failure). Turning that into a trusted ``OrchestrationDecision`` is
a separate, provider-independent step -
``app.orchestration.decision.parse_orchestration_decision`` - so structured-
output validation does not need to be reimplemented per vendor.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.orchestration.request import OrchestrationRequest


class OrchestrationLLMProvider(ABC):
    """Provider-agnostic contract for the shadow-mode intent classifier."""

    name: str = ""

    @property
    @abstractmethod
    def is_configured(self) -> bool:
        """True if this provider has everything needed to make calls."""

    @abstractmethod
    def classify(self, *, request: OrchestrationRequest) -> Any | None:
        """Returns the raw decoded JSON decision object, or None on any
        error (network, timeout, non-2xx, malformed JSON). Must not raise."""


class NullOrchestrationLLMProvider(OrchestrationLLMProvider):
    """Safe default when no orchestration LLM is configured yet.

    Phase 1 ships with this as the default: shadow mode is fully inert
    (``is_configured`` is False, so callers skip the shadow call entirely)
    until a real backend/adapter is wired in a later phase - see the Phase 1
    architecture report for why no live network adapter is included here.
    """

    name = "null"

    @property
    def is_configured(self) -> bool:
        return False

    def classify(self, *, request: OrchestrationRequest) -> Any | None:
        return None
