"""«Что делать сегодня»: transport-independent сборка DailyActions.

Собирает данные (work items, waiting, профиль, черновики, сигналы) и
вызывает уже существующий NextBestActionService — сам не открывает aiogram,
не знает про Message/CallbackQuery/keyboards. Telegram-хендлер
(app/handlers/daily_actions.py) только вызывает build() и рендерит
результат; будущий VK-хендлер сможет получить тот же DailyActions без
копирования этой сборки.

Ranking и деривация кандидатов не менялись — это по-прежнему целиком работа
NextBestActionService, сюда перенесена только сборка входных данных для него.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.domain.content import Artifact
from app.domain.work import DailyActions, SignalOpportunity
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.partner_repository import PartnerRepository
from app.repositories.work_repository import WorkRepository
from app.repositories.workspace_signal_repository import WorkspaceSignalRepository
from app.services.next_best_action import NextBestActionService

log = logging.getLogger(__name__)

_SIGNAL_SCORE_THRESHOLD = 60.0
_DRAFT_STATUSES = ("draft", "review_required")
_DRAFT_FETCH_LIMIT = 5
_SIGNAL_FETCH_LIMIT = 20


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DailyActionsService:
    def __init__(
        self,
        work_repository: WorkRepository,
        partner_repository: PartnerRepository,
        artifact_repository: ArtifactRepository,
        workspace_signal_repository: WorkspaceSignalRepository | None = None,
    ) -> None:
        self._work_repository = work_repository
        self._partner_repository = partner_repository
        self._artifact_repository = artifact_repository
        self._workspace_signal_repository = workspace_signal_repository

    async def build(self, workspace_id: int, *, now: str | None = None) -> DailyActions:
        now = now or _now()

        profile = await self._partner_repository.get_business_profile(workspace_id)
        ta_affiliated = profile is not None and profile.ta_affiliated
        profile_incomplete = profile is None or profile.profile_status == "incomplete"

        open_items = await self._work_repository.list_open_actionable(workspace_id, now=now)
        waiting_items = await self._work_repository.list_waiting_not_due(workspace_id, now=now)
        draft_artifacts = await self._fetch_draft_artifacts(workspace_id)
        signal_candidates = await self._fetch_signal_candidates(workspace_id)

        return NextBestActionService().build(
            workspace_id=workspace_id,
            ta_affiliated=ta_affiliated,
            open_work_items=open_items,
            waiting_not_due=waiting_items,
            profile_incomplete=profile_incomplete,
            draft_artifacts=draft_artifacts,
            signal_candidates=signal_candidates,
        )

    async def _fetch_draft_artifacts(self, workspace_id: int) -> list[Artifact]:
        drafts: list[Artifact] = []
        for status in _DRAFT_STATUSES:
            drafts.extend(await self._artifact_repository.list_artifacts(
                workspace_id, limit=_DRAFT_FETCH_LIMIT, status=status,
            ))
        return drafts

    async def _fetch_signal_candidates(self, workspace_id: int) -> list[SignalOpportunity]:
        """Только уже авторизованные workspace-сигналы с известным заголовком
        и высоким ai_score. Никакого вызова recommender/LLM здесь нет — это
        сознательно самый простой и безопасный срез derived-кандидатов."""
        if self._workspace_signal_repository is None:
            return []
        try:
            records = await self._workspace_signal_repository.list_for_workspace(
                workspace_id, limit=_SIGNAL_FETCH_LIMIT,
            )
        except Exception:
            log.warning("daily_actions_service: failed to fetch workspace signals")
            return []

        candidates: list[SignalOpportunity] = []
        for record in records:
            if record.status != "new":
                continue
            if record.ai_score is None or record.ai_score < _SIGNAL_SCORE_THRESHOLD:
                continue
            if not record.item_title:
                continue
            candidates.append(SignalOpportunity(
                signal_interpretation_id=record.interpretation_id,
                title=record.item_title,
                reason=record.ai_reason or record.item_summary or "Новый сигнал интереса.",
            ))
        return candidates
