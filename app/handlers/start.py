from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, ReplyKeyboardRemove

from app.domain.business_profiles import BusinessProfile
from app.domain.partners import WorkspaceContext
from app.handlers.lobby import is_access_granted, route_access_gate
from app.handlers.onboarding import enter_onboarding_gate
from app.keyboards import BTN_HOW_IT_WORKS, active_main_menu, main_menu
from app.repositories.partner_repository import (
    PartnerProvisioningConflictError,
    PartnerRepository,
)
from app.repositories.telegram_bind_token_repository import TelegramBindTokenRepository
from app.services.web_auth_tokens import hash_token

router = Router(name="start")

TELEGRAM_CONNECTED_TEXT = (
    "✅ ORCHESTRAVEL подключён к вашему рабочему пространству «{workspace_name}».\n\n"
    "Отправьте /start ещё раз, чтобы открыть рабочее меню."
)

TELEGRAM_BIND_FAILED_TEXT = (
    "Ссылка для подключения Telegram недействительна, уже использована или "
    "истекла.\n\n"
    "Откройте «Подключить Telegram» ещё раз в веб-кабинете, чтобы получить "
    "новую ссылку."
)

TELEGRAM_BIND_CONFLICT_TEXT = (
    "Этот Telegram-аккаунт уже подключён к другому рабочему пространству "
    "ORCHESTRAVEL."
)


async def _try_bind_telegram(
    token: str,
    telegram_user_id: int,
    partner_repository: PartnerRepository | None,
    telegram_bind_token_repository: TelegramBindTokenRepository | None,
) -> str:
    """Consumes a one-time Telegram-connect deep-link token (see POST
    /api/telegram/bind-token, app.repositories.telegram_bind_token_repository).
    Always returns a reply to show the user - never raises: any failure
    (unknown/expired/already-used token, or the incoming Telegram account
    already owning a different workspace) surfaces as a clear message,
    never a stack trace."""
    if partner_repository is None or telegram_bind_token_repository is None:
        return TELEGRAM_BIND_FAILED_TEXT
    consumed = await telegram_bind_token_repository.consume_token(
        hash_token(token), telegram_user_id,
    )
    if consumed is None:
        return TELEGRAM_BIND_FAILED_TEXT
    # Self-service workspaces start with a negative placeholder
    # telegram_user_id (-workspace_id) - see
    # provision_self_service_workspace/is_telegram_linked. The bind token
    # only ever targets such a workspace (POST /api/telegram/bind-token
    # refuses to issue one once real Telegram is already linked), so this
    # is the identity rebind_workspace_telegram_id renames FROM.
    placeholder_telegram_id = -consumed.workspace_id
    try:
        workspace = await partner_repository.get_workspace(consumed.workspace_id)
        await partner_repository.rebind_workspace_telegram_id(
            consumed.workspace_id, placeholder_telegram_id, telegram_user_id,
        )
    except PartnerProvisioningConflictError:
        return TELEGRAM_BIND_CONFLICT_TEXT
    except Exception:
        return TELEGRAM_BIND_FAILED_TEXT
    workspace_name = workspace.name if workspace is not None else ""
    return TELEGRAM_CONNECTED_TEXT.format(workspace_name=workspace_name)


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

# Рабочее пространство есть (workspace_context не None), но сам workspace не
# нашёлся при прямом lookup — данные рассинхронизированы. Fail-closed:
# подробности БД и идентификаторы наружу не раскрываются, решение — за
# администратором. "Нет workspace вообще" и "несколько workspace" теперь
# обрабатываются раньше, в route_access_gate (app/handlers/lobby.py) —
# Stage 3A показывает лобби, а не этот текст.
WORKSPACE_UNAVAILABLE_TEXT = (
    "Рабочее пространство для вашего аккаунта пока не подключено.\n\n"
    "Обратитесь к администратору сервиса, чтобы вам открыли доступ."
)


async def _resolve_start_reply(
    workspace_context: WorkspaceContext,
    partner_repository: PartnerRepository | None,
) -> tuple[str, bool]:
    """Возвращает (текст, доступен ли workspace для показа главного меню).

    Вызывается только когда workspace_context уже гарантированно не None —
    ветка "нет/неоднозначен workspace" обрабатывается раньше в cmd_start.
    """
    workspace = (
        None
        if partner_repository is None
        else await partner_repository.get_workspace(workspace_context.workspace_id)
    )
    if workspace is None:
        return WORKSPACE_UNAVAILABLE_TEXT, False
    # Название — да, workspace_id — нет: пользователю нужен человеко-
    # читаемый ориентир, а не внутренний идентификатор записи в БД.
    text = (
        f"Travel AI Orchestrator — рабочее пространство «{workspace.name}».\n\n"
        "Это ваше рабочее пространство: задачи, источники и материалы "
        "видны и доступны только внутри него.\n\n"
        "Выберите кнопку или напишите задачу текстом."
    )
    return text, True


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    state: FSMContext,
    command: CommandObject | None = None,
    v2_menu_enabled: bool = False,
    workspace_context: WorkspaceContext | None = None,
    workspace_context_ambiguous: bool = False,
    partner_repository: PartnerRepository | None = None,
    telegram_bind_token_repository: TelegramBindTokenRepository | None = None,
    onboarding_required: bool = False,
    onboarding_profile: BusinessProfile | None = None,
    access_state: str = "active",
) -> None:
    if v2_menu_enabled:
        await state.clear()

    # /start <token> - one-time Telegram-connect deep link from the web
    # cabinet's "Подключить Telegram" button (see POST
    # /api/telegram/bind-token). Handled BEFORE the workspace_context
    # branch below on purpose: a brand-new visitor opening this link has
    # workspace_context=None (computed by WorkspaceContextMiddleware
    # before this handler ever runs, from the OLD placeholder identity),
    # so binding first and replying immediately - rather than trying to
    # patch the already-computed workspace_context/access_state for this
    # same update - is the simplest correct behavior. The user is asked to
    # send /start again for the (now correctly resolved) main menu.
    token = (command.args or "").strip() if command is not None else ""
    if token:
        reply = await _try_bind_telegram(
            token, message.from_user.id, partner_repository, telegram_bind_token_repository,
        )
        await message.answer(reply)
        return

    # Stage 3A: публичный вход ≠ рабочий доступ. Без workspace (новый
    # посетитель) или с access_state, отличным от active/trial_active
    # (доступ истёк/приостановлен) — показываем лобби, а не рабочее меню и
    # не заводим workspace автоматически. workspace_context — главный
    # признак: он либо есть, либо нет, независимо от того, что подставлено
    # в access_state по умолчанию в тестах/старых вызовах.
    if workspace_context is None or not is_access_granted(access_state):
        await route_access_gate(
            message,
            workspace_context=workspace_context,
            workspace_context_ambiguous=workspace_context_ambiguous,
            access_state=access_state,
        )
        return

    # Централизованный gate (app/onboarding_gate.py) уже решил, обязателен ли
    # Business Onboarding для этого workspace — здесь только ролевая
    # развилка (владелец/админ заполняет, участник видит объяснение) и запуск
    # диалога. Обычный /start-текст ниже в этом случае не показывается.
    if onboarding_required and workspace_context is not None:
        await enter_onboarding_gate(message, state, workspace_context, onboarding_profile)
        return

    text, workspace_available = await _resolve_start_reply(
        workspace_context, partner_repository
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
