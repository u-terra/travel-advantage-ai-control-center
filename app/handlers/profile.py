"""Раздел «Профиль»: просмотр бизнес-профиля workspace.

Только просмотр. Профиль формируется отдельно (администратором сервиса);
здесь пользователь видит, какие данные о его бизнесе уже использует
Оркестратор для персонализации материалов и ответов.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import MagicData
from aiogram.types import Message

from app.domain.business_profiles import BusinessProfile
from app.domain.partners import WorkspaceContext
from app.keyboards import BTN_V2_PROFILE, v2_back_keyboard
from app.repositories.partner_repository import PartnerRepository

router = Router(name="profile")

_UNAVAILABLE = "Рабочее пространство недоступно."

_PROFILE_MISSING = (
    "Профиль бизнеса ещё не заполнен.\n\n"
    "Обратитесь к администратору сервиса, чтобы его настроить."
)

_BUSINESS_TYPE_LABELS: dict[str, str] = {
    "independent_agent": "независимый агент",
    "club_partner": "клубный партнёр",
    "agency": "турагентство",
    "travel_company": "туроператор",
    "other": "другое",
}

_FOOTER = (
    "Эти данные использует Оркестратор, чтобы персонализировать материалы "
    "и ответы под ваш бизнес."
)


def _joined(values: tuple[str, ...]) -> str | None:
    cleaned = [value.strip() for value in values if value.strip()]
    return ", ".join(cleaned) if cleaned else None


def _profile_text(profile: BusinessProfile) -> str:
    context = profile.context
    lines: list[str] = ["⚙️ Профиль бизнеса", ""]

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

    audiences = _joined(context.audiences)
    if audiences:
        lines.append(f"Аудитории: {audiences}")

    markets = _joined(context.markets)
    if markets:
        lines.append(f"Рынки: {markets}")

    statement = str(context.positioning.get("statement") or "").strip()
    if statement:
        lines.append(f"Позиционирование: {statement}")

    value_proposition = str(context.positioning.get("value_proposition") or "").strip()
    if value_proposition:
        lines.append(f"Ценность для клиента: {value_proposition}")

    tone = str(context.communication.get("tone") or "").strip()
    if tone:
        lines.append(f"Тон общения: {tone}")

    goals = _joined(context.goals)
    if goals:
        lines.append(f"Цели: {goals}")

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
        lines.append(
            "Профиль заполнен частично — персонализация пока ограничена."
        )

    lines.append("")
    lines.append(_FOOTER)
    return "\n".join(lines)


@router.message(MagicData(F.v2_menu_enabled), F.text == BTN_V2_PROFILE)
async def show_profile(
    message: Message,
    workspace_context: WorkspaceContext | None,
    partner_repository: PartnerRepository,
) -> None:
    if workspace_context is None:
        await message.answer(_UNAVAILABLE, reply_markup=v2_back_keyboard())
        return
    profile = await partner_repository.get_business_profile(
        workspace_context.workspace_id
    )
    if profile is None:
        await message.answer(_PROFILE_MISSING, reply_markup=v2_back_keyboard())
        return
    await message.answer(_profile_text(profile), reply_markup=v2_back_keyboard())
