"""«☀️ Что делать сегодня»: минимальный экран Next Best Action.

Только шаблонные карточки из уже посчитанного NextBestActionService.build() —
без LLM. Этот экран умеет отображать уже существующие WorkSubject/WorkItem и
переводить их состояние по нажатию кнопки.

«💬 Продолжить разговор» / «💬 Написать снова» — не тупик: это bridge в уже
существующий TRAVEL_ASSISTANT Reply flow (app/handlers/tasks.py) через тот же
FSM-вход, что и кнопка «Ответить клиенту» (AwaitTask.waiting + forced_module).
Обратного направления пока нет: Reply flow по-прежнему не создаёт и не
закрывает WorkItem сам — это отдельная, сознательно отложенная задача, а не
упущение (см. docstring on_daily_action_prompt ниже).

derived-кандидаты (незаполненный профиль, черновики, сигналы) добираются
максимально консервативно: если данных недостаточно для безопасной карточки
(пустой заголовок, сигнал без текста), кандидат просто пропускается — сервис
никогда не «придумывает» карточку.

derived-кандидаты (незаполненный профиль, черновики, сигналы) добираются
максимально консервативно: если данных недостаточно для безопасной карточки
(пустой заголовок, сигнал без текста), кандидат просто пропускается — сервис
никогда не «придумывает» карточку.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import MagicData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.domain.content import Artifact
from app.domain.partners import WorkspaceContext
from app.domain.work import NextAction, SignalOpportunity, WaitingSubject
from app.handlers.menu import AwaitTask
from app.keyboards import (
    BTN_V2_DAILY_ACTIONS,
    DAILY_ACTION_DISMISS_PREFIX,
    DAILY_ACTION_DONE_PREFIX,
    DAILY_ACTION_PROMPT_PREFIX,
    DAILY_ACTION_REPLIED_PREFIX,
    DAILY_ACTION_SNOOZE_PREFIX,
    daily_action_keyboard,
    v2_back_keyboard,
)
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.partner_repository import PartnerRepository
from app.repositories.work_repository import WorkRepository
from app.repositories.workspace_signal_repository import WorkspaceSignalRepository
from app.routing.modules import Module
from app.services.next_best_action import NextBestActionService

router = Router(name="daily_actions")
log = logging.getLogger(__name__)

_UNAVAILABLE = "Рабочее пространство недоступно."
_WORK_ITEM_NOT_FOUND = (
    "Действие уже недоступно — возможно, вы обновили его в другом чате."
)
_HEADLINE = "☀️ Что делать сегодня"
_NO_ACTIONS_TEXT = "Незавершённых задач нет — можно начать новую тему или контакт."
_WAITING_HEADLINE = "⏳ Ждём ответ"

_SNOOZE_OFFSET = timedelta(days=2)
_SIGNAL_SCORE_THRESHOLD = 60.0
_DRAFT_STATUSES = ("draft", "review_required")
_DRAFT_FETCH_LIMIT = 5
_SIGNAL_FETCH_LIMIT = 20

_DIALOG_ACTION_BUCKETS = {"active_dialog": "active_dialog", "due_follow_up": "due_follow_up"}


def _now(*, offset: timedelta = timedelta(0)) -> str:
    return (datetime.now(timezone.utc) + offset).isoformat()


def _parse_work_item_id(data: str, prefix: str) -> int | None:
    raw = data.removeprefix(prefix)
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


def _short_date(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y")
    except ValueError:
        return value


def _action_text(action: NextAction, index: int) -> str:
    lines = [f"{index}. {action.headline}"]
    if action.detail and action.detail != action.headline:
        lines.append(action.detail)
    return "\n".join(lines)


def _waiting_text(waiting: WaitingSubject) -> str:
    name = waiting.subject_name or "Без имени"
    lines = [f"{_WAITING_HEADLINE}: {name}"]
    if waiting.next_step:
        lines.append(waiting.next_step)
    if waiting.due_at:
        lines.append(f"Можно напомнить с {_short_date(waiting.due_at)}")
    return "\n".join(lines)


async def _fetch_draft_artifacts(
    artifact_repository: ArtifactRepository, workspace_id: int,
) -> list[Artifact]:
    drafts: list[Artifact] = []
    for status in _DRAFT_STATUSES:
        drafts.extend(await artifact_repository.list_artifacts(
            workspace_id, limit=_DRAFT_FETCH_LIMIT, status=status,
        ))
    return drafts


async def _fetch_signal_candidates(
    workspace_signal_repository: WorkspaceSignalRepository | None, workspace_id: int,
) -> list[SignalOpportunity]:
    """Только уже авторизованные workspace-сигналы с известным заголовком и
    высоким ai_score. Никакого вызова recommender/LLM здесь нет — это
    сознательно самый простой и безопасный срез derived-кандидатов."""
    if workspace_signal_repository is None:
        return []
    try:
        records = await workspace_signal_repository.list_for_workspace(
            workspace_id, limit=_SIGNAL_FETCH_LIMIT,
        )
    except Exception:
        log.warning("daily_actions: failed to fetch workspace signals")
        return []

    candidates: list[SignalOpportunity] = []
    for record in records:
        if record.status != "new":
            continue
        if record.ai_score is None or record.ai_score < _SIGNAL_SCORE_THRESHOLD:
            continue
        if not record.item_title:
            continue
        candidates.append(SignalOpportunity(
            signal_interpretation_id=record.interpretation_id,
            title=record.item_title,
            reason=record.ai_reason or record.item_summary or "Новый сигнал интереса.",
        ))
    return candidates


@router.message(MagicData(F.v2_menu_enabled), F.text == BTN_V2_DAILY_ACTIONS)
async def show_daily_actions(
    message: Message,
    workspace_context: WorkspaceContext | None,
    work_repository: WorkRepository,
    partner_repository: PartnerRepository,
    artifact_repository: ArtifactRepository,
    workspace_signal_repository: WorkspaceSignalRepository | None = None,
) -> None:
    if workspace_context is None:
        await message.answer(_UNAVAILABLE, reply_markup=v2_back_keyboard())
        return

    workspace_id = workspace_context.workspace_id
    now = _now()

    profile = await partner_repository.get_business_profile(workspace_id)
    ta_affiliated = profile is not None and profile.ta_affiliated
    profile_incomplete = profile is None or profile.profile_status == "incomplete"

    open_items = await work_repository.list_open_actionable(workspace_id, now=now)
    waiting_items = await work_repository.list_waiting_not_due(workspace_id, now=now)
    draft_artifacts = await _fetch_draft_artifacts(artifact_repository, workspace_id)
    signal_candidates = await _fetch_signal_candidates(
        workspace_signal_repository, workspace_id,
    )

    daily = NextBestActionService().build(
        workspace_id=workspace_id,
        ta_affiliated=ta_affiliated,
        open_work_items=open_items,
        waiting_not_due=waiting_items,
        profile_incomplete=profile_incomplete,
        draft_artifacts=draft_artifacts,
        signal_candidates=signal_candidates,
    )

    await message.answer(_HEADLINE, reply_markup=v2_back_keyboard())

    if not daily.actions:
        await message.answer(_NO_ACTIONS_TEXT)
    for index, action in enumerate(daily.actions, start=1):
        bucket = _DIALOG_ACTION_BUCKETS.get(action.source)
        keyboard = (
            daily_action_keyboard(action.work_item_id, bucket)
            if bucket is not None and action.work_item_id is not None
            else None
        )
        await message.answer(_action_text(action, index), reply_markup=keyboard)

    if daily.waiting:
        await message.answer(_WAITING_HEADLINE)
        for waiting in daily.waiting:
            await message.answer(
                _waiting_text(waiting),
                reply_markup=daily_action_keyboard(waiting.work_item_id, "waiting_not_due"),
            )


_ACTIVE_DIALOG_PROMPT_NO_SUBJECT = (
    "Пришлите последнее сообщение собеседника, и я помогу подготовить ответ."
)
_DUE_FOLLOW_UP_PROMPT_NO_SUBJECT = "Опишите, по какому поводу хотите написать снова."


def _bridge_prompt_text(loop_state: str | None, subject_name: str | None) -> str:
    """Имя субъекта никогда программно не склоняется — только вставляется как
    есть в именительном падеже («Контакт: Иван.»/«Контакт: Ольга.»). Поэтому
    шаблоны фиксированы и не пытаются строить грамматически согласованную
    фразу вокруг произвольного имени."""
    if loop_state == "active_dialog":
        if subject_name:
            return (
                f"Контакт: {subject_name}. Пришлите его последнее сообщение, "
                "и я помогу подготовить ответ."
            )
        return _ACTIVE_DIALOG_PROMPT_NO_SUBJECT
    if subject_name:
        return f"Контакт: {subject_name}. Опишите, по какому поводу хотите написать снова."
    return _DUE_FOLLOW_UP_PROMPT_NO_SUBJECT


def _is_promptable(item: WorkItem, *, now: str) -> bool:
    """Может ли этот work_item сейчас вести в Reply flow через кнопку.

    Старое сообщение Telegram с callback от уже закрытого/отложенного item —
    обычный сценарий (пользователь мог нажать «Завершить»/«Перенести» в
    другом чате, или экран просто устарел): такой callback должен fail-closed
    остановиться здесь, не трогая FSM и не запуская Reply flow.
    """
    if item.lifecycle != "open":
        return False
    if item.loop_state == "active_dialog":
        return True
    if item.loop_state == "waiting_reply" and item.due_at is not None and item.due_at <= now:
        return True
    return False


@router.callback_query(MagicData(F.v2_menu_enabled), F.data.startswith(DAILY_ACTION_PROMPT_PREFIX))
async def on_daily_action_prompt(
    callback: CallbackQuery,
    state: FSMContext,
    workspace_context: WorkspaceContext | None,
    work_repository: WorkRepository,
) -> None:
    """«Продолжить разговор» / «Написать снова» — bridge в уже существующий
    TRAVEL_ASSISTANT Reply flow (app/handlers/tasks.py), а не тупик.

    Здесь нет новой генерации и нет копии логики Content Factory: bridge
    только переиспользует тот же вход, что и кнопка «💬 Ответить клиенту» —
    forced_module=TRAVEL_ASSISTANT + AwaitTask.waiting (см. on_v2_category в
    app/handlers/menu.py). work_item_id/subject_name кладутся в FSM данные
    рядом, но tasks.py их сегодня не читает — это сознательно оставлено для
    отдельного шага, не переписывающего Reply flow.

    Bridge не меняет lifecycle/loop_state work_item — только читает его
    (get_work_item/get_subject). Состояние меняется исключительно явными
    кнопками «Ответил(а)» / «Завершить» / «Перенести» / «Не актуально».
    """
    work_item_id = _parse_work_item_id(callback.data or "", DAILY_ACTION_PROMPT_PREFIX)
    if work_item_id is None or workspace_context is None:
        await callback.answer(_WORK_ITEM_NOT_FOUND, show_alert=True)
        return
    item = await work_repository.get_work_item(workspace_context.workspace_id, work_item_id)
    if item is None or not _is_promptable(item, now=_now()):
        await callback.answer(_WORK_ITEM_NOT_FOUND, show_alert=True)
        return

    subject_name: str | None = None
    if item.subject_id is not None:
        subject = await work_repository.get_subject(
            workspace_context.workspace_id, item.subject_id,
        )
        subject_name = subject.name if subject is not None else None

    await state.update_data(
        forced_module=Module.TRAVEL_ASSISTANT.value,
        skip_route_card=True,
        daily_action_work_item_id=item.id,
        daily_action_subject_name=subject_name,
    )
    await state.set_state(AwaitTask.waiting)

    await callback.answer()
    if callback.message is not None:
        await callback.message.answer(_bridge_prompt_text(item.loop_state, subject_name))


@router.callback_query(MagicData(F.v2_menu_enabled), F.data.startswith(DAILY_ACTION_REPLIED_PREFIX))
async def on_daily_action_replied(
    callback: CallbackQuery,
    workspace_context: WorkspaceContext | None,
    work_repository: WorkRepository,
) -> None:
    work_item_id = _parse_work_item_id(callback.data or "", DAILY_ACTION_REPLIED_PREFIX)
    if work_item_id is None or workspace_context is None:
        await callback.answer(_WORK_ITEM_NOT_FOUND, show_alert=True)
        return
    updated = await work_repository.mark_reply_received(
        workspace_context.workspace_id, work_item_id,
    )
    if updated is None:
        await callback.answer(_WORK_ITEM_NOT_FOUND, show_alert=True)
        return
    await callback.answer("Отмечено: продолжайте разговор.")
    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(MagicData(F.v2_menu_enabled), F.data.startswith(DAILY_ACTION_SNOOZE_PREFIX))
async def on_daily_action_snooze(
    callback: CallbackQuery,
    workspace_context: WorkspaceContext | None,
    work_repository: WorkRepository,
) -> None:
    work_item_id = _parse_work_item_id(callback.data or "", DAILY_ACTION_SNOOZE_PREFIX)
    if work_item_id is None or workspace_context is None:
        await callback.answer(_WORK_ITEM_NOT_FOUND, show_alert=True)
        return
    updated = await work_repository.snooze(
        workspace_context.workspace_id, work_item_id, due_at=_now(offset=_SNOOZE_OFFSET),
    )
    if updated is None:
        await callback.answer(_WORK_ITEM_NOT_FOUND, show_alert=True)
        return
    await callback.answer("Перенесено на 2 дня.")
    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(MagicData(F.v2_menu_enabled), F.data.startswith(DAILY_ACTION_DONE_PREFIX))
async def on_daily_action_done(
    callback: CallbackQuery,
    workspace_context: WorkspaceContext | None,
    work_repository: WorkRepository,
) -> None:
    work_item_id = _parse_work_item_id(callback.data or "", DAILY_ACTION_DONE_PREFIX)
    if work_item_id is None or workspace_context is None:
        await callback.answer(_WORK_ITEM_NOT_FOUND, show_alert=True)
        return
    updated = await work_repository.resolve_done(
        workspace_context.workspace_id, work_item_id,
    )
    if updated is None:
        await callback.answer(_WORK_ITEM_NOT_FOUND, show_alert=True)
        return
    await callback.answer("Завершено.")
    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(MagicData(F.v2_menu_enabled), F.data.startswith(DAILY_ACTION_DISMISS_PREFIX))
async def on_daily_action_dismiss(
    callback: CallbackQuery,
    workspace_context: WorkspaceContext | None,
    work_repository: WorkRepository,
) -> None:
    work_item_id = _parse_work_item_id(callback.data or "", DAILY_ACTION_DISMISS_PREFIX)
    if work_item_id is None or workspace_context is None:
        await callback.answer(_WORK_ITEM_NOT_FOUND, show_alert=True)
        return
    updated = await work_repository.resolve_dismissed(
        workspace_context.workspace_id, work_item_id,
    )
    if updated is None:
        await callback.answer(_WORK_ITEM_NOT_FOUND, show_alert=True)
        return
    await callback.answer("Отмечено как неактуальное.")
    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)
