from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from app.keyboards import BTN_HOW_IT_WORKS, main_menu

router = Router(name="start")


HELP_TEXT = (
    "Travel Advantage AI Control Center — закрытый бот-диспетчер.\n\n"
    "Что он делает:\n"
    "1. Принимает задачу кнопкой или обычным текстом.\n"
    "2. Определяет нужный модуль экосистемы.\n"
    "3. Отмечает, требуется ли проверка Safety Layer.\n"
    "4. Возвращает карточку маршрута и ручные шаги.\n\n"
    "Бот ничего не отправляет людям, не публикует посты, не бронирует "
    "и не принимает решения вместо человека."
)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Главное меню. Выберите кнопку или напишите задачу текстом.",
        reply_markup=main_menu(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT, reply_markup=main_menu())


@router.message(F.text == BTN_HOW_IT_WORKS)
async def how_it_works(message: Message) -> None:
    await message.answer(HELP_TEXT, reply_markup=main_menu())
