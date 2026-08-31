"""Usage Cost & Subscription Foundation: a minimal, persisted ledger of AI
provider calls, keyed by workspace (billing unit) and Telegram user.

Token/cost fields are Optional by design: several call sites (Content
Factory's HTTP transport - see app/services/content_factory.py) do not
expose token usage in their responses at all today. A missing value here
means "not available from the provider", never a fabricated number - see
app/services/usage_pricing.py for the same rule applied to cost estimation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class UsageStatus(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"


@dataclass(frozen=True)
class LLMUsage:
    """Real token counts from a provider response. Only constructed when a
    provider actually returned this data - never estimated/guessed."""
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class UsageEvent:
    id: int
    occurred_at: str
    workspace_id: int
    telegram_user_id: int | None
    module: str
    provider: str
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    estimated_cost_usd: float | None
    status: UsageStatus


@dataclass(frozen=True)
class ModuleUsageBreakdown:
    module: str
    calls: int
    total_tokens: int | None
    estimated_cost_usd: float | None


@dataclass(frozen=True)
class WorkspaceUsageSummary:
    workspace_id: int
    total_calls: int
    successful_calls: int
    failed_calls: int
    calls_with_token_data: int
    total_tokens: int | None
    estimated_cost_usd: float | None
    by_module: tuple[ModuleUsageBreakdown, ...]
