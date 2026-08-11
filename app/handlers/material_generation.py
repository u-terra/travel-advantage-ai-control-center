from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.filters import MagicData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from app.keyboards import (
    SOURCE_ACTION_ANALYZE_MORE,
    SOURCE_ACTION_MAIN_MENU,
    SOURCE_MATERIAL_FORMAT_PREFIX,
    SOURCE_MATERIAL_PREFIX,
    active_main_menu,
    material_result_keyboard,
    source_material_formats_keyboard,
    v2_back_keyboard,
)
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.partner_repository import PartnerRepository
from app.repositories.source_analysis_repository import SourceAnalysisRepository
from app.services.generation_request_builder import build_provider_generation_request
from app.services.material_orchestration import MaterialOrchestrationService
from app.services.llm.base import LLMProvider
from app.domain.partners import WorkspaceContext

router = Router(name="material_generation")
log = logging.getLogger(__name__)

SUPPORTED_FORMATS = {"telegram": "Telegram", "vk": "VK"}
_ARTIFACT_TYPE = "post"
_MODE = "ai"
_FAILURE = (
    "Не удалось создать материал. Исходный разбор сохранён — "
    "можно попробовать ещё раз позже."
)


def build_source_context(source, analysis, limit: int = 11_000) -> str:
    def items(values: tuple[str, ...]) -> str:
        return "\n".join(f"- {value}" for value in values) or "—"

    protected = (
        "СВЕДЕНИЯ, ТРЕБУЮЩИЕ ПРОВЕРКИ. Не использовать как подтверждённые "
        f"факты без проверки:\n{items(analysis.disputed_claims)}\n\n"
        f"ПРЕДУПРЕЖДЕНИЯ:\n{items(analysis.warnings)}"
    )
    analysis_text = (
        f"АНАЛИЗ:\nКратко: {analysis.summary}\n"
        f"Ключевые факты из анализа:\n{items(analysis.key_facts)}\n"
        f"Ценность для аудитории: {analysis.audience_value}\n"
        f"Целевые аудитории:\n{items(analysis.target_audiences)}\n"
        f"Идеи подачи:\n{items(analysis.content_angles)}"
    )
    prefix = "ИСХОДНЫЙ ТЕКСТ:\n"
    suffix = f"\n\n{analysis_text}\n\n{protected}"
    available = max(0, limit - len(prefix) - len(suffix))
    original = (source.original_text or "")[:available]
    if len(source.original_text or "") > available and available > 1:
        original = original[:-1].rstrip() + "…"
    return f"{prefix}{original}{suffix}"


async def _owned_source(
    source_id: int,
    workspace_context: WorkspaceContext | None,
    artifact_repository: ArtifactRepository,
    source_analysis_repository: SourceAnalysisRepository,
):
    if workspace_context is None:
        return None
    analysis = await source_analysis_repository.get_by_source_id(
        workspace_context.workspace_id, source_id
    )
    source = await artifact_repository.get_source(
        workspace_context.workspace_id, source_id
    )
    if analysis is None or source is None or analysis.source_id != source.id:
        return None
    return source, analysis


def _parse_source_id(data: str, prefix: str) -> int | None:
    raw = data.removeprefix(prefix)
    if not raw.isdigit():
        return None
    value = int(raw)
    return value if value > 0 else None


def _draft_messages(output_format: str, text: str, warnings: tuple[str, ...]) -> list[str]:
    heading = f"✍️ Черновик материала\n\nФормат: {SUPPORTED_FORMATS[output_format]}\n\n"
    footer_parts = ["Черновик требует ручной проверки перед использованием."]
    if warnings:
        footer_parts[0:0] = ["⚠️ Предупреждения:", *[f"— {w}" for w in warnings], ""]
    footer = "\n".join(footer_parts)
    chunks = [text[index:index + 3500] for index in range(0, len(text), 3500)] or [""]
    messages = [heading + chunks[0]]
    messages.extend(chunks[1:])
    if len(messages[-1]) + len(footer) + 2 <= 3900:
        messages[-1] += f"\n\n{footer}"
    else:
        messages.append(footer)
    return messages


@router.callback_query(MagicData(F.v2_menu_enabled), F.data.startswith(SOURCE_MATERIAL_PREFIX))
async def choose_material_format(
    callback: CallbackQuery,
    workspace_context: WorkspaceContext | None,
    artifact_repository: ArtifactRepository,
    source_analysis_repository: SourceAnalysisRepository,
) -> None:
    data = callback.data or ""
    source_id = _parse_source_id(data, SOURCE_MATERIAL_PREFIX)
    owned = None if source_id is None else await _owned_source(
        source_id, workspace_context, artifact_repository,
        source_analysis_repository,
    )
    if owned is None:
        await callback.answer("Источник недоступен.", show_alert=True)
        return
    await callback.answer()
    if callback.message is not None:
        await callback.message.answer(
            "Выбери формат материала.",
            reply_markup=source_material_formats_keyboard(source_id),
        )


@router.callback_query(MagicData(F.v2_menu_enabled), F.data.startswith(SOURCE_MATERIAL_FORMAT_PREFIX))
async def generate_source_material(
    callback: CallbackQuery,
    workspace_context: WorkspaceContext | None,
    artifact_repository: ArtifactRepository,
    source_analysis_repository: SourceAnalysisRepository,
    llm_provider: LLMProvider,
    partner_repository: PartnerRepository,
) -> None:
    data = (callback.data or "").removeprefix(SOURCE_MATERIAL_FORMAT_PREFIX)
    parts = data.split(":")
    if len(parts) != 2 or not parts[0].isdigit() or parts[1] not in SUPPORTED_FORMATS:
        await callback.answer("Формат или источник недоступен.", show_alert=True)
        return
    source_id, output_format = int(parts[0]), parts[1]
    owned = await _owned_source(
        source_id, workspace_context, artifact_repository,
        source_analysis_repository,
    )
    if owned is None:
        await callback.answer("Источник недоступен.", show_alert=True)
        return
    source, analysis = owned
    if callback.message is None:
        await callback.answer("Сообщение недоступно.", show_alert=True)
        return
    await callback.answer("Готовлю черновик…")
    profile = await partner_repository.get_business_profile(
        workspace_context.workspace_id
    )
    spec = MaterialOrchestrationService().build_generation_spec(
        workspace_context.workspace_id,
        source,
        analysis,
        profile,
        artifact_type=_ARTIFACT_TYPE,
        output_format=output_format,
    )
    request = build_provider_generation_request(spec)
    draft = await asyncio.to_thread(
        llm_provider.generate_draft,
        source_text=request.source_text,
        material_type=request.material_type,
        output_format=request.output_format,
        mode=_MODE,
    )
    if draft is None:
        await callback.message.answer(_FAILURE)
        return
    try:
        artifact, _ = await artifact_repository.create_artifact_with_initial_version(
            workspace_context.workspace_id,
            artifact_type=_ARTIFACT_TYPE,
            title=f"{SUPPORTED_FORMATS[output_format]}: {source.title}",
            content=draft.text,
            source_id=source.id,
            generation_note=(
                f"Content Factory: {request.material_type}/{request.output_format}"
            ),
        )
    except Exception:
        log.warning("material_generation: artifact persistence failed")
        await callback.message.answer(_FAILURE)
        return
    messages = _draft_messages(output_format, draft.text, draft.warnings)
    for index, text in enumerate(messages):
        reply_markup = material_result_keyboard(artifact.id) if index == len(messages) - 1 else None
        await callback.message.answer(text, reply_markup=reply_markup)


@router.callback_query(MagicData(F.v2_menu_enabled), F.data == SOURCE_ACTION_MAIN_MENU)
async def source_main_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    if callback.message is not None:
        await callback.message.answer(
            "Главное меню. Выберите нужную задачу.",
            reply_markup=active_main_menu(True),
        )


@router.callback_query(MagicData(F.v2_menu_enabled), F.data == SOURCE_ACTION_ANALYZE_MORE)
async def analyze_more(callback: CallbackQuery, state: FSMContext) -> None:
    from app.handlers.source_analysis import AnalyzeSource, _PROMPT

    await state.clear()
    await state.set_state(AnalyzeSource.waiting_for_text)
    await callback.answer()
    if callback.message is not None:
        await callback.message.answer(_PROMPT, reply_markup=v2_back_keyboard())
