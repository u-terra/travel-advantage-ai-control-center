"""Раздел «Источники»: workspace-список, включение/выключение, добавление.

Экран административный и полностью ручной: ничего не собирается, ни один
адрес не открывается и не скачивается. Добавление источника — это запись
subscription в SQLite, то есть РАЗРЕШЕНИЕ наблюдать за источником,
а не запуск мониторинга.

JSON для Lead Radar создаётся отдельно как проекция и handler-ами не меняется.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import MagicData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.keyboards import (
    BTN_V2_MAIN_MENU,
    BTN_V2_SOURCES,
    SOURCE_TOGGLE_PREFIX,
    SOURCE_REGISTRY_ADD,
    active_main_menu,
    sources_registry_keyboard,
    v2_back_keyboard,
)
from app.domain.partners import WorkspaceContext
from app.domain.sources import WorkspaceSource
from app.repositories.source_catalog_repository import SourceCatalogRepository
from app.services.source_registry import SourceRegistryError
from app.services.source_registry_store import (
    SourceAddressError,
    UnknownSourceError,
)

router = Router(name="sources")
log = logging.getLogger(__name__)

# Телеграм не покажет клавиатуру произвольной длины, поэтому список
# ограничен.
_LIST_LIMIT = 20

_ADD_PROMPT = (
    "Пришлите ссылку на источник.\n\n"
    "Подойдёт https://... или @имя_канала для Telegram.\n"
    "Ссылка будет отправлена администратору на проверку."
)

_UNAVAILABLE = (
    "Реестр источников сейчас недоступен. Попробуйте ещё раз позже."
)


class AddSource(StatesGroup):
    waiting_for_url = State()


def _title(source: WorkspaceSource) -> str:
    state = "" if source.enabled else " · выключен"
    return f"{source.name}{state}"


def _summary(sources: tuple[WorkspaceSource, ...]) -> str:
    enabled = sum(1 for source in sources if source.enabled)
    lines = [
        "📚 Источники",
        "",
        f"Всего: {len(sources)} · активных: {enabled}",
        "",
        "🟢 — источник разрешён к использованию, ⚪️ — выключен.",
        "Нажатие на источник переключает это состояние.",
        "",
        "Разрешение — это ещё не мониторинг: сбором занимается "
        "Travel Lead Radar, и он читает свой список.",
    ]
    if len(sources) > _LIST_LIMIT:
        lines.append("")
        lines.append(f"Показаны первые {_LIST_LIMIT} источников.")
    return "\n".join(lines)


def _keyboard(sources: tuple[WorkspaceSource, ...]):
    return sources_registry_keyboard(
        tuple(
            (source.id, _title(source), source.enabled)
            for source in sources[:_LIST_LIMIT]
        )
    )


def _ordered(sources: tuple[WorkspaceSource, ...]) -> tuple[WorkspaceSource, ...]:
    return tuple(
        sorted(sources, key=lambda item: (not item.enabled, item.priority, item.id))
    )


async def _load(
    repository: SourceCatalogRepository, workspace_id: int
) -> tuple[WorkspaceSource, ...]:
    return _ordered(await repository.list_for_workspace(workspace_id))


@router.message(MagicData(F.v2_menu_enabled), F.text == BTN_V2_SOURCES)
async def show_sources(
    message: Message,
    state: FSMContext,
    source_catalog_repository: SourceCatalogRepository,
    workspace_context: WorkspaceContext | None,
) -> None:
    await state.clear()
    if workspace_context is None:
        await message.answer(_UNAVAILABLE, reply_markup=v2_back_keyboard())
        return
    try:
        sources = await _load(source_catalog_repository, workspace_context.workspace_id)
    except SourceRegistryError as exc:
        log.warning("Реестр источников не прочитан: %s", exc)
        await message.answer(_UNAVAILABLE, reply_markup=v2_back_keyboard())
        return

    await message.answer(
        _summary(sources),
        reply_markup=_keyboard(sources),
        disable_web_page_preview=True,
    )


@router.callback_query(
    MagicData(F.v2_menu_enabled), F.data.startswith(SOURCE_TOGGLE_PREFIX)
)
async def toggle_source(
    callback: CallbackQuery,
    source_catalog_repository: SourceCatalogRepository,
    workspace_context: WorkspaceContext | None,
) -> None:
    source_id = (callback.data or "").removeprefix(SOURCE_TOGGLE_PREFIX)

    if workspace_context is None:
        await callback.answer(_UNAVAILABLE, show_alert=True)
        return

    try:
        # Переключение целиком на стороне реестра: прочитать здесь и записать
        # обратное значило бы, что два быстрых нажатия отменят друг друга.
        updated = await source_catalog_repository.toggle(
            workspace_context.workspace_id, source_id
        )
        sources = await _load(
            source_catalog_repository, workspace_context.workspace_id
        )
    except UnknownSourceError:
        await callback.answer("Источник не найден в реестре.", show_alert=True)
        return
    except SourceRegistryError as exc:
        log.warning("Не удалось переключить источник %s: %s", source_id, exc)
        await callback.answer(_UNAVAILABLE, show_alert=True)
        return

    await callback.answer(
        f"{updated.name}: {'включён' if updated.enabled else 'выключен'}"
    )
    if callback.message is not None:
        await callback.message.edit_text(
            _summary(sources),
            reply_markup=_keyboard(sources),
            disable_web_page_preview=True,
        )


@router.callback_query(MagicData(F.v2_menu_enabled), F.data == SOURCE_REGISTRY_ADD)
async def start_add_source(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddSource.waiting_for_url)
    await callback.answer()
    if callback.message is not None:
        await callback.message.answer(_ADD_PROMPT, reply_markup=v2_back_keyboard())


@router.message(
    MagicData(F.v2_menu_enabled), AddSource.waiting_for_url, F.text == BTN_V2_MAIN_MENU
)
async def cancel_add_source(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Главное меню. Выберите нужную задачу.", reply_markup=active_main_menu(True)
    )


@router.message(MagicData(F.v2_menu_enabled), AddSource.waiting_for_url)
async def receive_source_url(
    message: Message,
    state: FSMContext,
    source_catalog_repository: SourceCatalogRepository,
    workspace_context: WorkspaceContext | None,
) -> None:
    address = (message.text or "").strip()

    if workspace_context is None:
        await message.answer(_UNAVAILABLE, reply_markup=v2_back_keyboard())
        return

    try:
        result = await source_catalog_repository.submit_source_request(
            workspace_context.workspace_id,
            workspace_context.telegram_user_id,
            address,
        )
    except SourceAddressError as exc:
        await message.answer(f"Не получилось: {exc}", reply_markup=v2_back_keyboard())
        return
    except SourceRegistryError as exc:
        log.warning("Источник не добавлен: %s", exc)
        await message.answer(_UNAVAILABLE, reply_markup=v2_back_keyboard())
        return

    await state.clear()
    if result.outcome == "already_connected":
        await message.answer(
            "Этот источник уже подключён.",
            reply_markup=active_main_menu(True),
        )
        return
    if result.outcome == "already_pending":
        await message.answer(
            "Этот источник уже ожидает проверки.",
            reply_markup=active_main_menu(True),
        )
        return
    if result.outcome == "reopened":
        await message.answer(
            "Источник повторно отправлен на проверку.\n"
            "После подключения он будет использоваться для вашего пространства.",
            reply_markup=active_main_menu(True),
        )
        return
    await message.answer(
        "Источник отправлен на проверку.\n"
        "После подключения он будет использоваться для вашего пространства.",
        reply_markup=active_main_menu(True),
    )
