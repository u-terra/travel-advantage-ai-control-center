"""«☀️ Что делать сегодня»: минимальный экран Next Best Action.

Только Telegram-рендеринг уже готового результата — сбор данных и переходы
состояния вынесены в transport-independent сервисы: DailyActionsService
(app/services/daily_actions.py) и WorkTransitionService
(app/services/work_transitions.py). Здесь остаются: разбор callback_data,
построение клавиатур, тексты для пользователя и перевод TransitionStatus в
конкретное Telegram-сообщение — то, что реально специфично для Telegram и
не обязано (и не должно) переиспользоваться VK-адаптером напрямую.

«💬 Продолжить разговор» / «💬 Написать снова» — bridge в уже существующий
TRAVEL_ASSISTANT Reply flow (app/handlers/tasks.py) через тот же FSM-вход,
что и кнопка «Ответить клиенту» (AwaitTask.waiting + forced_module). После
генерации черновика Reply flow сам создаёт/обновляет work_item (см.
tasks.py:_sync_reply_work_item → app/services/reply_sync.py) и прикладывает
к черновику кнопки «✅ Отправил / 🕒 Позже / 🚫 Не актуально» — их хендлеры
живут здесь же, рядом с остальными переходами состояния work_item.

derived-кандидаты (незаполненный профиль, черновики, сигналы) добираются
максимально консервативно: если данных недостаточно для безопасной карточки
(пустой заголовок, сигнал без текста), кандидат просто пропускается — сервис
никогда не «придумывает» карточку.
"""

from __future__ import annotations

from datetime import datetime

from aiogram import F, Router
from aiogram.filters import MagicData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.domain.partners import WorkspaceContext
from app.domain.work import NextAction, WaitingSubject
from app.handlers.menu import AwaitTask
from app.keyboards import (
    BTN_V2_DAILY_ACTIONS,
    DAILY_ACTION_DISMISS_PREFIX,
    DAILY_ACTION_DONE_PREFIX,
    DAILY_ACTION_PROMPT_PREFIX,
    DAILY_ACTION_REPLIED_PREFIX,
    DAILY_ACTION_SNOOZE_PREFIX,
    REPLY_CONFIRM_DISMISS_PREFIX,
    REPLY_CONFIRM_LATER_PREFIX,
    REPLY_CONFIRM_SENT_PREFIX,
    daily_action_keyboard,
    v2_back_keyboard,
)
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.partner_repository import PartnerRepository
from app.repositories.work_repository import WorkRepository
from app.repositories.workspace_signal_repository import WorkspaceSignalRepository
from app.routing.modules import Module
from app.services.daily_actions import DailyActionsService
from app.services.work_transitions import TransitionStatus, WorkTransitionService

router = Router(name="daily_actions")

_UNAVAILABLE = "Рабочее пространство недоступно."
_WORK_ITEM_NOT_FOUND = (
    "Действие уже недоступно — возможно, вы обновили его в другом чате."
)
_STALE_ACTION_MESSAGE = (
    'Это действие уже устарело. Откройте «Что делать сегодня».'
)
_HEADLINE = "☀️ Что делать сегодня"
_NO_ACTIONS_TEXT = "Незавершённых задач нет — можно начать новую тему или контакт."
_WAITING_HEADLINE = "⏳ Ждём ответ"

_DIALOG_ACTION_BUCKETS = {"active_dialog": "active_dialog", "due_follow_up": "due_follow_up"}


def _failure_message(status: TransitionStatus) -> str:
    """STALE — отдельное сообщение (revision не совпал, показана более
    старая клавиатура). NOT_FOUND и INVALID_STATE для пользователя выглядят
    одинаково — «действие недоступно» — различие между ними нужно только
    внутри WorkTransitionService."""
    if status is TransitionStatus.STALE:
        return _STALE_ACTION_MESSAGE
    return _WORK_ITEM_NOT_FOUND


def _parse_work_item_id(data: str, prefix: str) -> int | None:
    raw = data.removeprefix(prefix)
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


def _parse_reply_confirm_callback(data: str, prefix: str) -> tuple[int, str] | None:
    """work_item_id + optimistic-revision из callback_data кнопки под Reply-
    черновиком (см. keyboards.reply_confirm_keyboard). Отсутствующий или
    пустой revision-сегмент — как и нечисловой id — трактуется как
    неразбираемый callback, а не как «revision не важен»."""
    raw = data.removeprefix(prefix)
    work_item_part, separator, revision = raw.partition(":")
    if not separator or not revision:
        return None
    if not work_item_part.isdigit() or int(work_item_part) <= 0:
        return None
    return int(work_item_part), revision


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
    daily = await DailyActionsService(
        work_repository, partner_repository, artifact_repository,
        workspace_signal_repository,
    ).build(workspace_id)

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

    Bridge не меняет lifecycle/loop_state work_item — допустимость перехода
    проверяет WorkTransitionService.prepare_bridge (только чтение). Состояние
    меняется исключительно явными кнопками «Ответил(а)» / «Завершить» /
    «Перенести» / «Не актуально».
    """
    work_item_id = _parse_work_item_id(callback.data or "", DAILY_ACTION_PROMPT_PREFIX)
    if work_item_id is None or workspace_context is None:
        await callback.answer(_WORK_ITEM_NOT_FOUND, show_alert=True)
        return
    result = await WorkTransitionService(work_repository).prepare_bridge(
        workspace_context.workspace_id, work_item_id,
    )
    if result.status is not TransitionStatus.OK:
        await callback.answer(_failure_message(result.status), show_alert=True)
        return
    item = result.work_item
    assert item is not None

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
        daily_action_subject_id=item.subject_id,
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
    result = await WorkTransitionService(work_repository).mark_reply_received(
        workspace_context.workspace_id, work_item_id,
    )
    if result.status is not TransitionStatus.OK:
        await callback.answer(_failure_message(result.status), show_alert=True)
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
    result = await WorkTransitionService(work_repository).snooze(
        workspace_context.workspace_id, work_item_id,
    )
    if result.status is not TransitionStatus.OK:
        await callback.answer(_failure_message(result.status), show_alert=True)
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
    result = await WorkTransitionService(work_repository).mark_done(
        workspace_context.workspace_id, work_item_id,
    )
    if result.status is not TransitionStatus.OK:
        await callback.answer(_failure_message(result.status), show_alert=True)
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
    result = await WorkTransitionService(work_repository).mark_dismissed(
        workspace_context.workspace_id, work_item_id,
    )
    if result.status is not TransitionStatus.OK:
        await callback.answer(_failure_message(result.status), show_alert=True)
        return
    await callback.answer("Отмечено как неактуальное.")
    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(MagicData(F.v2_menu_enabled), F.data.startswith(REPLY_CONFIRM_SENT_PREFIX))
async def on_reply_confirm_sent(
    callback: CallbackQuery,
    workspace_context: WorkspaceContext | None,
    work_repository: WorkRepository,
) -> None:
    """«✅ Отправил» под свежим Reply-черновиком: тот же work_item уходит в
    waiting_reply с новым due_at — не создаёт второй work_item ни при первом
    сообщении, ни при повторном нажатии (mark_reply_sent идемпотентен).

    Revision-guard (WorkTransitionService.mark_sent): если для этого же
    work_item успели подготовить более новый черновик (updated_at сдвинулся),
    кнопка под ЭТИМ, более старым черновиком, отклоняется как устаревшая —
    без этого случайный тап по забытой клавиатуре мог бы откатить уже более
    свежее состояние.
    """
    parsed = _parse_reply_confirm_callback(callback.data or "", REPLY_CONFIRM_SENT_PREFIX)
    if parsed is None or workspace_context is None:
        await callback.answer(_WORK_ITEM_NOT_FOUND, show_alert=True)
        return
    work_item_id, revision = parsed
    result = await WorkTransitionService(work_repository).mark_sent(
        workspace_context.workspace_id, work_item_id, revision=revision,
    )
    if result.status is not TransitionStatus.OK:
        await callback.answer(_failure_message(result.status), show_alert=True)
        return
    await callback.answer("Отмечено: ждём ответ.")
    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(MagicData(F.v2_menu_enabled), F.data.startswith(REPLY_CONFIRM_LATER_PREFIX))
async def on_reply_confirm_later(
    callback: CallbackQuery,
    workspace_context: WorkspaceContext | None,
    work_repository: WorkRepository,
) -> None:
    """«🕒 Позже»: ничего не меняет — work_item остаётся как есть (обычно
    active_dialog) и продолжает быть виден в «Что делать сегодня». Не
    мутирует ничего даже без revision-guard, но проверяем его тоже — ради
    единообразия и чтобы toast не подтверждал устаревший черновик как
    актуальный."""
    parsed = _parse_reply_confirm_callback(callback.data or "", REPLY_CONFIRM_LATER_PREFIX)
    if parsed is None or workspace_context is None:
        await callback.answer(_WORK_ITEM_NOT_FOUND, show_alert=True)
        return
    work_item_id, revision = parsed
    result = await WorkTransitionService(work_repository).later(
        workspace_context.workspace_id, work_item_id, revision=revision,
    )
    if result.status is not TransitionStatus.OK:
        await callback.answer(_failure_message(result.status), show_alert=True)
        return
    await callback.answer("Хорошо, останется в «Что делать сегодня».")
    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(MagicData(F.v2_menu_enabled), F.data.startswith(REPLY_CONFIRM_DISMISS_PREFIX))
async def on_reply_confirm_dismiss(
    callback: CallbackQuery,
    workspace_context: WorkspaceContext | None,
    work_repository: WorkRepository,
) -> None:
    """«🚫 Не актуально» под свежим Reply-черновиком.

    Отдельный от DAILY_ACTION_DISMISS_PREFIX/on_daily_action_dismiss
    callback: тот не несёт revision. Оба в итоге вызывают тот же
    WorkTransitionService.mark_dismissed — с revision здесь, без него там.
    """
    parsed = _parse_reply_confirm_callback(callback.data or "", REPLY_CONFIRM_DISMISS_PREFIX)
    if parsed is None or workspace_context is None:
        await callback.answer(_WORK_ITEM_NOT_FOUND, show_alert=True)
        return
    work_item_id, revision = parsed
    result = await WorkTransitionService(work_repository).mark_dismissed(
        workspace_context.workspace_id, work_item_id, revision=revision,
    )
    if result.status is not TransitionStatus.OK:
        await callback.answer(_failure_message(result.status), show_alert=True)
        return
    await callback.answer("Отмечено как неактуальное.")
    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)
