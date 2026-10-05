"""Stage 3A: pre-subscription lobby и публичный read-only раздел "Осмотреться".

Публичный слой ≠ рабочий доступ. Всё в этом модуле работает БЕЗ workspace,
БЕЗ подписки и без обращения к LLM/репозиториям рабочих данных — статичные
тексты и явные, изолированные заглушки для будущей оплаты (без создания
workspace и без обещания работающей оплаты).

Router зарегистрирован после start.router/consent.router и до
onboarding.router/menu.router (см. app/handlers/__init__.py): специфичные
кнопки лобби и "Осмотреться" перехватываются здесь первыми, а catch-all в
конце файла перехватывает всё остальное, пока access_state не
active/trial_active — тот же приём, что и в app/handlers/onboarding.py.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import MagicData
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove

from app.domain.partners import WorkspaceContext
from app.keyboards import (
    BROWSE_BACK,
    BROWSE_SECTION_PREFIX,
    BTN_LOBBY_BROWSE,
    BTN_LOBBY_EXTEND_ACCESS,
    BTN_LOBBY_SUBSCRIBE_MONTH,
    BTN_LOBBY_TRY_14_DAYS,
    BTN_LOBBY_WHATS_INCLUDED,
    browse_section_back_keyboard,
    browse_sections_keyboard,
    lobby_expired_keyboard,
    lobby_new_visitor_keyboard,
)
from app.services.access_state import ACTIVE, TRIAL_ACTIVE
from app.services.plans import list_plans

router = Router(name="lobby")


def _format_plan_line(plan) -> str:
    amount = int(plan.amount) if plan.amount == plan.amount.to_integral_value() else plan.amount
    return f"{amount} ₽ — {plan.label}"


def _plan_lines_text() -> str:
    return "\n".join(_format_plan_line(plan) for plan in list_plans())


# ── Тексты лобби ──────────────────────────────────────────────────────────

WELCOME_NEW_TEXT = (
    "Добро пожаловать в Travel AI Orchestrator.\n\n"
    "Можно сначала посмотреть, как всё устроено, а затем подключить рабочий доступ."
)

WELCOME_EXPIRED_TEXT = (
    "Рабочий доступ к Travel AI Orchestrator для этого пространства закончился.\n\n"
    "Можно снова осмотреться или продлить доступ."
)

# workspace + suspended: рабочие функции заблокированы, но это не "нет
# доступа вообще" (как у нового посетителя) и не "закончился срок" (как у
# expired) — отдельная, административная причина, без кнопок лобби.
SUSPENDED_TEXT = (
    "Доступ к рабочим функциям этого рабочего пространства временно "
    "приостановлен.\n\n"
    "Если это неожиданно — обратитесь к администратору сервиса."
)

WORKSPACE_AMBIGUOUS_TEXT = (
    "К вашему аккаунту привязано несколько рабочих пространств, поэтому мы не "
    "можем однозначно выбрать нужное автоматически.\n\n"
    "Обратитесь к администратору сервиса."
)

# Явная, изолированная заглушка: НИКАКОЙ реальной оплаты, НИКАКОГО workspace.
# Только для нового посетителя (workspace_context отсутствует) - для него
# ещё нет workspace, к которому можно привязать оплату.
PAYMENT_COMING_SOON_TEXT = (
    "Подключение скоро будет доступно.\n\n"
    "Сейчас вы можете познакомиться с возможностями Оркестратора — кнопка "
    "«🧭 Осмотреться»."
)

# RoboKassa billing (см. app/web_api.py) - оплата и продление подписки живут
# ТОЛЬКО в web-кабинете. Ссылка сознательно НЕ содержит workspace_id/токен:
# workspace определяется server-side из web-сессии (email+пароль,
# app.web_api.get_current_principal), тот же принцип, что и у любого
# billing-эндпоинта. Отдельный signed-token flow для прямого перехода из
# Telegram без повторного логина реализован для обратного направления
# (веб -> Telegram) - см. POST /api/telegram/bind-token и
# app.handlers.start's /start <token>; здесь, для перехода Telegram -> веб,
# по-прежнему обычный логин по email/паролю.
WEB_BILLING_URL = "https://app.orchestravel.ru/billing"

PAYMENT_RENEW_TEXT = (
    "Оплатить или продлить подписку можно в веб-кабинете ORCHESTRAVEL:\n"
    f"{WEB_BILLING_URL}\n\n"
    "Войдите под своим email и паролем от веб-кабинета — рабочее "
    "пространство определится автоматически, оплата откроет доступ сразу "
    "и здесь, и в веб-кабинете."
)

WHATS_INCLUDED_TEXT = (
    "💳 Что входит в подписку\n\n"
    "Подписка — это рабочий доступ к Оркестратору на нашей инфраструктуре: "
    "сигналы и идеи, создание материалов, ответы клиентам, работа с "
    "источниками и конкурентами, профиль и персонализация — без необходимости "
    "отдельно подключать CRM, конструкторы ботов или Make/n8n.\n\n"
    "Сначала — платный ознакомительный доступ на 14 дней (один раз на рабочее "
    "пространство, без автоматического перехода на месяц), затем — месячная "
    "подписка.\n\n"
    f"{_plan_lines_text()}"
)

BROWSE_INTRO_TEXT = "🧭 Осмотреться\n\nВыберите раздел, чтобы посмотреть, как всё устроено."

_BROWSE_SECTIONS: dict[str, tuple[str, str]] = {
    "start": (
        "🚀 С чего начать",
        "После подключения рабочего доступа вы получаете собственное рабочее "
        "пространство — Telegram-ассистент для контента и коммуникаций. Дальше "
        "— короткая анкета о вашем бизнесе, и можно сразу работать.",
    ),
    "signals": (
        "📡 Сигналы и идеи",
        "Оркестратор следит за подключёнными источниками (Telegram, VK, RSS) и "
        "подбирает вопросы клиентов, рыночные новости и темы для контента — без "
        "потока лишнего шума.",
    ),
    "materials": (
        "✍️ Создание материалов",
        "Черновик поста, ответа или материала — по одной кнопке или обычным "
        "текстом. Черновики всегда требуют вашей проверки перед публикацией — "
        "ничего не публикуется автоматически.",
    ),
    "replies": (
        "💬 Ответы клиентам",
        "Помощь в подготовке аккуратного ответа на вопрос клиента — с учётом "
        "вашего бизнеса и тона общения. Отправка — всегда вручную, "
        "автоматической рассылки нет.",
    ),
    "sources": (
        "📚 Источники",
        "Вы сами выбираете, какие каналы и сообщества мониторить — под ваши "
        "направления и аудиторию.",
    ),
    "competitors": (
        "👀 Конкуренты",
        "Отдельно можно наблюдать за конкурентами: что публикуют, какие темы "
        "поднимают — без интеграции с их аккаунтами и без автоматических "
        "действий.",
    ),
    "profile": (
        "⚙️ Профиль и персонализация",
        "Название, специализация, регион, аудитория, стиль общения — всё это "
        "настраивается один раз в профиле и используется во всех материалах.",
    ),
    "access": (
        "💳 Как устроен доступ",
        "Базовая подписка работает на нашей инфраструктуре — отдельная CRM, "
        "Make/n8n или другой обязательный платный сервис не нужен. Бесплатного "
        "пробного периода пока нет: активная работа сразу создаёт "
        "инфраструктурные и AI/API-расходы. Вместо этого — платный "
        "ознакомительный доступ на 14 дней, затем месячная подписка:\n\n"
        f"{_plan_lines_text()}",
    ),
    "hosting": (
        "🖥 Свой сервер или подписка",
        "Подписка — рабочий доступ на нашей инфраструктуре, без отдельной "
        "настройки. Если нужно разместить решение на собственном сервере — это "
        "отдельный вариант с разовой стоимостью развёртывания: VPS и API в "
        "этом случае оплачивает клиент, обычная подписка не нужна, а "
        "дальнейшее сопровождение — самостоятельно или отдельным платным "
        "договором.",
    ),
    "faq": (
        "❓ Частые вопросы",
        "Почему нет бесплатного пробного периода? — Активная работа сразу "
        "создаёт расходы на инфраструктуру и AI/API, поэтому знакомство — "
        "через этот раздел, а рабочий доступ — платный.\n\n"
        "Нужна ли отдельная CRM или Make/n8n? — Нет, базовая работа их не "
        "требует.\n\n"
        "Можно продлевать 14-дневный доступ повторно? — Нет, это разовое "
        "ознакомление на workspace, дальше — месячная подписка.",
    ),
}


# ── Публичные функции для переиспользования из app/handlers/start.py ────────


async def send_new_visitor_lobby(message: Message) -> None:
    await message.answer(WELCOME_NEW_TEXT, reply_markup=lobby_new_visitor_keyboard())


async def send_expired_lobby(message: Message) -> None:
    await message.answer(WELCOME_EXPIRED_TEXT, reply_markup=lobby_expired_keyboard())


async def send_suspended_message(message: Message) -> None:
    await message.answer(SUSPENDED_TEXT, reply_markup=ReplyKeyboardRemove())


async def send_ambiguous_workspace_message(message: Message) -> None:
    await message.answer(WORKSPACE_AMBIGUOUS_TEXT, reply_markup=ReplyKeyboardRemove())


async def route_access_gate(
    message: Message,
    *,
    workspace_context,
    workspace_context_ambiguous: bool,
    access_state: str,
) -> None:
    """Единая точка решения "что показать", когда рабочий Оркестратор

    недоступен — используется и из cmd_start, и из catch-all этого модуля,
    чтобы ветвление не дублировалось в двух местах (тот же приём, что
    enter_onboarding_gate в app/handlers/onboarding.py).
    """
    if workspace_context is None:
        if workspace_context_ambiguous:
            await send_ambiguous_workspace_message(message)
            return
        await send_new_visitor_lobby(message)
        return
    if access_state == "suspended":
        await send_suspended_message(message)
        return
    # "expired" и любое неизвестное состояние (fail-safe) — тот же экран.
    await send_expired_lobby(message)


def is_access_granted(access_state: str) -> bool:
    return access_state in (ACTIVE, TRIAL_ACTIVE)


# ── Кнопки лобби (специфичные хендлеры — до catch-all) ──────────────────────


@router.message(F.text == BTN_LOBBY_BROWSE)
async def on_browse_pressed(message: Message) -> None:
    await message.answer(BROWSE_INTRO_TEXT, reply_markup=browse_sections_keyboard())


@router.callback_query(F.data.startswith(BROWSE_SECTION_PREFIX))
async def on_browse_section_selected(callback: CallbackQuery) -> None:
    key = (callback.data or "").removeprefix(BROWSE_SECTION_PREFIX)
    section = _BROWSE_SECTIONS.get(key)
    await callback.answer()
    if callback.message is None:
        return
    if section is None:
        await callback.message.edit_text(
            BROWSE_INTRO_TEXT, reply_markup=browse_sections_keyboard()
        )
        return
    title, body = section
    await callback.message.edit_text(
        f"{title}\n\n{body}", reply_markup=browse_section_back_keyboard()
    )


@router.callback_query(F.data == BROWSE_BACK)
async def on_browse_back(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message is None:
        return
    await callback.message.edit_text(
        BROWSE_INTRO_TEXT, reply_markup=browse_sections_keyboard()
    )


@router.message(F.text.in_({
    BTN_LOBBY_TRY_14_DAYS, BTN_LOBBY_SUBSCRIBE_MONTH, BTN_LOBBY_EXTEND_ACCESS,
}))
async def on_payment_stub_pressed(
    message: Message, workspace_context: WorkspaceContext | None = None,
) -> None:
    """workspace_context присутствует -> это существующий workspace с
    закрытым доступом (expired/past_due/suspended), для него уже есть
    конкретное, безопасное действие: ссылка на web billing. Его
    отсутствие -> совсем новый посетитель без workspace - для него оплата
    пока действительно недоступна (ничего не активирует, workspace не
    создаёт), поэтому остаётся прежняя заглушка."""
    if workspace_context is not None:
        await message.answer(PAYMENT_RENEW_TEXT, disable_web_page_preview=True)
        return
    await message.answer(PAYMENT_COMING_SOON_TEXT)


@router.message(F.text == BTN_LOBBY_WHATS_INCLUDED)
async def on_whats_included_pressed(message: Message) -> None:
    await message.answer(WHATS_INCLUDED_TEXT)


# ── Centralized gate: перехватывает всё остальное, пока доступ не активен ───


@router.message(
    MagicData(~F.access_state.in_({ACTIVE, TRIAL_ACTIVE})),
    F.text & ~F.text.startswith("/"),
)
async def on_access_gate_intercept(
    message: Message,
    workspace_context=None,
    workspace_context_ambiguous: bool = False,
    access_state: str = "no_workspace",
) -> None:
    await route_access_gate(
        message,
        workspace_context=workspace_context,
        workspace_context_ambiguous=workspace_context_ambiguous,
        access_state=access_state,
    )


@router.callback_query(MagicData(~F.access_state.in_({ACTIVE, TRIAL_ACTIVE})))
async def on_access_gate_intercept_callback(callback: CallbackQuery) -> None:
    """Устаревший/сторонний callback (кнопка, показанная до истечения доступа
    или отправки /start) — ничего не мутирует, просто просит продолжить через
    /start, как и аналогичный catch-all в app/handlers/onboarding.py."""
    await callback.answer(
        "Этот шаг уже не актуален. Отправьте /start, чтобы продолжить.",
        show_alert=True,
    )
