"""Раздел «Мои конкуренты»: workspace-список ссылок на конкурентов.

Только хранение и просмотр ссылок, которые владелец workspace сам считает
конкурентами — база для будущего конкурентного анализа, не сам анализ.
Ничего не скачивается и не анализируется автоматически.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

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
    COMPETITOR_REGISTRY_RENAME_PREFIX,
    active_main_menu,
    competitors_list_keyboard,
    v2_back_keyboard,
)
from app.repositories.competitor_repository import (
    CompetitorAddressError,
    CompetitorLabelError,
    CompetitorRepository,
)
from app.repositories.conversation_state_repository import ConversationStateRepository

router = Router(name="competitors")
log = logging.getLogger(__name__)

_LIST_LIMIT = 20
_BUTTON_LABEL_MAX_CHARS = 40

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
_RENAME_PROMPT = "Как назвать этого конкурента? Например: ТурКлуб"
_RENAME_NOT_FOUND = "Не удалось найти конкурента — возможно, он уже удалён."
_RENAME_SAVED = "Название сохранено."


class AddCompetitor(StatesGroup):
    waiting_for_url = State()


class RenameCompetitor(StatesGroup):
    waiting_for_label = State()


_RENAME_COMPETITOR_ID_KEY = "rename_competitor_id"
_RENAME_QUESTION_TYPE = "competitor_label"
# F2A: TTL for the persisted PendingQuestion mirror of this FSM step -
# generous enough to outlive a realistic "user steps away before answering".
_RENAME_QUESTION_TTL = timedelta(minutes=15)


def _line(competitor: Competitor) -> str:
    if competitor.label != competitor.url:
        return f"{competitor.label} — {competitor.url}"
    return competitor.label


def _summary(competitors: tuple[Competitor, ...]) -> str:
    lines = ["🎯 Мои конкуренты", "", f"Сохранено: {len(competitors)}", ""]
    for index, competitor in enumerate(competitors, start=1):
        lines.append(f"{index}. {_line(competitor)}")
    return "\n".join(lines)


def _button_label(competitor: Competitor) -> str:
    text = competitor.label
    if len(text) <= _BUTTON_LABEL_MAX_CHARS:
        return text
    return text[: _BUTTON_LABEL_MAX_CHARS - 1].rstrip() + "…"


def _list_keyboard(competitors: tuple[Competitor, ...]):
    return competitors_list_keyboard(
        tuple((competitor.id, _button_label(competitor)) for competitor in competitors)
    )


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
        reply_markup=_list_keyboard(competitors),
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


@router.callback_query(
    MagicData(F.v2_menu_enabled), F.data.startswith(COMPETITOR_REGISTRY_RENAME_PREFIX)
)
async def start_rename_competitor(
    callback: CallbackQuery,
    state: FSMContext,
    workspace_context: WorkspaceContext | None = None,
    conversation_state_repository: ConversationStateRepository | None = None,
) -> None:
    raw_id = (callback.data or "").removeprefix(COMPETITOR_REGISTRY_RENAME_PREFIX)
    await callback.answer()
    if not raw_id.isdigit() or int(raw_id) <= 0:
        return
    competitor_id = int(raw_id)
    await state.update_data(**{_RENAME_COMPETITOR_ID_KEY: competitor_id})
    await state.set_state(RenameCompetitor.waiting_for_label)
    # F2A PendingQuestion pilot: persisted in parallel to the FSM state above
    # - the FSM remains the actual mechanism driving this flow (see module
    # docstring/F2A report). Best-effort: a failure here must not stop the
    # existing rename flow from working exactly as before.
    if conversation_state_repository is not None and workspace_context is not None:
        try:
            await conversation_state_repository.create_question(
                workspace_context.workspace_id, workspace_context.telegram_user_id,
                _RENAME_QUESTION_TYPE, _RENAME_PROMPT,
                subject_ref_type="competitor", subject_ref_id=competitor_id,
                expires_at=(datetime.now(timezone.utc) + _RENAME_QUESTION_TTL).isoformat(),
            )
            # F2A subject_ref pilot: the user just opened a *known, already
            # persisted* competitor by id - not a guess derived from text
            # (see the F2A "не создавать fictitious subject_ref" constraint).
            await conversation_state_repository.patch_state(
                workspace_context.workspace_id, workspace_context.telegram_user_id,
                active_module="competitors", current_task="rename_competitor",
                current_subject_ref_type="competitor", current_subject_ref_id=competitor_id,
                last_action="rename_competitor_started",
            )
        except Exception:
            log.warning("competitors: conversation state bookkeeping failed for rename start")
    if callback.message is not None:
        await callback.message.answer(_RENAME_PROMPT, reply_markup=v2_back_keyboard())


@router.message(
    MagicData(F.v2_menu_enabled),
    RenameCompetitor.waiting_for_label,
    F.text == BTN_V2_MAIN_MENU,
)
async def cancel_rename_competitor(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Главное меню. Выберите нужную задачу.", reply_markup=active_main_menu(True)
    )


@router.message(MagicData(F.v2_menu_enabled), RenameCompetitor.waiting_for_label)
async def receive_competitor_label(
    message: Message,
    state: FSMContext,
    competitor_repository: CompetitorRepository,
    workspace_context: WorkspaceContext | None,
    conversation_state_repository: ConversationStateRepository | None = None,
) -> None:
    label = (message.text or "").strip()
    data = await state.get_data()
    competitor_id = data.get(_RENAME_COMPETITOR_ID_KEY)
    await state.clear()

    if workspace_context is None:
        await message.answer(_UNAVAILABLE, reply_markup=v2_back_keyboard())
        return
    if not isinstance(competitor_id, int):
        await message.answer(_RENAME_NOT_FOUND, reply_markup=active_main_menu(True))
        return

    try:
        updated = await competitor_repository.update_label(
            workspace_context.workspace_id, competitor_id, label,
        )
    except CompetitorLabelError as exc:
        await message.answer(f"Не получилось: {exc}", reply_markup=v2_back_keyboard())
        return

    if updated is None:
        await message.answer(_RENAME_NOT_FOUND, reply_markup=active_main_menu(True))
        return

    await _answer_rename_question_if_active(
        conversation_state_repository, workspace_context, competitor_id,
    )
    await message.answer(_RENAME_SAVED, reply_markup=active_main_menu(True))


async def _answer_rename_question_if_active(
    conversation_state_repository: ConversationStateRepository | None,
    workspace_context: WorkspaceContext,
    competitor_id: int,
) -> None:
    """Marks the F2A PendingQuestion pilot answered, if still active and
    matching this exact competitor. Best-effort - the FSM path above already
    completed the actual rename; this is bookkeeping only.

    Note (restart behaviour, per F2A spec section 3): FSM uses MemoryStorage,
    so a process restart between the button press and this handler wipes the
    FSM state - this function then simply never runs, and the persisted
    question is left to expire via its TTL rather than being answered. No
    catch-all resolver is added to handle that case in F2A - see the F2A
    report.
    """
    if conversation_state_repository is None:
        return
    try:
        question = await conversation_state_repository.get_active_question(
            workspace_context.workspace_id, workspace_context.telegram_user_id,
        )
        if (
            question is not None
            and question.question_type == _RENAME_QUESTION_TYPE
            and question.subject_ref_type == "competitor"
            and question.subject_ref_id == competitor_id
        ):
            await conversation_state_repository.answer_question(
                workspace_context.workspace_id, workspace_context.telegram_user_id,
                question.id,
            )
    except Exception:
        log.warning("competitors: conversation state bookkeeping failed for rename answer")
