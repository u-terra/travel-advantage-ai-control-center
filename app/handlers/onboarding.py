"""Обязательный Business Onboarding: первый вход post-rollout workspace.

Это онбординг WORKSPACE/бизнеса — переиспользует существующий BusinessProfile
и BusinessProfileService, а не отдельная система профилей. Спрашивает
СТРОГО отсутствующие поля (business_name, для independent — business_type,
short_description, specializations) и всегда обновляет уже существующую
строку partner_profiles через update() — profile_status='incomplete' для
post-rollout workspace означает, что строка уже создана provisioning'ом,
просто не полностью заполнена; create() здесь не нужен и не вызывается.

practical_stage / user display name / полное редактирование профиля здесь
сознательно не реализуются — это отдельные, намеренно отложенные решения
(см. product-аудит по этому этапу).

Centralized gate: OnboardingGateMiddleware (app/onboarding_gate.py) кладёт
onboarding_required/onboarding_profile в workflow data на каждый update.
Роутер этого модуля зарегистрирован после start.router/consent.router и до
menu/daily_actions/tasks — специфичные FSM-хендлеры перехватывают ответы на
текущий вопрос, а catch-all в конце файла перехватывает вообще всё остальное
(кнопки v2-меню, «Что делать сегодня», свободный текст), пока онбординг не
завершён. AllowlistMiddleware/WorkspaceContextMiddleware не меняются.
"""

from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.filters import MagicData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove

from app.domain.business_profiles import BusinessProfile
from app.domain.partners import WorkspaceContext
from app.handlers.daily_actions import show_daily_actions
from app.keyboards import (
    ONBOARDING_BUSINESS_TYPE_PREFIX,
    onboarding_business_type_keyboard,
    v2_back_keyboard,
)
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.partner_repository import PartnerRepository, business_context_to_dict
from app.repositories.work_repository import WorkRepository
from app.repositories.workspace_signal_repository import WorkspaceSignalRepository
from app.services.business_profile_context import BusinessProfileAccessError, BusinessProfileService

router = Router(name="onboarding")

_INDEPENDENT_BUSINESS_TYPES = frozenset({"independent_agent", "agency", "travel_company"})
_FIELD_ORDER = ("business_name", "business_type", "short_description", "specializations")

_UNAVAILABLE_TEXT = "Рабочее пространство недоступно."
_PROFILE_MISSING_TEXT = "Профиль бизнеса ещё не создан. Обратитесь к администратору сервиса."
_MEMBER_BLOCKED_TEXT = (
    "Профиль этого рабочего пространства ещё не заполнен полностью.\n\n"
    "Завершить его может только владелец или администратор workspace — "
    "попросите его открыть бота и ответить на несколько коротких вопросов."
)
_STALE_TEXT = "Этот шаг уже не актуален. Отправьте /start, чтобы продолжить."
_ONBOARDING_INTRO_TEXT = "Прежде чем начать, заполним несколько коротких вопросов о вашем бизнесе."
_ONBOARDING_COMPLETE_TEXT = "Профиль сохранён. Вот с чего можно начать сегодня — «☀️ Что делать сегодня»:"

_FIELD_PROMPTS: dict[str, str] = {
    "business_name": "Как назвать ваш проект или страницу?",
    "short_description": (
        "Расскажите в 1–2 предложениях, чем вы занимаетесь и чем можете быть "
        "полезны клиентам."
    ),
    "specializations": (
        "Назовите 1–3 направления, на которых хотите сосредоточиться.\n"
        "Например: семейные путешествия, круизы, индивидуальные маршруты."
    ),
}
_BUSINESS_TYPE_PROMPT = "Как лучше вас описать?"

_INVALID_ANSWER_TEXTS: dict[str, str] = {
    "business_name": "Название не должно быть пустым. Как назвать ваш проект или страницу?",
    "short_description": "Опишите в 1–2 предложениях, чем вы занимаетесь и чем можете быть полезны.",
    "specializations": "Укажите хотя бы одно направление, например: семейные путешествия.",
}


class BusinessOnboarding(StatesGroup):
    waiting_for_field = State()


def _is_field_missing(profile: BusinessProfile, field: str) -> bool:
    if field == "business_name":
        return not profile.business_name.strip()
    if field == "business_type":
        return (
            not profile.ta_affiliated
            and profile.business_type not in _INDEPENDENT_BUSINESS_TYPES
        )
    if field == "short_description":
        return not profile.short_description.strip()
    if field == "specializations":
        return not profile.context.specializations
    raise ValueError(f"Неизвестное поле onboarding: {field!r}")


def missing_onboarding_fields(profile: BusinessProfile) -> list[str]:
    """Строго недостающие поля, в фиксированном порядке. TA (ta_affiliated=
    True) никогда не получает 'business_type' — оно уже задано provisioning'ом
    и не входит в пользовательский onboarding-диалог."""
    return [field for field in _FIELD_ORDER if _is_field_missing(profile, field)]


def _parse_specializations(raw: str) -> list[str]:
    parts = re.split(r"[,;]", raw)
    return [part.strip() for part in parts if part.strip()]


async def _ask(message: Message, field: str) -> None:
    if field == "business_type":
        await message.answer(_BUSINESS_TYPE_PROMPT, reply_markup=onboarding_business_type_keyboard())
        return
    await message.answer(_FIELD_PROMPTS[field], reply_markup=ReplyKeyboardRemove())


async def show_member_blocked_message(message: Message) -> None:
    """Публичная обёртка над _MEMBER_BLOCKED_TEXT — используется и здесь, и
    из app/handlers/start.py, чтобы формулировка не расходилась и не
    импортировался приватный модульный константный текст напрямую."""
    await message.answer(_MEMBER_BLOCKED_TEXT, reply_markup=ReplyKeyboardRemove())


async def enter_onboarding_gate(
    message: Message,
    state: FSMContext,
    workspace_context: WorkspaceContext,
    profile: BusinessProfile | None,
) -> None:
    """Единая точка входа с ролевой развилкой — используется и из
    cmd_start (app/handlers/start.py), и из catch-all этого модуля, чтобы
    проверка role не дублировалась в двух местах."""
    if workspace_context.role not in {"owner", "admin"}:
        await show_member_blocked_message(message)
        return
    await start_business_onboarding(message, state, profile)


async def start_business_onboarding(
    message: Message, state: FSMContext, profile: BusinessProfile | None,
) -> None:
    """Точка входа: вызывается и из cmd_start (app/handlers/start.py), и из
    catch-all этого модуля — единая логика «спросить первый недостающий
    вопрос», ничего не дублируется между входами."""
    if profile is None:
        await state.clear()
        await message.answer(_PROFILE_MISSING_TEXT, reply_markup=ReplyKeyboardRemove())
        return
    missing = missing_onboarding_fields(profile)
    if not missing:
        await state.clear()
        return
    field = missing[0]
    await state.set_state(BusinessOnboarding.waiting_for_field)
    await state.update_data(field=field)
    await message.answer(_ONBOARDING_INTRO_TEXT)
    await _ask(message, field)


async def _continue_or_finish(
    message: Message,
    state: FSMContext,
    profile: BusinessProfile,
    workspace_context: WorkspaceContext,
    partner_repository: PartnerRepository,
    work_repository: WorkRepository,
    artifact_repository: ArtifactRepository,
    workspace_signal_repository: WorkspaceSignalRepository | None,
) -> None:
    missing = missing_onboarding_fields(profile)
    if missing:
        field = missing[0]
        await state.update_data(field=field)
        await _ask(message, field)
        return
    # profile_status == 'usable' теперь гарантирован существующим
    # _profile_status() внутри update_business_profile — отдельного флага
    # «онбординг завершён» не заводим.
    await state.clear()
    await message.answer(_ONBOARDING_COMPLETE_TEXT, reply_markup=v2_back_keyboard())
    await show_daily_actions(
        message, workspace_context, work_repository, partner_repository,
        artifact_repository, workspace_signal_repository,
    )


@router.message(BusinessOnboarding.waiting_for_field, F.text & ~F.text.startswith("/"))
async def on_onboarding_field_answer(
    message: Message,
    state: FSMContext,
    workspace_context: WorkspaceContext | None,
    partner_repository: PartnerRepository,
    work_repository: WorkRepository,
    artifact_repository: ArtifactRepository,
    workspace_signal_repository: WorkspaceSignalRepository | None = None,
) -> None:
    data = await state.get_data()
    field = data.get("field")
    if field == "business_type":
        # business_type отвечают кнопкой (on_onboarding_business_type_selected),
        # не свободным текстом.
        await message.answer(_BUSINESS_TYPE_PROMPT, reply_markup=onboarding_business_type_keyboard())
        return
    if workspace_context is None:
        await state.clear()
        await message.answer(_UNAVAILABLE_TEXT, reply_markup=ReplyKeyboardRemove())
        return

    profile = await partner_repository.get_business_profile(workspace_context.workspace_id)
    if profile is None:
        await state.clear()
        await message.answer(_PROFILE_MISSING_TEXT, reply_markup=ReplyKeyboardRemove())
        return

    raw = (message.text or "").strip()
    context_dict = business_context_to_dict(profile.context)
    business_name = profile.business_name
    short_description = profile.short_description

    if field == "business_name":
        if not raw:
            await message.answer(_INVALID_ANSWER_TEXTS["business_name"])
            return
        business_name = raw
    elif field == "short_description":
        if not raw:
            await message.answer(_INVALID_ANSWER_TEXTS["short_description"])
            return
        short_description = raw
    elif field == "specializations":
        items = _parse_specializations(raw)
        if not items:
            await message.answer(_INVALID_ANSWER_TEXTS["specializations"])
            return
        context_dict["specializations"] = items
    else:
        await state.clear()
        await message.answer(_PROFILE_MISSING_TEXT, reply_markup=ReplyKeyboardRemove())
        return

    try:
        updated = await BusinessProfileService(partner_repository).update(
            workspace_context, profile.revision,
            business_name=business_name, business_type=profile.business_type,
            short_description=short_description, context=context_dict,
        )
    except BusinessProfileAccessError:
        await state.clear()
        await show_member_blocked_message(message)
        return

    await _continue_or_finish(
        message, state, updated, workspace_context, partner_repository,
        work_repository, artifact_repository, workspace_signal_repository,
    )


@router.callback_query(
    BusinessOnboarding.waiting_for_field, F.data.startswith(ONBOARDING_BUSINESS_TYPE_PREFIX),
)
async def on_onboarding_business_type_selected(
    callback: CallbackQuery,
    state: FSMContext,
    workspace_context: WorkspaceContext | None,
    partner_repository: PartnerRepository,
    work_repository: WorkRepository,
    artifact_repository: ArtifactRepository,
    workspace_signal_repository: WorkspaceSignalRepository | None = None,
) -> None:
    business_type = (callback.data or "").removeprefix(ONBOARDING_BUSINESS_TYPE_PREFIX)
    if business_type not in _INDEPENDENT_BUSINESS_TYPES or workspace_context is None:
        await callback.answer(_STALE_TEXT, show_alert=True)
        return

    profile = await partner_repository.get_business_profile(workspace_context.workspace_id)
    if profile is None:
        await callback.answer(_STALE_TEXT, show_alert=True)
        return

    context_dict = business_context_to_dict(profile.context)
    try:
        updated = await BusinessProfileService(partner_repository).update(
            workspace_context, profile.revision,
            business_name=profile.business_name, business_type=business_type,
            short_description=profile.short_description, context=context_dict,
        )
    except BusinessProfileAccessError:
        await state.clear()
        await callback.answer(_STALE_TEXT, show_alert=True)
        if callback.message is not None:
            await show_member_blocked_message(callback.message)
        return

    await callback.answer()
    if callback.message is None:
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await _continue_or_finish(
        callback.message, state, updated, workspace_context, partner_repository,
        work_repository, artifact_repository, workspace_signal_repository,
    )


# ── Centralized gate: перехватывает всё остальное, пока онбординг не завершён ─


@router.message(MagicData(F.onboarding_required), F.text & ~F.text.startswith("/"))
async def on_onboarding_gate_intercept(
    message: Message,
    state: FSMContext,
    workspace_context: WorkspaceContext | None,
    onboarding_profile: BusinessProfile | None = None,
) -> None:
    """Любой другой текст (кнопка v2-меню, «Что делать сегодня», свободный
    запрос в task-хендлеры) при незавершённом обязательном онбординге —
    перенаправляется сюда же, а не в исходный handler. Сам FSM-диалог этот
    catch-all не перехватывает: BusinessOnboarding.waiting_for_field-хендлер
    выше зарегистрирован раньше и совпадает первым."""
    if workspace_context is None:
        return
    await enter_onboarding_gate(message, state, workspace_context, onboarding_profile)


@router.callback_query(MagicData(F.onboarding_required))
async def on_onboarding_gate_intercept_callback(callback: CallbackQuery) -> None:
    """Устаревшие/сторонние callback (например, кнопка daily_actions,
    показанная до того, как онбординг стал обязательным) — не мутируют
    ничего, просто просят продолжить через /start."""
    await callback.answer(_STALE_TEXT, show_alert=True)
