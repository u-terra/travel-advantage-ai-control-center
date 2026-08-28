"""Minimal runtime context for Planner executors.

Deliberately narrow: only the services/repositories individual executors in
``app.planner.executors`` actually call, not "the whole app" (no Bot,
Dispatcher, FSMContext, or Journal here - see the module docstring in
``app.planner`` for what Phase 2 is and is not responsible for).

Every field except ``workspace_id`` is optional. An executor whose required
field is ``None`` raises a controlled ``PlannerExecutionError`` (see
``app.planner.executors._require_context_service``) - it never crashes on a
``None`` attribute access.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.planner.cost import LLMCallBudget
from app.repositories.competitor_repository import CompetitorRepository
from app.repositories.partner_repository import PartnerRepository
from app.services.daily_actions import DailyActionsService
from app.services.lead_radar import LeadRadarConfig
from app.services.llm.base import LLMProvider


@dataclass(frozen=True)
class PlannerExecutionContext:
    workspace_id: int
    llm_provider: LLMProvider | None = None
    competitor_repository: CompetitorRepository | None = None
    partner_repository: PartnerRepository | None = None
    lead_radar_config: LeadRadarConfig | None = None
    daily_actions_service: DailyActionsService | None = None
    # Cost control (Phase 3): shared, mutable per-run counter - see
    # app.planner.cost. None means "no budget enforced", which every
    # existing Stage 2/2.1 test relies on (they predate cost control and
    # construct contexts without it); app.planner.service always attaches a
    # fresh budget for real Planner runs.
    llm_call_budget: LLMCallBudget | None = None
