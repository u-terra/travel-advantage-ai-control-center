from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.filters import MagicData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.keyboards import (
    ARTIFACT_CHECK_PREFIX, ARTIFACT_REVIEW_KEEP, ARTIFACT_REVIEW_SAVE_PREFIX,
    BTN_V2_CHECK_TEXT, SOURCE_ACTION_MAIN_MENU, TEXT_REVIEW_SAVE,
    active_main_menu, artifact_review_keyboard, free_text_review_keyboard,
    v2_back_keyboard,
)
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.partner_repository import PartnerRepository
from app.services.content_factory import ContentFactoryConfig, TextCheckResult, check_text_sync

router = Router(name="text_review")
log = logging.getLogger(__name__)

_WARNING = "Перед публикацией проверьте факты, даты, цены и ссылки."
_FAILURE = "Не удалось проверить текст. Попробуйте ещё раз позже."
_ARTIFACT_FAILURE = _FAILURE + "\n\nИсходный черновик сохранён и не изменён."


class TextReview(StatesGroup):
    waiting_for_text = State()
    free_result = State()
    artifact_result = State()


def _positive_id(data: str, prefix: str) -> int | None:
    raw = data.removeprefix(prefix)
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


def _card(result: TextCheckResult) -> str:
    lines = ["🛡 Проверка текста", "", "Что можно улучшить:"]
    findings = (*result.warnings, *result.rewrite_warnings)
    if findings:
        lines.extend(f"— {item.warning}" for item in findings)
    else:
        lines.append("— Существенных замечаний по текущим правилам нет.")
    lines.extend(["", "Предлагаемая версия:", result.rewritten_text or "Изменённая версия не предложена.", "", _WARNING])
    return "\n".join(lines)


async def _workspace(message_or_callback, repository: PartnerRepository):
    user = message_or_callback.from_user
    if user is None:
        return None
    return await repository.find_workspace_by_telegram_id(user.id)


@router.message(MagicData(F.v2_menu_enabled), F.text == BTN_V2_CHECK_TEXT)
async def start_free_text_review(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(TextReview.waiting_for_text)
    await message.answer(
        "Пришли текст, который нужно проверить и улучшить.",
        reply_markup=v2_back_keyboard(),
    )


@router.message(TextReview.waiting_for_text, F.text & ~F.text.startswith("/"))
async def review_free_text(
    message: Message, state: FSMContext, content_factory_config: ContentFactoryConfig,
) -> None:
    source_text = (message.text or "").strip()
    if not source_text:
        await message.answer("Пришли непустой текст.", reply_markup=v2_back_keyboard())
        return
    result = await asyncio.to_thread(
        check_text_sync, content_factory_config, source_text=source_text
    )
    if result is None:
        await message.answer(_FAILURE, reply_markup=v2_back_keyboard())
        return
    await state.set_state(TextReview.free_result)
    await state.update_data(reviewed_text=result.rewritten_text or source_text)
    await message.answer(_card(result), reply_markup=free_text_review_keyboard())


@router.callback_query(MagicData(F.v2_menu_enabled), F.data.startswith(ARTIFACT_CHECK_PREFIX))
async def review_artifact(
    callback: CallbackQuery, state: FSMContext,
    partner_repository: PartnerRepository, artifact_repository: ArtifactRepository,
    content_factory_config: ContentFactoryConfig,
) -> None:
    artifact_id = _positive_id(callback.data or "", ARTIFACT_CHECK_PREFIX)
    workspace = None if artifact_id is None else await _workspace(callback, partner_repository)
    artifact = None if workspace is None else await artifact_repository.get_artifact(workspace.id, artifact_id)
    version = None if artifact is None else await artifact_repository.get_current_artifact_version(workspace.id, artifact_id)
    if artifact_id is None or workspace is None or artifact is None or version is None or artifact.current_version_id != version.id:
        await callback.answer("Материал недоступен.", show_alert=True)
        return
    if callback.message is None:
        await callback.answer("Сообщение недоступно.", show_alert=True)
        return
    await callback.answer("Проверяю текст…")
    result = await asyncio.to_thread(
        check_text_sync, content_factory_config, source_text=version.content
    )
    if result is None:
        await callback.message.answer(_ARTIFACT_FAILURE)
        return
    await state.set_state(TextReview.artifact_result)
    await state.update_data(
        review_artifact_id=artifact.id, review_version_id=version.id,
        reviewed_text=result.rewritten_text or version.content,
    )
    await callback.message.answer(_card(result), reply_markup=artifact_review_keyboard(artifact.id))


@router.callback_query(MagicData(F.v2_menu_enabled), F.data.startswith(ARTIFACT_REVIEW_SAVE_PREFIX))
async def save_artifact_review(
    callback: CallbackQuery, state: FSMContext,
    partner_repository: PartnerRepository, artifact_repository: ArtifactRepository,
) -> None:
    artifact_id = _positive_id(callback.data or "", ARTIFACT_REVIEW_SAVE_PREFIX)
    data = await state.get_data()
    expected_version_id = data.get("review_version_id")
    reviewed_text = data.get("reviewed_text")
    if (
        artifact_id is None
        or data.get("review_artifact_id") != artifact_id
        or not isinstance(expected_version_id, int)
        or not isinstance(reviewed_text, str)
        or not reviewed_text.strip()
    ):
        await callback.answer("Проверка устарела. Запустите её снова.", show_alert=True)
        return
    workspace = await _workspace(callback, partner_repository)
    if workspace is None:
        await callback.answer("Материал недоступен.", show_alert=True)
        return
    try:
        version = await artifact_repository.add_artifact_version_if_current(
            workspace.id, artifact_id, expected_version_id, reviewed_text,
            generation_note="Safety Layer: улучшенная версия",
        )
    except Exception:
        log.warning("text_review: artifact version persistence failed")
        version = None
    if version is None:
        await callback.answer("Не удалось сохранить: версия изменилась или материал недоступен.", show_alert=True)
        return
    await state.clear()
    await callback.answer("Версия сохранена.")
    if callback.message is not None:
        await callback.message.answer(f"✅ Сохранена версия №{version.version_number}.")


@router.callback_query(MagicData(F.v2_menu_enabled), F.data == ARTIFACT_REVIEW_KEEP)
async def keep_artifact(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Текущая версия оставлена без изменений.")


@router.callback_query(MagicData(F.v2_menu_enabled), F.data == TEXT_REVIEW_SAVE)
async def save_free_text(
    callback: CallbackQuery, state: FSMContext,
    partner_repository: PartnerRepository, artifact_repository: ArtifactRepository,
) -> None:
    data = await state.get_data()
    text = data.get("reviewed_text")
    workspace = await _workspace(callback, partner_repository)
    if not isinstance(text, str) or not text.strip() or workspace is None:
        await callback.answer("Результат недоступен. Запустите проверку снова.", show_alert=True)
        return
    try:
        artifact, _ = await artifact_repository.create_artifact_with_initial_version(
            workspace.id, artifact_type="other", title="Проверенный текст",
            content=text, generation_note="Safety Layer: проверка текста",
        )
    except Exception:
        log.warning("text_review: free text persistence failed")
        await callback.answer("Не удалось сохранить материал.", show_alert=True)
        return
    await state.clear()
    await callback.answer("Материал сохранён.")
    if callback.message is not None:
        await callback.message.answer(f"💾 Материал №{artifact.id} сохранён как черновик.")


@router.callback_query(MagicData(F.v2_menu_enabled), F.data == SOURCE_ACTION_MAIN_MENU)
async def review_main_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    if callback.message is not None:
        await callback.message.answer("Главное меню. Выберите нужную задачу.", reply_markup=active_main_menu(True))
