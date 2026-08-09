"""Раздел «Источники»: просмотр реестра, включение/выключение, добавление.

Экран административный и полностью ручной: ничего не собирается, ни один
адрес не открывается и не скачивается. Добавление источника — это запись
строки в data-файл реестра, то есть РАЗРЕШЕНИЕ наблюдать за источником,
а не запуск мониторинга.

Конкретных каналов здесь нет и быть не может: список целиком приходит из
``config/sources.json``.
"""

from __future__ import annotations

import asyncio
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
from app.domain.sources import Source
from app.services.source_registry import SourceRegistryError
from app.services.source_registry_store import (
    DuplicateSourceError,
    SourceAddressError,
    SourceRegistryStore,
    UnknownSourceError,
)

router = Router(name="sources")
log = logging.getLogger(__name__)

# Телеграм не покажет клавиатуру произвольной длины, поэтому список
# ограничен. Полный реестр всегда доступен в data-файле.
_LIST_LIMIT = 20

_ADD_PROMPT = (
    "Пришлите ссылку на источник.\n\n"
    "Подойдёт https://... или @имя_канала для Telegram.\n"
    "Ссылка только записывается в реестр — ничего не открывается и "
    "не скачивается."
)

_UNAVAILABLE = (
    "Реестр источников сейчас недоступен. Попробуйте ещё раз позже."
)


class AddSource(StatesGroup):
    waiting_for_url = State()


def _title(source: Source) -> str:
    state = "" if source.enabled else " · выключен"
    return f"{source.name}{state}"


def _summary(sources: tuple[Source, ...]) -> str:
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


def _keyboard(sources: tuple[Source, ...]):
    return sources_registry_keyboard(
        tuple(
            (source.id, _title(source), source.enabled)
            for source in sources[:_LIST_LIMIT]
        )
    )


def _ordered(sources: tuple[Source, ...]) -> tuple[Source, ...]:
    return tuple(
        sorted(sources, key=lambda item: (not item.enabled, item.priority, item.id))
    )


async def _load(store: SourceRegistryStore) -> tuple[Source, ...]:
    return _ordered(await asyncio.to_thread(store.list))


@router.message(MagicData(F.v2_menu_enabled), F.text == BTN_V2_SOURCES)
async def show_sources(
    message: Message, state: FSMContext, source_registry_store: SourceRegistryStore
) -> None:
    await state.clear()
    try:
        sources = await _load(source_registry_store)
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
    callback: CallbackQuery, source_registry_store: SourceRegistryStore
) -> None:
    source_id = (callback.data or "").removeprefix(SOURCE_TOGGLE_PREFIX)

    try:
        # Переключение целиком на стороне реестра: прочитать здесь и записать
        # обратное значило бы, что два быстрых нажатия отменят друг друга.
        updated = await asyncio.to_thread(source_registry_store.toggle, source_id)
        sources = await _load(source_registry_store)
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
    message: Message, state: FSMContext, source_registry_store: SourceRegistryStore
) -> None:
    address = (message.text or "").strip()

    try:
        source = await asyncio.to_thread(source_registry_store.add, address)
    except DuplicateSourceError as exc:
        state_text = "уже включён" if exc.existing.enabled else "есть, но выключен"
        await message.answer(
            f"Такой источник {state_text}: {exc.existing.name}.\n"
            "Второй раз добавлять не нужно — состояние переключается "
            "в списке источников.",
            reply_markup=v2_back_keyboard(),
        )
        return
    except SourceAddressError as exc:
        await message.answer(f"Не получилось: {exc}", reply_markup=v2_back_keyboard())
        return
    except SourceRegistryError as exc:
        log.warning("Источник не добавлен: %s", exc)
        await message.answer(_UNAVAILABLE, reply_markup=v2_back_keyboard())
        return

    await state.clear()
    await message.answer(
        f"✅ Источник добавлен: {source.name}\n"
        f"{source.target}\n\n"
        f"id: {source.id}\n"
        "Назначение пока нейтральное — его стоит уточнить после того, как "
        "содержимое источника будет разобрано.\n\n"
        "Запись означает разрешение наблюдать за источником. "
        "Автоматический сбор при этом не запускается.",
        reply_markup=active_main_menu(True),
        disable_web_page_preview=True,
    )
