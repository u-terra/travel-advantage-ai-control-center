from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, ReplyKeyboardRemove

from app.domain.partners import WorkspaceContext
from app.keyboards import BTN_HOW_IT_WORKS, active_main_menu, main_menu
from app.repositories.partner_repository import PartnerRepository

router = Router(name="start")


HELP_TEXT = (
    "Travel AI Orchestrator — рабочий ассистент для контента и коммуникаций.\n\n"
    "Что он делает:\n"
    "1. Принимает задачу кнопкой или обычным текстом.\n"
    "2. Определяет нужный модуль экосистемы.\n"
    "3. Отмечает, требуется ли проверка Safety Layer.\n"
    "4. Возвращает карточку маршрута и ручные шаги.\n\n"
    "Бот ничего не отправляет людям, не публикует посты, не бронирует "
    "и не принимает решения вместо человека."
)

# Рабочее пространство не определено однозначно (нет активного membership,
# либо оно ссылается на неактивный workspace). Fail-closed: подробности БД и
# идентификаторы наружу не раскрываются, решение — за администратором.
WORKSPACE_UNAVAILABLE_TEXT = (
    "Рабочее пространство для вашего аккаунта пока не подключено.\n\n"
    "Обратитесь к администратору сервиса, чтобы вам открыли доступ."
)

# Несколько активных membership у одного Telegram-пользователя: автоматический
# выбор workspace недопустим (см. AmbiguousWorkspaceError), поэтому явно
# просим обратиться к администратору, а не тихо открываем произвольное.
WORKSPACE_AMBIGUOUS_TEXT = (
    "К вашему аккаунту привязано несколько рабочих пространств, поэтому мы не "
    "можем однозначно выбрать нужное автоматически.\n\n"
    "Обратитесь к администратору сервиса."
)


async def _resolve_start_reply(
    workspace_context: WorkspaceContext | None,
    workspace_context_ambiguous: bool,
    partner_repository: PartnerRepository | None,
) -> tuple[str, bool]:
    """Возвращает (текст, доступен ли workspace для показа главного меню)."""
    if workspace_context is not None:
        workspace = (
            None
            if partner_repository is None
            else await partner_repository.get_workspace(workspace_context.workspace_id)
        )
        if workspace is not None:
            # Название — да, workspace_id — нет: пользователю нужен человеко-
            # читаемый ориентир, а не внутренний идентификатор записи в БД.
            text = (
                f"Travel AI Orchestrator — рабочее пространство «{workspace.name}».\n\n"
                "Это ваше рабочее пространство: задачи, источники и материалы "
                "видны и доступны только внутри него.\n\n"
                "Выберите кнопку или напишите задачу текстом."
            )
            return text, True
        return WORKSPACE_UNAVAILABLE_TEXT, False
    if workspace_context_ambiguous:
        return WORKSPACE_AMBIGUOUS_TEXT, False
    return WORKSPACE_UNAVAILABLE_TEXT, False


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    state: FSMContext,
    v2_menu_enabled: bool = False,
    workspace_context: WorkspaceContext | None = None,
    workspace_context_ambiguous: bool = False,
    partner_repository: PartnerRepository | None = None,
) -> None:
    if v2_menu_enabled:
        await state.clear()
    text, workspace_available = await _resolve_start_reply(
        workspace_context, workspace_context_ambiguous, partner_repository
    )
    # Без рабочего пространства кнопки главного меню всё равно упрутся в тот же
    # отказ в каждом сценарии — показывать их означало бы вести в тупик.
    # Убираем и старую reply keyboard, если она успела остаться у пользователя.
    reply_markup = (
        active_main_menu(v2_menu_enabled)
        if workspace_available
        else ReplyKeyboardRemove()
    )
    await message.answer(text, reply_markup=reply_markup)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT, reply_markup=main_menu())


@router.message(F.text == BTN_HOW_IT_WORKS)
async def how_it_works(message: Message) -> None:
    await message.answer(HELP_TEXT, reply_markup=main_menu())
