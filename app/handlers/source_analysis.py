from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.filters import MagicData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from app.cards import source_analysis_card
from app.keyboards import (
    BTN_V2_ANALYZE_LINK,
    BTN_V2_ANALYZE_MORE,
    BTN_V2_MAIN_MENU,
    source_analysis_result_keyboard,
    analyzed_source_keyboard,
    v2_back_keyboard,
)
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.source_analysis_repository import SourceAnalysisRepository
from app.services.llm.base import LLMProvider
from app.domain.partners import WorkspaceContext

router = Router(name="source_analysis")
log = logging.getLogger(__name__)


class AnalyzeSource(StatesGroup):
    waiting_for_text = State()
    processing = State()


_PROMPT = (
    "Пришли текст новости, поста или публикации, которую нужно разобрать.\n\n"
    "Я проанализирую вставленный текст и помогу подготовить материал на его основе."
)


@router.message(MagicData(F.v2_menu_enabled), AnalyzeSource.processing)
async def source_analysis_in_progress(message: Message) -> None:
    await message.answer("Анализ уже выполняется. Дождись результата.")


@router.message(
    MagicData(F.v2_menu_enabled),
    F.text.in_({BTN_V2_ANALYZE_LINK, BTN_V2_ANALYZE_MORE}),
)
async def start_source_analysis(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AnalyzeSource.waiting_for_text)
    await message.answer(_PROMPT, reply_markup=v2_back_keyboard())


@router.message(
    MagicData(F.v2_menu_enabled),
    AnalyzeSource.waiting_for_text,
)
async def receive_source_text(
    message: Message,
    state: FSMContext,
    workspace_context: WorkspaceContext | None,
    artifact_repository: ArtifactRepository,
    source_analysis_repository: SourceAnalysisRepository,
    llm_provider: LLMProvider,
) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пришли непустой текст для анализа.", reply_markup=v2_back_keyboard())
        return
    if len(text) > 12_000:
        await message.answer("Текст слишком длинный. Максимум — 12 000 символов.", reply_markup=v2_back_keyboard())
        return
    if workspace_context is None:
        await state.clear()
        await message.answer(
            "Рабочее пространство не найдено. Обратитесь к владельцу сервиса."
        )
        return
    await state.set_state(AnalyzeSource.processing)
    source = None
    analysis = None
    try:
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "Источник")
        title = first_line if len(first_line) <= 120 else first_line[:119].rstrip() + "…"
        source = await artifact_repository.create_source(
            workspace_context.workspace_id, source_type="text", original_url=None,
            original_text=text, title=title, status="new",
        )
        payload = await asyncio.to_thread(
            llm_provider.analyze_source, source_text=text
        )
        if payload is None:
            await message.answer(
                "Не удалось выполнить анализ. Текст сохранён, поэтому его можно будет обработать повторно позже.",
                reply_markup=source_analysis_result_keyboard(),
            )
            return
        analysis = await source_analysis_repository.save_successful_analysis(
            workspace_context.workspace_id, source.id, payload
        )
        # Классификация приходит вместе с разбором и показывается сразу.
        # В базе её нет: хранить род материала имеет смысл тогда, когда
        # появятся кнопки действий, а не раньше.
        card = source_analysis_card(analysis, classification=payload.classification)
        await message.answer(card, reply_markup=analyzed_source_keyboard(source.id))
    except Exception:
        log.warning("source_analysis: processing failed")
        if analysis is not None:
            text_error = "Анализ сохранён, но не удалось показать карточку."
        elif source is not None:
            text_error = "Не удалось выполнить анализ. Текст сохранён, поэтому его можно будет обработать повторно позже."
        else:
            text_error = "Не удалось начать анализ. Попробуйте ещё раз позже."
        await message.answer(text_error, reply_markup=source_analysis_result_keyboard())
    finally:
        await state.clear()
