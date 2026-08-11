from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.domain.partners import WorkspaceContext
from app.keyboards import (
    PILOT_CONSENT_ACCEPT_PREFIX,
    pilot_consent_keyboard,
)
from app.repositories.partner_repository import PartnerRepository
from app.services.pilot_consent import PILOT_CONSENT_VERSION, PilotConsentService


router = Router(name="consent")

CONSENT_TEXT = (
    "Для использования платного пилота нужно отдельно подтвердить согласие "
    "на обработку персональных данных. Данные используются для работы "
    "AI-Оркестратора; введённые тексты могут передаваться подключённому "
    "провайдеру AI для выполнения запроса.\n\n"
    f"Версия согласия: {PILOT_CONSENT_VERSION}"
)


async def show_pilot_consent(message: Message) -> None:
    await message.answer(
        CONSENT_TEXT,
        reply_markup=pilot_consent_keyboard(PILOT_CONSENT_VERSION),
    )


@router.message(Command("consent"))
async def request_pilot_consent(message: Message) -> None:
    await show_pilot_consent(message)


@router.callback_query(F.data.startswith(PILOT_CONSENT_ACCEPT_PREFIX))
async def accept_pilot_consent(
    callback: CallbackQuery,
    workspace_context: WorkspaceContext | None,
    partner_repository: PartnerRepository,
) -> None:
    version = (callback.data or "").removeprefix(PILOT_CONSENT_ACCEPT_PREFIX)
    if version != PILOT_CONSENT_VERSION or workspace_context is None:
        await callback.answer(
            "Версия согласия устарела. Откройте /consent повторно.",
            show_alert=True,
        )
        return
    await PilotConsentService(partner_repository).accept_current(workspace_context)
    await callback.answer("Согласие сохранено.")
    if callback.message is not None:
        await callback.message.answer(
            "Согласие на обработку персональных данных подтверждено."
        )
