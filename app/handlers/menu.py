from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.keyboards import (
    BTN_CHECK_TEXT,
    BTN_CLIENT_QUESTION,
    BTN_CREATE_CONTENT,
    BTN_FIND_SIGNALS,
    BTN_LAST_TASK,
    BTN_PACKAGE_MATERIALS,
    BTN_UNSURE,
    CATEGORY_BUTTONS,
    main_menu,
)
from app.routing.modules import Module
from app.routing.safety import SafetyLevel
from app.services.content_factory import (
    ContentFactoryConfig,
    generate_draft_sync,
)
from app.services.lead_radar import (
    LeadRadarConfig,
    build_summary,
    fetch_signals_sync,
    route_card,
    unavailable_summary,
)
from app.storage import Journal

router = Router(name="menu")

_RADAR_CONTENT_PREFIX = "radar_content:"
_RADAR_CONTENT_LIMIT = 3
_DRAFT_MATERIAL_TYPE = "market_offer"
_DRAFT_OUTPUT_FORMAT = "telegram"
_DRAFT_MODE = "ai"


class AwaitTask(StatesGroup):
    waiting = State()


BUTTON_TO_MODULE: dict[str, Module] = {
    BTN_CREATE_CONTENT: Module.CONTENT_FACTORY,
    BTN_CLIENT_QUESTION: Module.TRAVEL_ASSISTANT,
    BTN_CHECK_TEXT: Module.SAFETY_LAYER,
    BTN_PACKAGE_MATERIALS: Module.PARTNER_PACKAGING,
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
                    callback_data=f"{_RADAR_CONTENT_PREFIX}{index - 1}",
                )
            ]
            for index, idea in enumerate(ideas, start=1)
        ]
    )


def _build_radar_content_task(idea: dict[str, str]) -> str:
    title = idea.get("title") or "Тема из Travel Lead Radar"
    reason = idea.get("reason") or "Подходит как информационная тема."
    url = idea.get("url") or "—"
    return (
        "Нужен спокойный Telegram-пост для Travel Advantage по теме из "
        "Travel Lead Radar.\n"
        f"Тема: {title}\n"
        f"Почему тема выбрана: {reason}\n"
        f"Источник идеи: {url}\n\n"
        "Сделай полезный информационный пост без обещаний дохода, "
        "окупаемости или гарантированных скидок. Не утверждай, что "
        "формат подходит всем. Избегай фразы «без давления»."
    )


def _radar_content_ideas(signals) -> list[dict[str, str]]:
    return [
        {
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


@router.message(F.text == BTN_UNSURE)
async def on_unsure(message: Message, state: FSMContext) -> None:
    await state.update_data(forced_module=None)
    await state.set_state(AwaitTask.waiting)
    await message.answer(BUTTON_HINTS[BTN_UNSURE], reply_markup=main_menu())


@router.message(F.text == BTN_FIND_SIGNALS)
async def on_find_signals(
    message: Message,
    state: FSMContext,
    lead_radar_config: LeadRadarConfig,
) -> None:
    await state.clear()
    await message.answer(route_card(), reply_markup=main_menu())
    signals = await asyncio.to_thread(fetch_signals_sync, lead_radar_config, limit=5)
    if signals is None:
        await message.answer(unavailable_summary())
        return

    await message.answer(build_summary(signals), disable_web_page_preview=True)

    ideas = _radar_content_ideas(signals)
    if not ideas:
        return

    await state.update_data(radar_content_ideas=ideas)
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
    content_factory_config: ContentFactoryConfig,
) -> None:
    raw_data = callback.data or ""
    try:
        index = int(raw_data.removeprefix(_RADAR_CONTENT_PREFIX))
    except ValueError:
        await callback.answer(
            "Не удалось определить выбранную идею.",
            show_alert=True,
        )
        return

    state_data = await state.get_data()
    ideas = state_data.get("radar_content_ideas") or []
    if not isinstance(ideas, list) or index < 0 or index >= len(ideas):
        await callback.answer(
            "Выбор уже устарел. Нажмите «📡 Найти сигналы интереса» ещё раз.",
            show_alert=True,
        )
        return

    idea = ideas[index]
    if not isinstance(idea, dict):
        await callback.answer(
            "Не удалось прочитать выбранную идею.",
            show_alert=True,
        )
        return

    task_text = _build_radar_content_task(idea)
    await state.update_data(radar_content_ideas=[])

    await callback.answer("Готовлю черновик…")
    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            "🧩 Маршрут: Travel Lead Radar → Travel Content Factory → "
            "ручная проверка."
        )

    await journal.add(
        task_text=task_text,
        primary_module=Module.CONTENT_FACTORY.value,
        secondary_modules=(),
        safety_level=SafetyLevel.NOT_REQUIRED.value,
    )

    draft = await asyncio.to_thread(
        generate_draft_sync,
        content_factory_config,
        source_text=task_text,
        material_type=_DRAFT_MATERIAL_TYPE,
        output_format=_DRAFT_OUTPUT_FORMAT,
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

    await callback.message.answer("\n".join(lines))


@router.message(F.text == BTN_LAST_TASK)
async def on_last_task(message: Message, journal: Journal) -> None:
    entry = await journal.last()
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
