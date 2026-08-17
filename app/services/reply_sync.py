"""Связывает свежий Reply-черновик с рабочей памятью — transport-independent.

Ровно то, что раньше делала app/handlers/tasks.py:_sync_reply_work_item, без
единственного отличия: здесь не строится и не импортируется
InlineKeyboardMarkup. Возвращается plain WorkItem (или None) — Telegram-
хендлер сам решает, какую клавиатуру под него показать (или показать ли
вообще). Будущий VK-хендлер сможет вызвать этот же сервис и построить свою
клавиатуру из того же WorkItem, не копируя правило «создать vs переиспользовать».
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.domain.work import WorkItem
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.work_repository import WorkRepository

log = logging.getLogger(__name__)

# Artifact для Reply-черновика: существующий, ничего не расширяющий kind
# (см. app/domain/content.py:ARTIFACT_TYPES) — «ответ клиенту» уже описан
# как client_message, отдельный material kind под этот сценарий не нужен.
_REPLY_ARTIFACT_TYPE = "client_message"


@dataclass(frozen=True)
class ReplyBridgeContext:
    """Что уже известно о собеседнике к моменту, когда Reply flow получил
    сообщение клиента — из подсказки субъекта (новый subject) или из
    daily_actions bridge (уже существующий work_item)."""

    work_item_id: int | None
    subject_id: int | None
    subject_name: str | None


class ReplyWorkSyncService:
    def __init__(
        self,
        work_repository: WorkRepository,
        artifact_repository: ArtifactRepository | None = None,
    ) -> None:
        self._work_repository = work_repository
        self._artifact_repository = artifact_repository

    async def sync(
        self, workspace_id: int, draft_text: str, context: ReplyBridgeContext,
    ) -> WorkItem | None:
        """- Уже существующий work_item (bridge) — переиспользуем как есть,
          второй не создаём: именно так due follow-up/active_dialog
          «продолжить разговор» возвращает пользователя к тому же work_item.
          Подготовка НОВОГО черновика обязана обновить updated_at и, для due
          follow-up, перевести loop_state в active_dialog («теперь ход
          пользователя») — этим занимается mark_draft_prepared.
        - Новый subject без work_item — создаём один open/active_dialog
          work_item (черновик подготовлен, но ещё не отправлен) и по
          возможности связываем его с сохранённым Artifact через
          ref_type='artifact'. Продолжения того же диалога повторно артефакт
          не создают — сознательное ограничение пилота, не пропуск.
        - Ни артефакт, ни работа с work_item не должны ронять Reply flow:
          любая ошибка здесь оставляет вызывающего с None, а не с
          исключением.
        """
        if context.work_item_id is not None:
            who = context.subject_name or "клиент"
            return await self._work_repository.mark_draft_prepared(
                workspace_id, context.work_item_id,
                next_step=f"Отправить подготовленный ответ: {who}",
            )

        if context.subject_id is None:
            return None

        ref_type, ref_id = await self._maybe_create_artifact(workspace_id, draft_text, context)
        who = context.subject_name or "клиент"
        try:
            return await self._work_repository.create_work_item(
                workspace_id,
                kind="dialog",
                subject_id=context.subject_id,
                loop_state="active_dialog",
                next_step=f"Отправить подготовленный ответ: {who}",
                ref_type=ref_type,
                ref_id=ref_id,
            )
        except Exception:
            log.warning("reply_sync: work_item creation failed")
            return None

    async def _maybe_create_artifact(
        self, workspace_id: int, draft_text: str, context: ReplyBridgeContext,
    ) -> tuple[str | None, int | None]:
        if self._artifact_repository is None:
            return None, None
        try:
            artifact, _ = await self._artifact_repository.create_artifact_with_initial_version(
                workspace_id,
                artifact_type=_REPLY_ARTIFACT_TYPE,
                title=f"Ответ: {context.subject_name or 'клиент'}",
                content=draft_text,
                generation_note="TRAVEL_ASSISTANT Reply flow",
            )
            return "artifact", artifact.id
        except Exception:
            log.warning("reply_sync: artifact persistence failed")
            return None, None
