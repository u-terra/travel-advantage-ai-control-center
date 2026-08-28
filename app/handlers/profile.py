"""Stage 3B1: «⚙️ Профиль и персонализация» — self-service центр настройки.

Два уровня персонализации, которые нельзя путать:
- профиль КОМПАНИИ/workspace (BusinessProfile/BusinessContext) — пишет
  только owner/admin, читает любой активный участник;
- личный стиль КОНКРЕТНОГО пользователя (WorkspaceUserPreferences через
  UserStyleService) — каждый активный участник читает/пишет только свою
  запись, независимо от роли.

FSM — тот же принцип, что и app/handlers/onboarding.py (один вопрос за раз,
state хранит, что именно редактируется), но как self-service меню с выбором
поля, а не форсированная последовательность: правка одного поля не требует
заново проходить все остальные.
"""

from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.filters import MagicData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.domain.business_profiles import BusinessProfile
from app.domain.partners import WorkspaceContext, WorkspaceUserPreferences
from app.keyboards import (
    BTN_V2_PROFILE,
    ONBOARDING_BUSINESS_TYPE_PREFIX,
    PROFILE_EXAMPLES_PREFIX,
    PROFILE_FIELD_PREFIX,
    PROFILE_MENU_PREFIX,
    onboarding_business_type_keyboard,
    profile_back_keyboard,
    profile_company_fields_keyboard,
    profile_examples_keyboard,
    profile_menu_keyboard,
    v2_back_keyboard,
)
from app.repositories.partner_repository import PartnerRepository, business_context_to_dict
from app.repositories.partner_repository import TooManyUserExamplesError
from app.services.business_profile_context import BusinessProfileAccessError, BusinessProfileService
from app.services.user_style import UserStyleAccessError, UserStyleService

router = Router(name="profile")

_UNAVAILABLE = "Рабочее пространство недоступно."
_PROFILE_MISSING = (
    "Профиль бизнеса ещё не заполнен.\n\n"
    "Обратитесь к администратору сервиса, чтобы его настроить."
)
_COMPANY_WRITE_BLOCKED = (
    "Изменять профиль компании может только владелец или администратор "
    "рабочего пространства."
)
_STALE_TEXT = "Этот шаг уже не актуален. Откройте «⚙️ Профиль» ещё раз."

_BUSINESS_TYPE_LABELS: dict[str, str] = {
    "independent_agent": "независимый агент",
    "club_partner": "клубный партнёр",
    "agency": "турагентство",
    "travel_company": "туроператор",
    "other": "другое",
}

_MENU_INTRO = "⚙️ Профиль и персонализация\n\nВыберите, что настроить."

_COMPANY_INTRO = "🏢 Профиль компании\n\nВыберите поле, чтобы посмотреть или изменить его."

_STYLE_EXPLANATION = (
    "✍️ Мой стиль общения\n\n"
    "Напишите свободно, как вы обычно общаетесь с клиентами и подписчиками.\n"
    "Например: пишу просто, немного с юмором, обращаюсь на «вы», не люблю "
    "рекламные штампы."
)

_EXAMPLE_EXPLANATION = (
    "📝 Пришлите текст одного вашего поста или сообщения как есть — это "
    "поможет Оркестратору писать в вашей манере. Можно добавить до 5 примеров."
)

_AVOID_EXPLANATION = (
    "🚫 Чего не использовать\n\n"
    "Перечислите слова и обороты, которых не должно быть в текстах — через "
    "запятую или с новой строки.\n"
    "Например: уникальное предложение, успейте купить, лучший тур."
)

_COMPANY_FIELD_PROMPTS: dict[str, str] = {
    "business_name": "Как называется ваш бизнес?",
    "short_description": "Опишите в 1–2 предложениях, чем вы занимаетесь.",
    "specializations": "Перечислите специализации через запятую или с новой строки.",
    "destinations": "Перечислите направления через запятую или с новой строки.",
    "region": "В каком регионе вы работаете? Например: Москва и область, вся Россия.",
    "audiences": "Опишите целевую аудиторию через запятую или с новой строки.",
    "tone": (
        "Опишите стиль общения компании — как компания обращается к клиентам "
        "публично (это отдельно от вашего личного стиля)."
    ),
}
_LIST_COMPANY_FIELDS = frozenset({"specializations", "destinations", "audiences"})


class ProfileEdit(StatesGroup):
    waiting_for_company_field = State()
    waiting_for_business_type = State()
    waiting_for_style_description = State()
    waiting_for_example_post = State()
    waiting_for_avoid_phrases = State()


def _parse_list(raw: str) -> list[str]:
    parts = re.split(r"[,;\n]", raw)
    return [part.strip() for part in parts if part.strip()]


def _joined(values: tuple[str, ...]) -> str | None:
    cleaned = [value.strip() for value in values if value.strip()]
    return ", ".join(cleaned) if cleaned else None


def _profile_text(profile: BusinessProfile) -> str:
    context = profile.context
    lines: list[str] = ["👁 Профиль бизнеса", ""]

    business_name = profile.business_name.strip()
    if business_name:
        lines.append(f"Название: {business_name}")

    type_label = _BUSINESS_TYPE_LABELS.get(profile.business_type, profile.business_type)
    lines.append(f"Тип бизнеса: {type_label}")

    short_description = profile.short_description.strip()
    if short_description:
        lines.append(f"Описание: {short_description}")

    specializations = _joined(context.specializations)
    if specializations:
        lines.append(f"Специализации: {specializations}")

    destinations = _joined(context.destinations)
    if destinations:
        lines.append(f"Направления: {destinations}")

    if context.region.strip():
        lines.append(f"Регион работы: {context.region.strip()}")

    audiences = _joined(context.audiences)
    if audiences:
        lines.append(f"Аудитории: {audiences}")

    tone = str(context.communication.get("tone") or "").strip()
    if tone:
        lines.append(f"Стиль общения компании: {tone}")

    contact_parts = [
        f"{key}: {value.strip()}"
        for key, value in context.public_contacts.items()
        if value.strip()
    ]
    if contact_parts:
        lines.append("Контакты: " + "; ".join(contact_parts))

    verified_claims = [
        claim.text.strip()
        for claim in context.claims
        if claim.verification_status == "verified" and claim.text.strip()
    ]
    if verified_claims:
        lines.append("Подтверждённые факты: " + "; ".join(verified_claims))

    if profile.profile_status == "incomplete":
        lines.append("")
        lines.append("Профиль заполнен частично — персонализация пока ограничена.")

    return "\n".join(lines)


def _personal_style_text(preferences: WorkspaceUserPreferences | None) -> str:
    lines = ["", "— Личный стиль —"]
    if preferences is None or not preferences.style_description.strip():
        lines.append("Стиль общения: не заполнен.")
    else:
        lines.append(f"Стиль общения: {preferences.style_description.strip()}")
    count = 0 if preferences is None else len(preferences.example_posts)
    lines.append(f"Примеров текстов сохранено: {count} из 5.")
    if preferences is not None and preferences.avoid_phrases:
        lines.append("Не использовать: " + ", ".join(preferences.avoid_phrases))
    else:
        lines.append("Не использовать: список пуст.")
    return "\n".join(lines)


async def _show_menu(message: Message) -> None:
    await message.answer(_MENU_INTRO, reply_markup=profile_menu_keyboard())


@router.message(MagicData(F.v2_menu_enabled), F.text == BTN_V2_PROFILE)
async def show_profile_menu(
    message: Message,
    state: FSMContext,
    workspace_context: WorkspaceContext | None,
) -> None:
    await state.clear()
    if workspace_context is None:
        await message.answer(_UNAVAILABLE, reply_markup=v2_back_keyboard())
        return
    await _show_menu(message)


@router.callback_query(F.data == f"{PROFILE_MENU_PREFIX}root")
async def on_profile_menu_root(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    if callback.message is not None:
        await callback.message.edit_text(_MENU_INTRO, reply_markup=profile_menu_keyboard())


@router.callback_query(F.data == f"{PROFILE_MENU_PREFIX}view")
async def on_profile_view(
    callback: CallbackQuery,
    workspace_context: WorkspaceContext | None,
    partner_repository: PartnerRepository,
) -> None:
    await callback.answer()
    if callback.message is None or workspace_context is None:
        return
    profile = await partner_repository.get_business_profile(workspace_context.workspace_id)
    if profile is None:
        await callback.message.edit_text(_PROFILE_MISSING, reply_markup=profile_back_keyboard())
        return
    preferences = await UserStyleService(partner_repository).get(workspace_context)
    text = _profile_text(profile) + "\n" + _personal_style_text(preferences)
    await callback.message.edit_text(text, reply_markup=profile_back_keyboard())


# ── 🏢 Профиль компании ──────────────────────────────────────────────────────


@router.callback_query(F.data == f"{PROFILE_MENU_PREFIX}company")
async def on_profile_company_menu(
    callback: CallbackQuery,
    workspace_context: WorkspaceContext | None,
    partner_repository: PartnerRepository,
) -> None:
    await callback.answer()
    if callback.message is None or workspace_context is None:
        return
    profile = await partner_repository.get_business_profile(workspace_context.workspace_id)
    if profile is None:
        await callback.message.edit_text(_PROFILE_MISSING, reply_markup=profile_back_keyboard())
        return
    await callback.message.edit_text(
        _COMPANY_INTRO,
        reply_markup=profile_company_fields_keyboard(show_business_type=not profile.ta_affiliated),
    )


@router.callback_query(F.data.startswith(PROFILE_FIELD_PREFIX))
async def on_profile_field_selected(
    callback: CallbackQuery,
    state: FSMContext,
    workspace_context: WorkspaceContext | None,
    partner_repository: PartnerRepository,
) -> None:
    field = (callback.data or "").removeprefix(PROFILE_FIELD_PREFIX)
    if field not in _COMPANY_FIELD_PROMPTS and field != "business_type":
        await callback.answer(_STALE_TEXT, show_alert=True)
        return
    await callback.answer()
    if callback.message is None or workspace_context is None:
        return
    if workspace_context.role not in {"owner", "admin"}:
        await callback.message.edit_text(_COMPANY_WRITE_BLOCKED, reply_markup=profile_back_keyboard())
        return
    profile = await partner_repository.get_business_profile(workspace_context.workspace_id)
    if profile is None:
        await callback.message.edit_text(_PROFILE_MISSING, reply_markup=profile_back_keyboard())
        return

    if field == "business_type":
        if profile.ta_affiliated:
            await callback.answer(_STALE_TEXT, show_alert=True)
            return
        await state.set_state(ProfileEdit.waiting_for_business_type)
        await callback.message.edit_text(
            "Как лучше вас описать?", reply_markup=onboarding_business_type_keyboard(),
        )
        return

    await state.set_state(ProfileEdit.waiting_for_company_field)
    await state.update_data(field=field)
    current = _current_field_value(profile, field)
    prompt = _COMPANY_FIELD_PROMPTS[field]
    if current:
        prompt = f"Сейчас: {current}\n\n{prompt}"
    await callback.message.edit_text(prompt)


def _current_field_value(profile: BusinessProfile, field: str) -> str:
    if field == "business_name":
        return profile.business_name
    if field == "short_description":
        return profile.short_description
    if field == "specializations":
        return _joined(profile.context.specializations) or ""
    if field == "destinations":
        return _joined(profile.context.destinations) or ""
    if field == "region":
        return profile.context.region
    if field == "audiences":
        return _joined(profile.context.audiences) or ""
    if field == "tone":
        return str(profile.context.communication.get("tone") or "")
    return ""


async def _apply_company_field_and_save(
    partner_repository: PartnerRepository,
    workspace_context: WorkspaceContext,
    profile: BusinessProfile,
    field: str,
    raw_value: str,
) -> BusinessProfile:
    context_dict = business_context_to_dict(profile.context)
    business_name = profile.business_name
    short_description = profile.short_description
    if field == "business_name":
        business_name = raw_value
    elif field == "short_description":
        short_description = raw_value
    elif field in _LIST_COMPANY_FIELDS:
        context_dict[field] = _parse_list(raw_value)
    elif field == "region":
        context_dict["region"] = raw_value
    elif field == "tone":
        context_dict["communication"]["tone"] = raw_value
    return await BusinessProfileService(partner_repository).update(
        workspace_context, profile.revision,
        business_name=business_name, business_type=profile.business_type,
        short_description=short_description, context=context_dict,
    )


@router.message(ProfileEdit.waiting_for_company_field, F.text & ~F.text.startswith("/"))
async def on_company_field_answer(
    message: Message,
    state: FSMContext,
    workspace_context: WorkspaceContext | None,
    partner_repository: PartnerRepository,
) -> None:
    data = await state.get_data()
    field = data.get("field")
    if workspace_context is None or field not in _COMPANY_FIELD_PROMPTS:
        await state.clear()
        await message.answer(_STALE_TEXT)
        return
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Значение не должно быть пустым. Попробуйте ещё раз.")
        return
    profile = await partner_repository.get_business_profile(workspace_context.workspace_id)
    if profile is None:
        await state.clear()
        await message.answer(_PROFILE_MISSING)
        return
    try:
        await _apply_company_field_and_save(
            partner_repository, workspace_context, profile, field, raw,
        )
    except BusinessProfileAccessError:
        await state.clear()
        await message.answer(_COMPANY_WRITE_BLOCKED)
        return
    await state.clear()
    await message.answer("Сохранено.")
    await _show_menu(message)


@router.callback_query(
    ProfileEdit.waiting_for_business_type, F.data.startswith(ONBOARDING_BUSINESS_TYPE_PREFIX),
)
async def on_profile_business_type_selected(
    callback: CallbackQuery,
    state: FSMContext,
    workspace_context: WorkspaceContext | None,
    partner_repository: PartnerRepository,
) -> None:
    business_type = (callback.data or "").removeprefix(ONBOARDING_BUSINESS_TYPE_PREFIX)
    await state.clear()
    if workspace_context is None:
        await callback.answer(_STALE_TEXT, show_alert=True)
        return
    profile = await partner_repository.get_business_profile(workspace_context.workspace_id)
    if profile is None or profile.ta_affiliated:
        await callback.answer(_STALE_TEXT, show_alert=True)
        return
    try:
        await BusinessProfileService(partner_repository).update(
            workspace_context, profile.revision,
            business_name=profile.business_name, business_type=business_type,
            short_description=profile.short_description,
            context=business_context_to_dict(profile.context),
        )
    except BusinessProfileAccessError:
        await callback.answer(_STALE_TEXT, show_alert=True)
        if callback.message is not None:
            await callback.message.edit_text(_COMPANY_WRITE_BLOCKED, reply_markup=profile_back_keyboard())
        return
    await callback.answer("Сохранено.")
    if callback.message is not None:
        await callback.message.edit_text(_MENU_INTRO, reply_markup=profile_menu_keyboard())


# ── ✍️ Мой стиль общения ─────────────────────────────────────────────────────


@router.callback_query(F.data == f"{PROFILE_MENU_PREFIX}style")
async def on_profile_style_menu(
    callback: CallbackQuery, state: FSMContext, workspace_context: WorkspaceContext | None,
) -> None:
    await callback.answer()
    if callback.message is None or workspace_context is None:
        return
    await state.set_state(ProfileEdit.waiting_for_style_description)
    await callback.message.edit_text(_STYLE_EXPLANATION)


@router.message(ProfileEdit.waiting_for_style_description, F.text & ~F.text.startswith("/"))
async def on_style_description_answer(
    message: Message,
    state: FSMContext,
    workspace_context: WorkspaceContext | None,
    partner_repository: PartnerRepository,
) -> None:
    await state.clear()
    if workspace_context is None:
        await message.answer(_STALE_TEXT)
        return
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Текст не должен быть пустым.")
        return
    try:
        await UserStyleService(partner_repository).set_style_description(workspace_context, raw)
    except UserStyleAccessError:
        await message.answer(_UNAVAILABLE)
        return
    await message.answer("Сохранено.")
    await _show_menu(message)


# ── 📝 Примеры моих текстов ──────────────────────────────────────────────────


@router.callback_query(F.data == f"{PROFILE_MENU_PREFIX}examples")
async def on_profile_examples_menu(
    callback: CallbackQuery,
    workspace_context: WorkspaceContext | None,
    partner_repository: PartnerRepository,
) -> None:
    await callback.answer()
    if callback.message is None or workspace_context is None:
        return
    preferences = await UserStyleService(partner_repository).get(workspace_context)
    count = 0 if preferences is None else len(preferences.example_posts)
    await callback.message.edit_text(
        f"📝 Примеры моих текстов\n\nСохранено: {count} из 5.",
        reply_markup=profile_examples_keyboard(count),
    )


@router.callback_query(F.data == f"{PROFILE_EXAMPLES_PREFIX}add")
async def on_profile_example_add(
    callback: CallbackQuery, state: FSMContext, workspace_context: WorkspaceContext | None,
) -> None:
    await callback.answer()
    if callback.message is None or workspace_context is None:
        return
    await state.set_state(ProfileEdit.waiting_for_example_post)
    await callback.message.edit_text(_EXAMPLE_EXPLANATION)


@router.message(ProfileEdit.waiting_for_example_post, F.text & ~F.text.startswith("/"))
async def on_example_post_answer(
    message: Message,
    state: FSMContext,
    workspace_context: WorkspaceContext | None,
    partner_repository: PartnerRepository,
) -> None:
    await state.clear()
    if workspace_context is None:
        await message.answer(_STALE_TEXT)
        return
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Текст не должен быть пустым.")
        return
    try:
        preferences = await UserStyleService(partner_repository).add_example_post(
            workspace_context, raw,
        )
    except UserStyleAccessError:
        await message.answer(_UNAVAILABLE)
        return
    except TooManyUserExamplesError:
        await message.answer("Уже сохранено максимум 5 примеров. Сначала очистите список.")
        return
    await message.answer(f"Сохранено. Всего примеров: {len(preferences.example_posts)} из 5.")
    await _show_menu(message)


@router.callback_query(F.data == f"{PROFILE_EXAMPLES_PREFIX}view")
async def on_profile_examples_view(
    callback: CallbackQuery,
    workspace_context: WorkspaceContext | None,
    partner_repository: PartnerRepository,
) -> None:
    await callback.answer()
    if callback.message is None or workspace_context is None:
        return
    preferences = await UserStyleService(partner_repository).get(workspace_context)
    examples = () if preferences is None else preferences.example_posts
    if not examples:
        await callback.message.edit_text(
            "Примеров пока нет.", reply_markup=profile_examples_keyboard(0),
        )
        return
    lines = [f"{index}. {text}" for index, text in enumerate(examples, start=1)]
    await callback.message.edit_text(
        "📝 Ваши примеры:\n\n" + "\n\n".join(lines),
        reply_markup=profile_examples_keyboard(len(examples)),
    )


@router.callback_query(F.data == f"{PROFILE_EXAMPLES_PREFIX}clear")
async def on_profile_examples_clear(
    callback: CallbackQuery,
    workspace_context: WorkspaceContext | None,
    partner_repository: PartnerRepository,
) -> None:
    await callback.answer("Примеры очищены.")
    if callback.message is None or workspace_context is None:
        return
    await UserStyleService(partner_repository).clear_example_posts(workspace_context)
    await callback.message.edit_text(
        "📝 Примеры моих текстов\n\nСохранено: 0 из 5.",
        reply_markup=profile_examples_keyboard(0),
    )


# ── 🚫 Чего не использовать ──────────────────────────────────────────────────


@router.callback_query(F.data == f"{PROFILE_MENU_PREFIX}avoid")
async def on_profile_avoid_menu(
    callback: CallbackQuery,
    state: FSMContext,
    workspace_context: WorkspaceContext | None,
    partner_repository: PartnerRepository,
) -> None:
    await callback.answer()
    if callback.message is None or workspace_context is None:
        return
    preferences = await UserStyleService(partner_repository).get(workspace_context)
    text = _AVOID_EXPLANATION
    if preferences is not None and preferences.avoid_phrases:
        text += "\n\nСейчас: " + ", ".join(preferences.avoid_phrases)
    await state.set_state(ProfileEdit.waiting_for_avoid_phrases)
    await callback.message.edit_text(text)


@router.message(ProfileEdit.waiting_for_avoid_phrases, F.text & ~F.text.startswith("/"))
async def on_avoid_phrases_answer(
    message: Message,
    state: FSMContext,
    workspace_context: WorkspaceContext | None,
    partner_repository: PartnerRepository,
) -> None:
    await state.clear()
    if workspace_context is None:
        await message.answer(_STALE_TEXT)
        return
    phrases = _parse_list(message.text or "")
    try:
        await UserStyleService(partner_repository).set_avoid_phrases(workspace_context, phrases)
    except UserStyleAccessError:
        await message.answer(_UNAVAILABLE)
        return
    await message.answer("Сохранено.")
    await _show_menu(message)
