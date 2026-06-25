from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

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
from app.storage import Journal

router = Router(name="menu")


class AwaitTask(StatesGroup):
    waiting = State()


BUTTON_TO_MODULE: dict[str, Module] = {
    BTN_CREATE_CONTENT: Module.CONTENT_FACTORY,
    BTN_CLIENT_QUESTION: Module.TRAVEL_ASSISTANT,
    BTN_FIND_SIGNALS: Module.LEAD_RADAR,
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
    BTN_FIND_SIGNALS: (
        "Опишите, какие сигналы интереса нужно поискать. Примеры:\n"
        "— Темы, где люди обсуждают отдых в Турции.\n"
        "— Идеи для постов по реальному спросу.\n"
        "— Сигналы интереса к Life Experiences."
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
