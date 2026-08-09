"""Тесты административного раздела «Источники»."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.handlers import build_router
from app.handlers.sources import (
    AddSource,
    receive_source_url,
    router,
    show_sources,
    start_add_source,
    toggle_source,
)
from app.keyboards import (
    BTN_V2_MAIN_MENU,
    BTN_V2_SOURCES,
    SOURCE_REGISTRY_ADD,
    SOURCE_TOGGLE_PREFIX,
    sources_registry_keyboard,
    v2_main_menu,
)
from app.services.source_registry_store import SourceRegistryStore

# Несуществующий seed: раздел проверяется на своих данных, а не на стартовом
# наборе проекта.
_EMPTY_SEED = Path(__file__).with_name("__absent_seed__.json")


def run(value):
    return asyncio.run(value)


class State:
    def __init__(self) -> None:
        self.state = None
        self.clear_calls = 0

    async def clear(self) -> None:
        self.state = None
        self.clear_calls += 1

    async def set_state(self, value) -> None:
        self.state = value


class Message:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.from_user = SimpleNamespace(id=1)
        self.answers: list[tuple[str, dict]] = []
        self.edits: list[tuple[str, dict]] = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class Callback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = Message()
        self.answers: list[tuple[str, dict]] = []

    async def answer(self, text: str = "", **kwargs):
        self.answers.append((text, kwargs))


@pytest.fixture()
def store(tmp_path: Path) -> SourceRegistryStore:
    return SourceRegistryStore(tmp_path / "sources.json", seed_path=_EMPTY_SEED)


def _button_texts(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def _callback_data(markup) -> list[str]:
    return [
        button.callback_data for row in markup.inline_keyboard for button in row
    ]


# ── Просмотр списка ──────────────────────────────────────────────────────────


def test_list_shows_enabled_and_disabled_state(store: SourceRegistryStore) -> None:
    store.add("https://example.com/a", name="Активный")
    store.add("https://example.com/b", name="Выключенный", enabled=False)

    message, state = Message(BTN_V2_SOURCES), State()
    run(show_sources(message, state, store))

    text, kwargs = message.answers[0]
    assert "Всего: 2" in text and "активных: 1" in text
    labels = _button_texts(kwargs["reply_markup"])
    assert "🟢 Активный" in labels
    assert "⚪️ Выключенный · выключен" in labels


def test_list_explains_that_a_record_is_not_monitoring(
    store: SourceRegistryStore,
) -> None:
    store.add("https://example.com/a")
    message = Message(BTN_V2_SOURCES)
    run(show_sources(message, State(), store))

    assert "разрешён" in message.answers[0][0]
    assert "Lead Radar" in message.answers[0][0]


def test_empty_registry_does_not_break_the_screen(store: SourceRegistryStore) -> None:
    message = Message(BTN_V2_SOURCES)
    run(show_sources(message, State(), store))

    assert "Всего: 0" in message.answers[0][0]
    assert SOURCE_REGISTRY_ADD in _callback_data(message.answers[0][1]["reply_markup"])


def test_broken_registry_is_reported_without_crashing(tmp_path: Path) -> None:
    path = tmp_path / "sources.json"
    path.write_text("{ broken", encoding="utf-8")
    message = Message(BTN_V2_SOURCES)

    run(show_sources(message, State(), SourceRegistryStore(path, seed_path=_EMPTY_SEED)))

    assert "недоступен" in message.answers[0][0]


# ── Переключение ─────────────────────────────────────────────────────────────


def test_toggle_disables_an_enabled_source(store: SourceRegistryStore) -> None:
    source = store.add("https://example.com/a", name="Источник")
    callback = Callback(f"{SOURCE_TOGGLE_PREFIX}{source.id}")

    run(toggle_source(callback, store))

    assert store.get(source.id).enabled is False
    assert "выключен" in callback.answers[0][0]


def test_toggle_enables_a_disabled_source(store: SourceRegistryStore) -> None:
    source = store.add("https://example.com/a", enabled=False)
    callback = Callback(f"{SOURCE_TOGGLE_PREFIX}{source.id}")

    run(toggle_source(callback, store))

    assert store.get(source.id).enabled is True


def test_toggle_refreshes_the_list_in_place(store: SourceRegistryStore) -> None:
    source = store.add("https://example.com/a", name="Источник")
    callback = Callback(f"{SOURCE_TOGGLE_PREFIX}{source.id}")

    run(toggle_source(callback, store))

    assert "⚪️ Источник · выключен" in _button_texts(
        callback.message.edits[0][1]["reply_markup"]
    )


def test_toggle_of_an_unknown_source_is_reported(store: SourceRegistryStore) -> None:
    callback = Callback(f"{SOURCE_TOGGLE_PREFIX}no-such-source")

    run(toggle_source(callback, store))

    assert "не найден" in callback.answers[0][0]
    assert callback.message.edits == []


# ── Добавление ───────────────────────────────────────────────────────────────


def test_add_button_asks_for_a_url() -> None:
    callback, state = Callback(SOURCE_REGISTRY_ADD), State()

    run(start_add_source(callback, state))

    assert state.state == AddSource.waiting_for_url
    assert "ссылку" in callback.message.answers[0][0]


def test_prompt_promises_no_fetching() -> None:
    callback = Callback(SOURCE_REGISTRY_ADD)
    run(start_add_source(callback, State()))

    text = callback.message.answers[0][0]
    assert "не открывается" in text and "не скачивается" in text


def test_url_is_added_to_the_registry(store: SourceRegistryStore) -> None:
    message, state = Message("https://example.com/blog"), State()

    run(receive_source_url(message, state, store))

    assert len(store.list()) == 1
    assert "добавлен" in message.answers[0][0]
    assert state.state is None


def test_telegram_link_is_added_as_telegram(store: SourceRegistryStore) -> None:
    message = Message("https://t.me/some_public_channel/17")

    run(receive_source_url(message, State(), store))

    added = store.list()[0]
    assert added.platform == "telegram"
    assert added.url == "https://t.me/some_public_channel"


def test_invalid_url_is_explained_and_not_stored(store: SourceRegistryStore) -> None:
    message, state = Message("ftp://example.com/file"), State()

    run(receive_source_url(message, state, store))

    assert store.list() == ()
    assert "Не получилось" in message.answers[0][0]
    # Состояние сохраняется: владелец может сразу прислать корректный адрес.
    assert state.clear_calls == 0


def test_duplicate_is_reported_without_a_second_record(
    store: SourceRegistryStore,
) -> None:
    store.add("https://example.com/blog", name="Уже есть")
    message = Message("https://example.com/blog")

    run(receive_source_url(message, State(), store))

    assert len(store.list()) == 1
    assert "уже включён" in message.answers[0][0]


def test_duplicate_of_a_disabled_source_says_it_is_off(
    store: SourceRegistryStore,
) -> None:
    store.add("https://example.com/blog", name="Кандидат", enabled=False)
    message = Message("https://example.com/blog")

    run(receive_source_url(message, State(), store))

    assert "выключен" in message.answers[0][0]
    assert len(store.list()) == 1


def test_added_source_message_does_not_promise_monitoring(
    store: SourceRegistryStore,
) -> None:
    message = Message("https://example.com/blog")
    run(receive_source_url(message, State(), store))

    assert "не запускается" in message.answers[0][0]


# ── Меню и маршрутизация ─────────────────────────────────────────────────────


def test_sources_button_is_in_the_v2_menu() -> None:
    assert BTN_V2_SOURCES in [
        button.text for row in v2_main_menu().keyboard for button in row
    ]


def test_keyboard_always_offers_add_and_exit() -> None:
    markup = sources_registry_keyboard((("id", "Название", True),))

    assert _button_texts(markup)[-2:] == ["➕ Добавить источник", BTN_V2_MAIN_MENU]


def test_sources_router_is_registered() -> None:
    # Роутеры — модульные синглтоны, поэтому собранный ранее owner
    # переиспользуется: повторный build_router() упал бы на привязке.
    owner = router.parent_router or build_router()
    assert "sources" in [child.name for child in owner.sub_routers]


async def _matching_handler(text: str, flag_data: dict, raw_state=None):
    message = Message(text)
    for handler in router.message.handlers:
        matched, _ = await handler.check(message, raw_state=raw_state, **flag_data)
        if matched:
            return handler.callback.__name__
    return None


def test_section_is_fail_closed_by_the_v2_flag() -> None:
    assert run(_matching_handler(BTN_V2_SOURCES, {"v2_menu_enabled": True})) == (
        "show_sources"
    )
    assert run(_matching_handler(BTN_V2_SOURCES, {"v2_menu_enabled": False})) is None
    assert run(_matching_handler(BTN_V2_SOURCES, {})) is None


def test_main_menu_button_cancels_adding_instead_of_being_stored() -> None:
    assert run(
        _matching_handler(
            BTN_V2_MAIN_MENU,
            {"v2_menu_enabled": True},
            raw_state=AddSource.waiting_for_url.state,
        )
    ) == "cancel_add_source"
