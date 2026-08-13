from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.filters import MagicData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from app.domain.partners import WorkspaceContext
from app.keyboards import (
    BTN_CHECK_TEXT,
    BTN_CLIENT_QUESTION,
    BTN_CREATE_CONTENT,
    BTN_FIND_SIGNALS,
    BTN_LAST_TASK,
    BTN_PACKAGE_MATERIALS,
    BTN_UNSURE,
    BTN_WEB_RESOURCES,
    BTN_V2_CHECK_TEXT,
    BTN_V2_CLIENT_REPLY,
    BTN_V2_HELP,
    BTN_V2_CREATE_MATERIAL,
    BTN_V2_ANALYZE_LINK,
    BTN_V2_FIND_SIGNALS,
    BTN_V2_MAIN_MENU,
    CATEGORY_BUTTONS,
    V2_CATEGORY_BUTTONS,
    V2_PLACEHOLDER_BUTTONS,
    WEB_RESOURCES_BACK,
    active_main_menu,
    main_menu,
    material_result_keyboard,
    v2_back_keyboard,
    web_resources_keyboard,
)
from app.routing.modules import Module
from app.routing.safety import SafetyLevel
from app.services.lead_radar import (
    LeadRadarConfig,
    build_summary,
    build_workspace_signals,
    route_card,
    unavailable_summary,
)
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.partner_repository import PartnerRepository
from app.repositories.workspace_signal_repository import WorkspaceSignalRepository
from app.services.generation_request_builder import build_provider_generation_request
from app.services.llm.base import LLMProvider
from app.services.material_orchestration import MaterialOrchestrationService
from app.storage import Journal

router = Router(name="menu")
log = logging.getLogger(__name__)

_RADAR_CONTENT_PREFIX = "radar_content:"
_RADAR_CONTENT_LIMIT = 3
_DRAFT_MODE = "ai"
_RADAR_ARTIFACT_FAILURE = (
    "⚠️ Материал создан, но сохранить его в «Мои материалы» не удалось. "
    "Скопируйте текст ниже, чтобы не потерять его."
)


class AwaitTask(StatesGroup):
    waiting = State()


BUTTON_TO_MODULE: dict[str, Module] = {
    BTN_CREATE_CONTENT: Module.CONTENT_FACTORY,
    BTN_CLIENT_QUESTION: Module.TRAVEL_ASSISTANT,
    BTN_CHECK_TEXT: Module.SAFETY_LAYER,
    BTN_PACKAGE_MATERIALS: Module.PARTNER_PACKAGING,
    BTN_V2_CLIENT_REPLY: Module.TRAVEL_ASSISTANT,
}


BUTTON_HINTS: dict[str, str] = {
    BTN_CREATE_CONTENT: (
        "Опишите задачу для контента. Примеры:\n"
        "— Нужен Telegram-пост о сомнениях перед поездкой.\n"
        "— Сделай сценарий Reels о сравнении вариантов.\n"
        "— Напиши ответ на возражение «Это сетевой маркетинг?»."
    ),
    BTN_CLIENT_QUESTION: (
        "Опишите вопрос клиента. Примеры:\n"
        "— Чем Travel Advantage отличается от обычного поиска отелей?\n"
        "— Можно ли оплатить бронирование из России?\n"
        "— Какие есть варианты тарифа?"
    ),
    BTN_CHECK_TEXT: (
        "Пришлите текст, который нужно проверить перед публикацией или отправкой человеку."
    ),
    BTN_PACKAGE_MATERIALS: (
        "Опишите, какие материалы нужно подготовить для партнёра. Примеры:\n"
        "— Инструкция по Travel Content Factory.\n"
        "— Коммерческое предложение по настройке AI-инструмента.\n"
        "— Презентация продукта для нового партнёра."
    ),
    BTN_V2_CLIENT_REPLY: (
        "Опишите вопрос клиента или сообщение, на которое нужно подготовить ответ."
    ),
    BTN_UNSURE: (
        "Опишите задачу обычным языком — даже если она смешанная. "
        "Бот попробует разложить её на отдельные маршруты."
    ),
}


def _short_title(text: str, max_len: int = 42) -> str:
    value = (text or "").strip() or "Идея без заголовка"
    if len(value) <= max_len:
        return value
    return value[: max_len - 1].rstrip() + "…"


def _radar_content_keyboard(
    ideas: list[dict[str, str]],
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"📝 {index}. {_short_title(idea['title'])}",
                    callback_data=f"{_RADAR_CONTENT_PREFIX}{idea['interpretation_id']}",
                )
            ]
            for index, idea in enumerate(ideas, start=1)
        ]
    )


def _radar_content_ideas(signals) -> list[dict[str, str]]:
    return [
        {
            "interpretation_id": str(signal.id),
            "title": signal.title,
            "reason": signal.action_reason,
            "url": signal.url,
        }
        for signal in signals
        if signal.recommended_action == "content"
    ][:_RADAR_CONTENT_LIMIT]


@router.message(F.text.in_(CATEGORY_BUTTONS))
async def on_category(message: Message, state: FSMContext) -> None:
    module = BUTTON_TO_MODULE[message.text]
    await state.update_data(forced_module=module.value)
    await state.set_state(AwaitTask.waiting)
    await message.answer(BUTTON_HINTS[message.text], reply_markup=main_menu())


@router.message(MagicData(F.v2_menu_enabled), F.text.in_(V2_CATEGORY_BUTTONS))
async def on_v2_category(message: Message, state: FSMContext) -> None:
    module = BUTTON_TO_MODULE[message.text]
    await state.update_data(forced_module=module.value)
    await state.set_state(AwaitTask.waiting)
    await message.answer(
        BUTTON_HINTS[message.text],
        reply_markup=active_main_menu(True),
    )

@router.message(MagicData(F.v2_menu_enabled), F.text == BTN_V2_MAIN_MENU)
async def on_v2_main_menu(
    message: Message, state: FSMContext
) -> None:
    await state.clear()
    await message.answer(
        "Главное меню. Выберите нужную задачу.",
        reply_markup=active_main_menu(True),
    )


@router.message(MagicData(F.v2_menu_enabled), F.text == BTN_V2_HELP)
async def on_v2_help(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Здесь можно выбрать задачу: подготовить ответ клиенту, проверить текст "
        "или открыть разделы для работы с материалами.",
        reply_markup=v2_back_keyboard(),
    )


@router.message(MagicData(F.v2_menu_enabled), F.text.in_(V2_PLACEHOLDER_BUTTONS))
async def on_v2_placeholder(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Раздел подготовлен в новой структуре. Рабочий сценарий будет подключён "
        "на следующем этапе.",
        reply_markup=v2_back_keyboard(),
    )


@router.message(MagicData(F.v2_menu_enabled), F.text == BTN_V2_CREATE_MATERIAL)
async def on_v2_create_material(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Сначала разберите источник. После анализа можно создать материал на его основе.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=BTN_V2_ANALYZE_LINK)],
                      [KeyboardButton(text=BTN_V2_MAIN_MENU)]],
            resize_keyboard=True,
        ),
    )


@router.message(F.text == BTN_UNSURE)
async def on_unsure(message: Message, state: FSMContext) -> None:
    await state.update_data(forced_module=None)
    await state.set_state(AwaitTask.waiting)
    await message.answer(BUTTON_HINTS[BTN_UNSURE], reply_markup=main_menu())


@router.message(F.text == BTN_WEB_RESOURCES)
async def on_web_resources(message: Message) -> None:
    await message.answer(
        "🌐 Веб-ресурсы Travel-экосистемы. Откройте нужный сайт в браузере.",
        reply_markup=web_resources_keyboard(),
    )


@router.callback_query(F.data == WEB_RESOURCES_BACK)
async def on_web_resources_back(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            "Главное меню. Выберите кнопку или напишите задачу текстом.",
            reply_markup=main_menu(),
        )


@router.message(F.text == BTN_FIND_SIGNALS)
@router.message(MagicData(F.v2_menu_enabled), F.text == BTN_V2_FIND_SIGNALS)
async def on_find_signals(
    message: Message,
    state: FSMContext,
    lead_radar_config: LeadRadarConfig,
    workspace_signal_repository: WorkspaceSignalRepository,
    workspace_context: WorkspaceContext | None,
    v2_menu_enabled: bool = False,
) -> None:
    await state.clear()
    if workspace_context is None:
        await message.answer(
            "Рабочее пространство недоступно.",
            reply_markup=active_main_menu(v2_menu_enabled),
        )
        return
    await message.answer(route_card(), reply_markup=active_main_menu(v2_menu_enabled))
    await workspace_signal_repository.sync_eligible()
    records = await workspace_signal_repository.list_for_workspace(
        workspace_context.workspace_id, limit=200
    )
    signals = build_workspace_signals(lead_radar_config, records, limit=5)
    if signals is None:
        await message.answer(unavailable_summary())
        return

    await message.answer(build_summary(signals), disable_web_page_preview=True)

    ideas = _radar_content_ideas(signals)
    if not ideas:
        return

    await message.answer(
        "💡 Выберите идею, чтобы подготовить черновик Telegram-поста. "
        "Ничего не публикуется автоматически.",
        reply_markup=_radar_content_keyboard(ideas),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith(_RADAR_CONTENT_PREFIX))
async def on_radar_content_selected(
    callback: CallbackQuery,
    state: FSMContext,
    journal: Journal,
    llm_provider: LLMProvider,
    workspace_context: WorkspaceContext | None,
    workspace_signal_repository: WorkspaceSignalRepository,
    lead_radar_config: LeadRadarConfig,
    partner_repository: PartnerRepository,
    artifact_repository: ArtifactRepository,
) -> None:
    raw_data = callback.data or ""
    try:
        interpretation_id = int(raw_data.removeprefix(_RADAR_CONTENT_PREFIX))
    except ValueError:
        await callback.answer(
            "Не удалось определить выбранную идею.",
            show_alert=True,
        )
        return

    if workspace_context is None:
        await callback.answer("Рабочее пространство недоступно.", show_alert=True)
        return

    record = await workspace_signal_repository.get_for_workspace(
        workspace_context.workspace_id, interpretation_id
    )
    if record is None:
        await callback.answer("Сигнал недоступен.", show_alert=True)
        return
    authorized = build_workspace_signals(lead_radar_config, [record], limit=1)
    if not authorized:
        await callback.answer("Сигнал недоступен.", show_alert=True)
        return
    signal = authorized[0]

    profile = await partner_repository.get_business_profile(
        workspace_context.workspace_id
    )
    spec = MaterialOrchestrationService().build_radar_generation_spec(
        workspace_context.workspace_id,
        profile,
        title=record.item_title,
        summary=record.item_summary,
        source_type=record.source_type,
        origin_type=record.origin_type,
        url=record.item_url,
        category=record.ai_category or "",
        reason=record.ai_reason or signal.action_reason,
    )
    request = build_provider_generation_request(spec)
    task_text = f"Radar draft: {signal.title}"

    await callback.answer("Готовлю черновик…")
    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            "🧩 Маршрут: Travel Lead Radar → Travel Content Factory → "
            "ручная проверка."
        )

    await journal.add(
        workspace_context.workspace_id,
        task_text=task_text,
        primary_module=Module.CONTENT_FACTORY.value,
        secondary_modules=(),
        safety_level=SafetyLevel.NOT_REQUIRED.value,
    )

    draft = await asyncio.to_thread(
        llm_provider.generate_draft,
        source_text=request.source_text,
        material_type=request.material_type,
        output_format=request.output_format,
        mode=_DRAFT_MODE,
    )

    if callback.message is None:
        return

    if draft is None:
        await callback.message.answer(
            "Не удалось получить черновик автоматически. "
            "Можно открыть Travel Content Factory вручную."
        )
        return

    lines: list[str] = [
        "📝 Черновик по идее из Radar — для ручной проверки",
        "",
        draft.text,
    ]
    if draft.warnings:
        lines.append("")
        lines.append("⚠️ Предупреждения Content Factory:")
        for warning in draft.warnings:
            lines.append(f"— {warning}")
        lines.append("")
        lines.append("Текст требует ручной проверки перед публикацией.")
    draft_text = "\n".join(lines)

    try:
        artifact, _ = await artifact_repository.create_artifact_with_initial_version(
            workspace_context.workspace_id,
            artifact_type=spec.artifact_type,
            title=f"Telegram: {signal.title}",
            content=draft.text,
            generation_note=f"Lead Radar: {request.material_type}/{request.output_format}",
        )
    except Exception:
        log.warning("menu: radar artifact persistence failed")
        await callback.message.answer(_RADAR_ARTIFACT_FAILURE)
        await callback.message.answer(draft_text, reply_markup=v2_back_keyboard())
        return

    await callback.message.answer(
        draft_text, reply_markup=material_result_keyboard(artifact.id)
    )


@router.message(F.text == BTN_LAST_TASK)
async def on_last_task(
    message: Message,
    journal: Journal,
    workspace_context: WorkspaceContext | None,
) -> None:
    if workspace_context is None:
        await message.answer(
            "Рабочее пространство недоступно.", reply_markup=main_menu()
        )
        return
    entry = await journal.last(workspace_context.workspace_id)
    if entry is None:
        await message.answer(
            "Журнал пуст. Поставьте задачу через меню или текстом.",
            reply_markup=main_menu(),
        )
        return
    text = (
        "📋 Последняя задача\n\n"
        f"Дата (UTC): {entry.created_at}\n"
        f"Задача: {entry.task_text}\n"
        f"Основной модуль: {entry.primary_module}\n"
        f"Дополнительный модуль: {entry.secondary_modules or 'не требуется'}\n"
        f"Safety Layer: {entry.safety_level}\n"
        f"Статус: {entry.status}\n"
        f"Заметка: {entry.note or '—'}"
    )
    await message.answer(text, reply_markup=main_menu())
