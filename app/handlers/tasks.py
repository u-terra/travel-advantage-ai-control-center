from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.cards import build_card
from app.handlers.menu import AwaitTask
from app.keyboards import main_menu
from app.routing.modules import Module
from app.routing.router import route_for_button, route_text
from app.storage import Journal

router = Router(name="tasks")


@router.message(AwaitTask.waiting, F.text & ~F.text.startswith("/"))
async def on_task_after_button(message: Message, state: FSMContext, journal: Journal) -> None:
    data = await state.get_data()
    forced_raw = data.get("forced_module")
    task_text = (message.text or "").strip()
    await state.clear()

    if not task_text:
        await message.answer("Пустой запрос. Опишите задачу.", reply_markup=main_menu())
        return

    if forced_raw:
        forced = Module(forced_raw)
        decision = route_for_button(forced, task_text)
    else:
        decision = route_text(task_text)

    await journal.add(
        task_text=task_text,
        primary_module=decision.primary_module.value,
        secondary_modules=tuple(m.value for m in decision.secondary_modules),
        safety_level=decision.safety_level.value,
    )
    await message.answer(build_card(decision), reply_markup=main_menu())


@router.message(F.text & ~F.text.startswith("/"))
async def on_free_text(message: Message, journal: Journal) -> None:
    task_text = (message.text or "").strip()
    if not task_text:
        await message.answer("Пустой запрос. Опишите задачу.", reply_markup=main_menu())
        return
    decision = route_text(task_text)
    await journal.add(
        task_text=task_text,
        primary_module=decision.primary_module.value,
        secondary_modules=tuple(m.value for m in decision.secondary_modules),
        safety_level=decision.safety_level.value,
    )
    await message.answer(build_card(decision), reply_markup=main_menu())
