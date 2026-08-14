"""Раздел «Мои конкуренты»: workspace-список ссылок на конкурентов.

Только хранение и просмотр ссылок, которые владелец workspace сам считает
конкурентами — база для будущего конкурентного анализа, не сам анализ.
Ничего не скачивается и не анализируется автоматически.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import MagicData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.domain.competitors import Competitor
from app.domain.partners import WorkspaceContext
from app.keyboards import (
    BTN_V2_COMPETITORS,
    BTN_V2_MAIN_MENU,
    COMPETITOR_REGISTRY_ADD,
    active_main_menu,
    competitors_list_keyboard,
    v2_back_keyboard,
)
from app.repositories.competitor_repository import (
    CompetitorAddressError,
    CompetitorRepository,
)

router = Router(name="competitors")

_LIST_LIMIT = 20

_UNAVAILABLE = "Рабочее пространство недоступно."
_EMPTY = (
    "🎯 Мои конкуренты\n\n"
    "Пока не сохранено ни одной ссылки.\n"
    "Добавьте сайт или ресурс конкурента — Оркестратор сможет использовать "
    "его для анализа в вашем рабочем пространстве."
)
_ADD_PROMPT = (
    "Пришлите ссылку на сайт или ресурс конкурента "
    "(начинается с http:// или https://)."
)
_SAVED = (
    "Конкурент сохранён. Ссылка будет использоваться Оркестратором для "
    "анализа в вашем рабочем пространстве."
)


class AddCompetitor(StatesGroup):
    waiting_for_url = State()


def _summary(competitors: tuple[Competitor, ...]) -> str:
    lines = ["🎯 Мои конкуренты", "", f"Сохранено: {len(competitors)}", ""]
    for index, competitor in enumerate(competitors, start=1):
        lines.append(f"{index}. {competitor.label}")
    return "\n".join(lines)


@router.message(MagicData(F.v2_menu_enabled), F.text == BTN_V2_COMPETITORS)
async def show_competitors(
    message: Message,
    state: FSMContext,
    competitor_repository: CompetitorRepository,
    workspace_context: WorkspaceContext | None,
) -> None:
    await state.clear()
    if workspace_context is None:
        await message.answer(_UNAVAILABLE, reply_markup=v2_back_keyboard())
        return

    competitors = tuple(
        await competitor_repository.list_for_workspace(
            workspace_context.workspace_id, limit=_LIST_LIMIT
        )
    )
    if not competitors:
        await message.answer(_EMPTY, reply_markup=competitors_list_keyboard())
        return

    await message.answer(
        _summary(competitors),
        reply_markup=competitors_list_keyboard(),
        disable_web_page_preview=True,
    )


@router.callback_query(
    MagicData(F.v2_menu_enabled), F.data == COMPETITOR_REGISTRY_ADD
)
async def start_add_competitor(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddCompetitor.waiting_for_url)
    await callback.answer()
    if callback.message is not None:
        await callback.message.answer(_ADD_PROMPT, reply_markup=v2_back_keyboard())


@router.message(
    MagicData(F.v2_menu_enabled),
    AddCompetitor.waiting_for_url,
    F.text == BTN_V2_MAIN_MENU,
)
async def cancel_add_competitor(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Главное меню. Выберите нужную задачу.", reply_markup=active_main_menu(True)
    )


@router.message(MagicData(F.v2_menu_enabled), AddCompetitor.waiting_for_url)
async def receive_competitor_url(
    message: Message,
    state: FSMContext,
    competitor_repository: CompetitorRepository,
    workspace_context: WorkspaceContext | None,
) -> None:
    address = (message.text or "").strip()

    if workspace_context is None:
        await message.answer(_UNAVAILABLE, reply_markup=v2_back_keyboard())
        return

    try:
        await competitor_repository.add_competitor(
            workspace_context.workspace_id, address
        )
    except CompetitorAddressError as exc:
        await message.answer(f"Не получилось: {exc}", reply_markup=v2_back_keyboard())
        return

    await state.clear()
    await message.answer(_SAVED, reply_markup=active_main_menu(True))
