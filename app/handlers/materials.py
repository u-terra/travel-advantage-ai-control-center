"""Раздел «Мои материалы»: список последних Artifact workspace и просмотр текущей версии.

Только список и открытие. Поиск, фильтры, теги, пагинация и архив сюда
намеренно не входят — библиотека растёт по мере необходимости отдельными
этапами.
"""

from __future__ import annotations

from datetime import datetime

from aiogram import F, Router
from aiogram.filters import MagicData
from aiogram.types import CallbackQuery, Message

from app.domain.content import Artifact
from app.domain.partners import WorkspaceContext
from app.keyboards import (
    ARTIFACT_OPEN_PREFIX,
    BTN_V2_MATERIALS,
    material_result_keyboard,
    materials_list_keyboard,
    v2_back_keyboard,
)
from app.repositories.artifact_repository import ArtifactRepository

router = Router(name="materials")

_LIST_LIMIT = 10
_CHUNK_SIZE = 3500

_UNAVAILABLE = "Рабочее пространство недоступно."
_EMPTY = "Сохранённых материалов пока нет."
_NOT_FOUND = "Материал недоступен."

_ARTIFACT_TYPE_LABELS: dict[str, str] = {
    "post": "Пост",
    "video_script": "Сценарий видео",
    "stories": "Stories",
    "client_message": "Ответ клиенту",
    "objection_reply": "Ответ на возражение",
    "faq": "FAQ",
    "content_plan_item": "Пункт контент-плана",
    "other": "Материал",
}


def _type_label(artifact_type: str) -> str:
    return _ARTIFACT_TYPE_LABELS.get(artifact_type, artifact_type)


def _short_title(text: str, max_len: int = 40) -> str:
    value = (text or "").strip() or "Без названия"
    if len(value) <= max_len:
        return value
    return value[: max_len - 1].rstrip() + "…"


def _short_date(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y")
    except ValueError:
        return value


def _list_label(artifact: Artifact) -> str:
    return (
        f"{_type_label(artifact.artifact_type)} · "
        f"{_short_title(artifact.title)} · {_short_date(artifact.created_at)}"
    )


def _positive_id(data: str, prefix: str) -> int | None:
    raw = data.removeprefix(prefix)
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


def _chunks(text: str, size: int = _CHUNK_SIZE) -> list[str]:
    return [text[index:index + size] for index in range(0, len(text), size)] or [""]


@router.message(MagicData(F.v2_menu_enabled), F.text == BTN_V2_MATERIALS)
async def show_materials(
    message: Message,
    workspace_context: WorkspaceContext | None,
    artifact_repository: ArtifactRepository,
) -> None:
    if workspace_context is None:
        await message.answer(_UNAVAILABLE, reply_markup=v2_back_keyboard())
        return
    artifacts = await artifact_repository.list_artifacts(
        workspace_context.workspace_id, limit=_LIST_LIMIT
    )
    if not artifacts:
        await message.answer(_EMPTY, reply_markup=v2_back_keyboard())
        return
    items = tuple((artifact.id, _list_label(artifact)) for artifact in artifacts)
    await message.answer(
        "📚 Ваши последние материалы. Откройте, чтобы посмотреть текст.",
        reply_markup=materials_list_keyboard(items),
    )


@router.callback_query(
    MagicData(F.v2_menu_enabled), F.data.startswith(ARTIFACT_OPEN_PREFIX)
)
async def open_material(
    callback: CallbackQuery,
    workspace_context: WorkspaceContext | None,
    artifact_repository: ArtifactRepository,
) -> None:
    artifact_id = _positive_id(callback.data or "", ARTIFACT_OPEN_PREFIX)
    if artifact_id is None or workspace_context is None:
        await callback.answer(_NOT_FOUND, show_alert=True)
        return

    # Строго через workspace_context.workspace_id: get_artifact и
    # get_current_artifact_version фильтруют по workspace_id в самом SQL,
    # поэтому чужой artifact_id для этого workspace вернёт None.
    artifact = await artifact_repository.get_artifact(
        workspace_context.workspace_id, artifact_id
    )
    version = None if artifact is None else await artifact_repository.get_current_artifact_version(
        workspace_context.workspace_id, artifact_id
    )
    if artifact is None or version is None:
        await callback.answer(_NOT_FOUND, show_alert=True)
        return
    if callback.message is None:
        await callback.answer("Сообщение недоступно.", show_alert=True)
        return

    await callback.answer()
    heading = f"{_type_label(artifact.artifact_type)}: {artifact.title}\n\n"
    messages = _chunks(heading + version.content)
    for index, text in enumerate(messages):
        reply_markup = (
            material_result_keyboard(artifact.id)
            if index == len(messages) - 1
            else None
        )
        await callback.message.answer(text, reply_markup=reply_markup)
