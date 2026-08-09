"""Regression-тесты управляемого реестра источников (Source Registry v1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.domain.sources import (
    PLATFORM_TELEGRAM,
    PLATFORM_WEB,
    PURPOSE_MIXED,
    detect_platform,
    normalize_platform,
    normalize_url,
)
from app.services.source_registry import (
    SourceRegistryError,
    load_registry,
    parse_registry,
)
from app.services.source_registry_store import (
    DuplicateSourceError,
    SourceAddressError,
    SourceRegistryStore,
    UnknownSourceError,
    resolve_address,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Несуществующий путь: магазин стартует с пустым реестром вместо стартового
# набора из config/sources.json. Разворачивание seed проверяется отдельно,
# в tests/test_source_registry_bootstrap.py.
_EMPTY_SEED = Path(__file__).with_name("__absent_seed__.json")


@pytest.fixture()
def store(tmp_path: Path) -> SourceRegistryStore:
    """Пустой реестр во временном файле — production-данные не трогаются."""
    return SourceRegistryStore(tmp_path / "sources.json", seed_path=_EMPTY_SEED)


def _payload(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ── Создание источника ───────────────────────────────────────────────────────


def test_add_creates_source(store: SourceRegistryStore) -> None:
    source = store.add("https://example.com/blog")

    assert source.platform == PLATFORM_WEB
    assert source.url == "https://example.com/blog"
    assert source.enabled is True
    assert source.purpose == PURPOSE_MIXED
    assert store.get(source.id) == source


def test_add_writes_a_data_file(store: SourceRegistryStore) -> None:
    store.add("https://example.com/blog")

    payload = _payload(store.path)
    assert [record["url"] for record in payload["sources"]] == [
        "https://example.com/blog"
    ]


def test_added_source_gets_timestamps(store: SourceRegistryStore) -> None:
    source = store.add("https://example.com/blog")

    assert source.created_at
    assert source.updated_at == source.created_at


def test_add_does_not_invent_a_purpose(store: SourceRegistryStore) -> None:
    # Содержание канала неизвестно, поэтому назначение остаётся нейтральным,
    # а заметка честно говорит, что классификация ещё впереди.
    source = store.add("https://t.me/some_public_channel")

    assert source.purpose == PURPOSE_MIXED
    assert "уточняется" in source.notes.lower()


def test_explicit_fields_win_over_defaults(store: SourceRegistryStore) -> None:
    source = store.add(
        "https://example.com/blog",
        name="Пример",
        purpose="market",
        notes="Ручная заметка",
        enabled=False,
        priority=10,
        source_id="custom_id",
    )

    assert (source.id, source.name, source.purpose) == (
        "custom_id",
        "Пример",
        "market",
    )
    assert (source.enabled, source.priority, source.notes) == (
        False,
        10,
        "Ручная заметка",
    )


def test_generated_ids_do_not_collide(store: SourceRegistryStore) -> None:
    first = store.add("https://example.com/a")
    second = store.add("https://example.com/b")

    assert first.id != second.id


# ── Список ───────────────────────────────────────────────────────────────────


def test_list_returns_everything(store: SourceRegistryStore) -> None:
    store.add("https://example.com/a")
    store.add("https://example.com/b", enabled=False)

    assert len(store.list()) == 2


def test_list_can_return_only_enabled(store: SourceRegistryStore) -> None:
    store.add("https://example.com/a")
    disabled = store.add("https://example.com/b", enabled=False)

    ids = {source.id for source in store.list(only_enabled=True)}
    assert disabled.id not in ids


def test_list_can_filter_by_platform(store: SourceRegistryStore) -> None:
    telegram = store.add("https://t.me/some_public_channel")
    store.add("https://example.com/a")

    assert [source.id for source in store.list(platform="telegram")] == [telegram.id]


def test_list_platform_filter_accepts_aliases(store: SourceRegistryStore) -> None:
    web = store.add("https://example.com/a")
    store.add("https://t.me/some_public_channel")

    assert [source.id for source in store.list(platform="website")] == [web.id]


def test_list_of_an_absent_file_is_empty(tmp_path: Path) -> None:
    # Отсутствующий файл — это пустой реестр, а не сбой раздела.
    assert SourceRegistryStore(tmp_path / "missing.json", seed_path=_EMPTY_SEED).list() == ()


# ── enabled / disabled ───────────────────────────────────────────────────────


def test_disable_keeps_the_source(store: SourceRegistryStore) -> None:
    source = store.add("https://example.com/a")

    disabled = store.disable(source.id)

    assert disabled.enabled is False
    assert store.get(source.id) is not None


def test_enable_turns_the_source_back_on(store: SourceRegistryStore) -> None:
    source = store.add("https://example.com/a", enabled=False)

    assert store.enable(source.id).enabled is True


def test_toggling_updates_the_timestamp(store: SourceRegistryStore) -> None:
    source = store.add("https://example.com/a")

    disabled = store.disable(source.id)

    assert disabled.updated_at >= source.updated_at
    assert disabled.created_at == source.created_at


def test_repeated_disable_is_harmless(store: SourceRegistryStore) -> None:
    source = store.add("https://example.com/a")
    first = store.disable(source.id)
    second = store.disable(source.id)

    assert second.enabled is False
    assert second.updated_at == first.updated_at


def test_unknown_source_cannot_be_toggled(store: SourceRegistryStore) -> None:
    store.add("https://example.com/a")

    with pytest.raises(UnknownSourceError):
        store.enable("no-such-source")


def test_remove_deletes_the_record(store: SourceRegistryStore) -> None:
    source = store.add("https://example.com/a")

    removed = store.remove(source.id)

    assert removed.id == source.id
    assert store.get(source.id) is None


def test_unknown_source_cannot_be_removed(store: SourceRegistryStore) -> None:
    with pytest.raises(UnknownSourceError):
        store.remove("no-such-source")


# ── Нормализация Telegram-адресов ────────────────────────────────────────────


@pytest.mark.parametrize(
    "address",
    [
        "@some_public_channel",
        "t.me/some_public_channel",
        "https://t.me/some_public_channel",
        "https://t.me/some_public_channel/",
        "https://t.me/s/some_public_channel",
        "https://t.me/some_public_channel/1234",
        "  https://t.me/some_public_channel  ",
    ],
)
def test_telegram_addresses_are_normalized(
    store: SourceRegistryStore, address: str
) -> None:
    resolved = resolve_address(address)

    assert resolved.platform == PLATFORM_TELEGRAM
    assert resolved.username == "some_public_channel"
    assert resolved.url == "https://t.me/some_public_channel"


def test_telegram_source_is_stored_canonically(store: SourceRegistryStore) -> None:
    source = store.add("https://t.me/some_public_channel/1234")

    assert source.platform == PLATFORM_TELEGRAM
    assert source.username == "some_public_channel"
    assert source.url == "https://t.me/some_public_channel"
    assert source.handle == "@some_public_channel"


def test_telegram_url_is_detected_as_telegram_not_web() -> None:
    assert detect_platform("https://t.me/some_public_channel") == PLATFORM_TELEGRAM
    assert detect_platform("https://example.com/x") == PLATFORM_WEB


@pytest.mark.parametrize(
    "address",
    ["https://t.me/+secret_invite_hash", "https://t.me/joinchat/secret"],
)
def test_private_telegram_invites_are_rejected(address: str) -> None:
    # Приглашение не даёт публичного username. Молча угадывать имя канала
    # нельзя: догадка превратилась бы в канонический адрес чужого канала.
    with pytest.raises(SourceAddressError):
        resolve_address(address)


def test_a_foreign_host_never_becomes_a_telegram_source(
    store: SourceRegistryStore,
) -> None:
    # Ссылка чужого сайта со строкой t.me внутри пути — обычный веб-адрес.
    source = store.add("https://evil.example.com/t.me/channel")

    assert source.platform == PLATFORM_WEB
    assert source.username is None


def test_a_bare_name_without_a_scheme_is_rejected(store: SourceRegistryStore) -> None:
    # Голое слово может быть чем угодно. Требование явной формы (@имя или
    # ссылка) исключает запись «угаданного» канала.
    with pytest.raises(SourceAddressError):
        store.add("some_public_channel")


# ── Дубликаты ────────────────────────────────────────────────────────────────


def test_duplicate_url_does_not_create_a_second_source(
    store: SourceRegistryStore,
) -> None:
    first = store.add("https://example.com/blog")

    with pytest.raises(DuplicateSourceError) as error:
        store.add("https://example.com/blog")

    assert error.value.existing.id == first.id
    assert len(store.list()) == 1


def test_same_telegram_channel_in_another_form_is_a_duplicate(
    store: SourceRegistryStore,
) -> None:
    store.add("@some_public_channel")

    with pytest.raises(DuplicateSourceError):
        store.add("https://t.me/some_public_channel/99")

    assert len(store.list()) == 1


def test_duplicate_is_reported_even_when_the_existing_source_is_disabled(
    store: SourceRegistryStore,
) -> None:
    existing = store.add("https://example.com/blog", enabled=False)

    with pytest.raises(DuplicateSourceError) as error:
        store.add("https://example.com/blog")

    assert error.value.existing.id == existing.id
    assert error.value.existing.enabled is False


def test_duplicate_ignores_trailing_slash_and_case(
    store: SourceRegistryStore,
) -> None:
    store.add("https://Example.com/blog")

    with pytest.raises(DuplicateSourceError):
        store.add("https://example.com/blog")


def test_registry_rejects_two_enabled_sources_with_one_address(
    tmp_path: Path,
) -> None:
    payload = {
        "schema_version": 2,
        "sources": [
            {
                "id": "a",
                "name": "A",
                "platform": "web",
                "url": "https://example.com/blog",
                "source_type": "monitored_source",
                "purpose": "mixed",
            },
            {
                "id": "b",
                "name": "B",
                "platform": "web",
                "url": "https://example.com/blog",
                "source_type": "monitored_source",
                "purpose": "mixed",
            },
        ],
    }

    with pytest.raises(SourceRegistryError):
        parse_registry(payload)


def test_a_disabled_twin_is_allowed(tmp_path: Path) -> None:
    payload = {
        "schema_version": 2,
        "sources": [
            {
                "id": "a",
                "name": "A",
                "platform": "web",
                "url": "https://example.com/blog",
                "source_type": "monitored_source",
                "purpose": "mixed",
            },
            {
                "id": "b",
                "name": "B",
                "platform": "web",
                "url": "https://example.com/blog",
                "source_type": "monitored_source",
                "purpose": "mixed",
                "enabled": False,
            },
        ],
    }

    assert len(parse_registry(payload)) == 2


def test_find_by_address_recognizes_any_form(store: SourceRegistryStore) -> None:
    source = store.add("@some_public_channel")

    assert store.find_by_address("https://t.me/some_public_channel").id == source.id
    assert store.find_by_address("https://example.com/nothing") is None


# ── Некорректные адреса ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "address",
    [
        "",
        "   ",
        "не ссылка",
        "ftp://example.com/file",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "tg://resolve?domain=channel",
        "https://",
        "http://",
        "example.com/blog",
        "https://user:password@example.com/blog",
    ],
)
def test_invalid_addresses_are_rejected(
    store: SourceRegistryStore, address: str
) -> None:
    with pytest.raises(SourceAddressError):
        store.add(address)

    assert store.list() == ()


def test_url_normalization_rules() -> None:
    assert normalize_url("  https://Example.com/  ") == "https://example.com"
    assert normalize_url("https://example.com/a?b=1#c") == "https://example.com/a?b=1#c"
    assert normalize_url("https://example.com:8443/a") == "https://example.com:8443/a"
    assert normalize_url("ftp://example.com") == ""
    assert normalize_url("https://user:pass@example.com") == ""


def test_platform_aliases_map_to_one_value() -> None:
    for alias in ("website", "site", "url", "WEBSITE"):
        assert normalize_platform(alias) == PLATFORM_WEB


def test_broken_file_is_not_silently_replaced(tmp_path: Path) -> None:
    path = tmp_path / "sources.json"
    path.write_text("{ broken", encoding="utf-8")
    store = SourceRegistryStore(path, seed_path=_EMPTY_SEED)

    with pytest.raises(SourceRegistryError):
        store.add("https://example.com/blog")

    assert path.read_text(encoding="utf-8") == "{ broken"


# ── Persistence ──────────────────────────────────────────────────────────────


def test_sources_survive_a_new_store_over_the_same_file(tmp_path: Path) -> None:
    path = tmp_path / "sources.json"
    created = SourceRegistryStore(path, seed_path=_EMPTY_SEED).add("https://example.com/blog", enabled=False)

    reopened = SourceRegistryStore(path, seed_path=_EMPTY_SEED).get(created.id)

    assert reopened is not None
    assert reopened.url == created.url
    assert reopened.enabled is False
    assert reopened.created_at == created.created_at


def test_toggle_survives_reopening(tmp_path: Path) -> None:
    path = tmp_path / "sources.json"
    source = SourceRegistryStore(path, seed_path=_EMPTY_SEED).add("https://example.com/blog")
    SourceRegistryStore(path, seed_path=_EMPTY_SEED).disable(source.id)

    assert SourceRegistryStore(path, seed_path=_EMPTY_SEED).get(source.id).enabled is False


def test_removal_survives_reopening(tmp_path: Path) -> None:
    path = tmp_path / "sources.json"
    source = SourceRegistryStore(path, seed_path=_EMPTY_SEED).add("https://example.com/blog")
    SourceRegistryStore(path, seed_path=_EMPTY_SEED).remove(source.id)

    assert SourceRegistryStore(path, seed_path=_EMPTY_SEED).get(source.id) is None


def test_stored_file_is_readable_by_the_plain_loader(tmp_path: Path) -> None:
    # Управляемый реестр и read-only загрузчик работают с одним форматом:
    # запись через магазин не создаёт параллельного хранилища.
    path = tmp_path / "sources.json"
    store = SourceRegistryStore(path, seed_path=_EMPTY_SEED)
    source = store.add("https://t.me/some_public_channel")

    loaded = load_registry(path, use_cache=False)

    assert loaded.get(source.id) is not None


def test_unknown_fields_of_other_sources_are_preserved(tmp_path: Path) -> None:
    path = tmp_path / "sources.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "description": "Комментарий верхнего уровня",
                "sources": [
                    {
                        "id": "legacy",
                        "name": "Legacy",
                        "platform": "web",
                        "url": "https://legacy.example.com",
                        "source_type": "monitored_source",
                        "purpose": "mixed",
                        "future_field": {"kept": True},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    SourceRegistryStore(path, seed_path=_EMPTY_SEED).add("https://example.com/blog")

    payload = _payload(path)
    assert payload["description"] == "Комментарий верхнего уровня"
    assert payload["sources"][0]["future_field"] == {"kept": True}


# ── Production-данные не сломаны ─────────────────────────────────────────────


def test_real_registry_still_loads() -> None:
    assert len(load_registry()) > 0


def test_real_registry_is_readable_through_the_store() -> None:
    # Магазин не заводит своё хранилище: пока рабочего файла нет, и он,
    # и загрузчик показывают один и тот же стартовый набор.
    from app.services.source_registry import SEED_REGISTRY_PATH, runtime_registry_path

    store = SourceRegistryStore()

    assert store.path == runtime_registry_path()
    assert store.seed_path == SEED_REGISTRY_PATH
    assert len(store.list()) == len(load_registry())


def test_store_does_not_write_into_the_seed_file() -> None:
    # Стартовый набор — часть поставки: магазин пишет только в рабочий файл.
    from app.services.source_registry import SEED_REGISTRY_PATH

    store = SourceRegistryStore()
    assert store.path != SEED_REGISTRY_PATH
    assert not store.path.exists(), (
        "тесты не должны разворачивать рабочий реестр — проверьте conftest.py"
    )


def test_store_module_contains_no_specific_channels() -> None:
    # Guard из test_source_registry.py покрывает весь app/, но проверка
    # рядом с новым кодом делает требование явным.
    text = (PROJECT_ROOT / "app" / "services" / "source_registry_store.py").read_text(
        encoding="utf-8"
    )
    for source in load_registry():
        if source.username:
            assert source.username.lower() not in text.lower()
