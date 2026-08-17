"""«✅ Опубликовал» / «📌 Оставить на потом» под material_result_keyboard.

Единая точка для всех Content entry points (material_generation.py, menu.py
Radar, text_review.py, materials.py) — они все уже переиспользуют одну и ту
же material_result_keyboard, поэтому и обработчики этих кнопок здесь одни,
а не свои под каждый flow. Бизнес-правило "Artifact used ⇒ ровно один
completed content work_item" здесь не живёт — оно в
app/services/content_usage.py (ContentUsageService), transport-independent.

«✏️ Доработать» отдельной кнопкой не добавлена: существующая «🛡 Проверить
текст» (ARTIFACT_CHECK_PREFIX, app/handlers/text_review.py) уже ведёт в
единственный существующий flow доработки/сохранения новой версии — заводить
рядом вторую кнопку с тем же результатом означало бы либо дублировать
обработчик, либо создать параллельную систему, что явно запрещено.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import MagicData
from aiogram.types import CallbackQuery

from app.domain.partners import WorkspaceContext
from app.keyboards import ARTIFACT_KEEP_LATER_PREFIX, ARTIFACT_MARK_USED_PREFIX
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.work_repository import WorkRepository
from app.services.content_usage import ContentUsageService

router = Router(name="content_usage")

_NOT_FOUND = "Материал недоступен."
_ARCHIVED = "Материал в архиве — отметить как опубликованный нельзя."
_ALREADY_USED = "Уже отмечено как опубликованное."
_MARKED_USED = "✅ Отмечено как опубликованное."
_KEEP_LATER = "📌 Материал остаётся черновиком — вернёмся к нему в «Что делать сегодня»."


def _positive_id(data: str, prefix: str) -> int | None:
    raw = data.removeprefix(prefix)
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


@router.callback_query(
    MagicData(F.v2_menu_enabled), F.data.startswith(ARTIFACT_MARK_USED_PREFIX),
)
async def on_artifact_mark_used(
    callback: CallbackQuery,
    workspace_context: WorkspaceContext | None,
    artifact_repository: ArtifactRepository,
    work_repository: WorkRepository,
) -> None:
    artifact_id = _positive_id(callback.data or "", ARTIFACT_MARK_USED_PREFIX)
    if artifact_id is None or workspace_context is None:
        await callback.answer(_NOT_FOUND, show_alert=True)
        return

    result = await ContentUsageService(artifact_repository, work_repository).mark_used(
        workspace_context.workspace_id, artifact_id,
    )
    if result.outcome == "not_found":
        await callback.answer(_NOT_FOUND, show_alert=True)
        return
    if result.outcome == "archived":
        await callback.answer(_ARCHIVED, show_alert=True)
        return
    if result.outcome == "already_used":
        await callback.answer(_ALREADY_USED)
        return

    await callback.answer(_MARKED_USED)
    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(
    MagicData(F.v2_menu_enabled), F.data.startswith(ARTIFACT_KEEP_LATER_PREFIX),
)
async def on_artifact_keep_later(
    callback: CallbackQuery,
    workspace_context: WorkspaceContext | None,
    artifact_repository: ArtifactRepository,
) -> None:
    artifact_id = _positive_id(callback.data or "", ARTIFACT_KEEP_LATER_PREFIX)
    if artifact_id is None or workspace_context is None:
        await callback.answer(_NOT_FOUND, show_alert=True)
        return
    artifact = await artifact_repository.get_artifact(
        workspace_context.workspace_id, artifact_id,
    )
    if artifact is None:
        await callback.answer(_NOT_FOUND, show_alert=True)
        return
    await callback.answer(_KEEP_LATER)
