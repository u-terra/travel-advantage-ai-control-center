from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from app.domain.sources import (
    KNOWN_PLATFORMS,
    Source,
    is_valid_telegram_username,
    normalize_telegram_username,
    telegram_url,
)
from app.keyboards import WEB_RESOURCE_LINKS
from app.services.source_registry import (
    DEFAULT_REGISTRY_PATH,
    IMPLEMENTED_COLLECTOR_PLATFORMS,
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    SourceRegistryError,
    clear_cache,
    collection_targets,
    load_registry,
    parse_registry,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Каналы, добавленные в реестр. Здесь, в тесте, они перечислены намеренно:
# именно так проверяется, что в самой бизнес-логике их нет.
NEW_TELEGRAM_USERNAMES = (
    "alexperevez",
    "cashflow21123",
    "artemedanium",
    "pro_biznes_zharkov",
    "koroleva_60",
    "otziviMWR",
)


def _write_registry(
    tmp_path: Path, sources: list[dict], **extra: object
) -> Path:
    path = tmp_path / "sources.json"
    payload: dict[str, object] = {"schema_version": 1, "sources": sources}
    payload.update(extra)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _source_payload(**overrides) -> dict:
    payload = {
        "id": "telegram_example",
        "name": "@example",
        "platform": "telegram",
        "username": "example",
        "source_type": "monitored_source",
        "purpose": "content_and_business_signals",
        "enabled": True,
    }
    payload.update(overrides)
    return payload


@pytest.fixture(autouse=True)
def _clean_registry_cache():
    clear_cache()
    yield
    clear_cache()


# ── Загрузка реестра ─────────────────────────────────────────────────────────


def test_default_registry_file_exists():
    assert DEFAULT_REGISTRY_PATH.is_file()


def test_default_registry_loads():
    registry = load_registry()
    assert len(registry) > 0
    for source in registry:
        assert isinstance(source, Source)
        assert source.id
        assert source.name
        assert source.platform
        assert source.source_type
        assert source.purpose


def test_registry_loads_from_arbitrary_path(tmp_path):
    path = _write_registry(tmp_path, [_source_payload()])
    registry = load_registry(path)

    assert len(registry) == 1
    assert registry.get("telegram_example") is not None
    assert registry.get("missing_id") is None


# ── schema_version ───────────────────────────────────────────────────────────


def test_data_file_declares_current_schema_version():
    payload = json.loads(DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))
    assert payload["schema_version"] == SCHEMA_VERSION
    assert SCHEMA_VERSION in SUPPORTED_SCHEMA_VERSIONS


def test_supported_schema_version_loads(tmp_path):
    path = _write_registry(tmp_path, [_source_payload()], schema_version=1)
    assert len(load_registry(path)) == 1


def test_missing_schema_version_is_treated_as_version_one(tmp_path):
    path = tmp_path / "sources.json"
    path.write_text(
        json.dumps({"sources": [_source_payload()]}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert len(load_registry(path)) == 1


def test_future_schema_version_is_rejected(tmp_path):
    path = _write_registry(tmp_path, [_source_payload()], schema_version=2)
    with pytest.raises(SourceRegistryError):
        load_registry(path)


def test_non_numeric_schema_version_is_rejected(tmp_path):
    path = _write_registry(tmp_path, [_source_payload()], schema_version="latest")
    with pytest.raises(SourceRegistryError):
        load_registry(path)


# ── Расширяемость платформ ───────────────────────────────────────────────────


def test_unknown_platform_loads_without_code_changes(tmp_path):
    path = _write_registry(
        tmp_path,
        [
            _source_payload(
                id="yt_example",
                platform="youtube",
                username=None,
                url="https://youtube.com/@example",
                source_type="monitored_source",
                purpose="content_and_business_signals",
            ),
            _source_payload(
                id="dzen_example",
                platform="dzen",
                username=None,
                url="https://dzen.ru/example",
                source_type="monitored_source",
                purpose="content_and_business_signals",
            ),
        ],
    )
    registry = load_registry(path)

    assert registry.platforms() == ("dzen", "youtube")
    assert [source.id for source in registry.by_platform("youtube")] == ["yt_example"]
    # Неизвестная платформа не ломает и общую выборку для сбора.
    assert len(collection_targets(registry=registry)) == 2


def test_registry_membership_does_not_imply_an_implemented_collector():
    # Реестр отвечает на вопрос «за чем наблюдаем», а не «что уже собирается».
    # В этом репозитории сборщиков нет вообще — сбор живёт в Lead Radar.
    assert IMPLEMENTED_COLLECTOR_PLATFORMS == frozenset()

    registry = load_registry()
    assert len(registry.enabled()) > 0
    for source in registry.enabled():
        assert source.platform not in IMPLEMENTED_COLLECTOR_PLATFORMS


def test_data_file_states_that_sources_are_not_yet_monitored():
    payload = json.loads(DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))
    assert payload["status_note"].strip()


# ── Неизвестные поля ─────────────────────────────────────────────────────────


def test_unknown_extra_fields_are_ignored_safely(tmp_path):
    payload = _source_payload(
        id="tg_extra",
        username="extra_channel",
        unknown_field="что-то новое",
        nested={"a": [1, 2, 3]},
        enabled_typo=False,
    )
    source = load_registry(_write_registry(tmp_path, [payload])).get("tg_extra")

    assert source is not None
    # Опечатка в имени поля не должна тихо отключить источник.
    assert source.enabled is True
    assert not hasattr(source, "unknown_field")
    assert not hasattr(source, "nested")


def test_top_level_unknown_keys_are_ignored(tmp_path):
    path = _write_registry(
        tmp_path, [_source_payload()], description="комментарий", future_key=[1, 2]
    )
    assert len(load_registry(path)) == 1


def test_registry_platforms_are_known_or_explicitly_extended():
    # Список платформ открытый, но всё, что лежит в реестре сейчас,
    # должно быть из известного набора — иначе это опечатка.
    for source in load_registry():
        assert source.platform in KNOWN_PLATFORMS


def test_registry_ids_are_unique():
    ids = [source.id for source in load_registry()]
    assert len(ids) == len(set(ids))


def test_duplicate_ids_are_rejected(tmp_path):
    path = _write_registry(
        tmp_path, [_source_payload(), _source_payload(name="@example-2")]
    )
    with pytest.raises(SourceRegistryError):
        load_registry(path)


def test_missing_required_field_is_rejected(tmp_path):
    payload = _source_payload()
    del payload["purpose"]
    path = _write_registry(tmp_path, [payload])
    with pytest.raises(SourceRegistryError):
        load_registry(path)


def test_missing_file_is_rejected(tmp_path):
    with pytest.raises(SourceRegistryError):
        load_registry(tmp_path / "does-not-exist.json")


def test_broken_json_is_rejected(tmp_path):
    path = tmp_path / "sources.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(SourceRegistryError):
        load_registry(path)


def test_non_telegram_source_requires_url_or_username(tmp_path):
    payload = _source_payload(
        id="web_broken", platform="web", username=None, url=None
    )
    payload.pop("username", None)
    path = _write_registry(tmp_path, [payload])
    with pytest.raises(SourceRegistryError):
        load_registry(path)


# ── enabled / disabled ───────────────────────────────────────────────────────


def test_enabled_and_disabled_are_separated(tmp_path):
    path = _write_registry(
        tmp_path,
        [
            _source_payload(id="tg_on", username="on", enabled=True),
            _source_payload(id="tg_off", username="off", enabled=False),
        ],
    )
    registry = load_registry(path)

    assert [source.id for source in registry.enabled()] == ["tg_on"]
    assert [source.id for source in registry.disabled()] == ["tg_off"]
    assert len(registry.all()) == 2


def test_disabled_source_never_reaches_collection(tmp_path):
    path = _write_registry(
        tmp_path,
        [
            _source_payload(id="tg_on", username="on", enabled=True),
            _source_payload(id="tg_off", username="off", enabled=False),
        ],
    )
    targets = collection_targets(path=path)

    ids = [source.id for source in targets]
    assert "tg_on" in ids
    assert "tg_off" not in ids


def test_enabled_defaults_to_true_when_field_absent(tmp_path):
    payload = _source_payload()
    del payload["enabled"]
    path = _write_registry(tmp_path, [payload])

    assert load_registry(path).enabled()[0].id == "telegram_example"


def test_collection_targets_can_filter_by_platform(tmp_path):
    path = _write_registry(
        tmp_path,
        [
            _source_payload(id="tg_one", username="one"),
            _source_payload(
                id="web_one",
                platform="web",
                username=None,
                url="https://example.org",
                source_type="own_web_resource",
                purpose="product_reference",
            ),
        ],
    )
    telegram_only = collection_targets(platform="telegram", path=path)

    assert [source.id for source in telegram_only] == ["tg_one"]


def test_collection_targets_can_filter_by_source_type(tmp_path):
    path = _write_registry(
        tmp_path,
        [
            _source_payload(id="tg_monitored", username="one"),
            _source_payload(
                id="tg_reviews", username="two", source_type="reviews_and_stories"
            ),
        ],
    )
    reviews = collection_targets(source_types=["reviews_and_stories"], path=path)

    assert [source.id for source in reviews] == ["tg_reviews"]


def test_enabled_sources_are_ordered_by_priority(tmp_path):
    path = _write_registry(
        tmp_path,
        [
            _source_payload(id="tg_low", username="low", priority=90),
            _source_payload(id="tg_high", username="high", priority=10),
        ],
    )
    assert [source.id for source in load_registry(path).enabled()] == [
        "tg_high",
        "tg_low",
    ]


# ── Telegram username и t.me URL ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        "alexperevez",
        "@alexperevez",
        "t.me/alexperevez",
        "https://t.me/alexperevez",
        "https://t.me/alexperevez/",
        "http://t.me/alexperevez",
        "https://t.me/s/alexperevez",
        "https://t.me/alexperevez/1234",
        "https://t.me/alexperevez?before=10",
        "https://telegram.me/alexperevez",
        "  https://t.me/alexperevez  ",
    ],
)
def test_telegram_username_normalization(raw):
    assert normalize_telegram_username(raw) == "alexperevez"


def test_telegram_username_normalization_on_empty_input():
    assert normalize_telegram_username("") == ""
    assert normalize_telegram_username("   ") == ""
    assert telegram_url("") == ""


@pytest.mark.parametrize(
    "raw",
    [
        "tg://resolve?domain=evil",       # неподдерживаемая схема
        "https://evil.com/t.me/channel",  # чужой хост
        "https://t.me/+privatehash",      # приватное приглашение
        "https://t.me/joinchat/AAAABBBB",  # приватное приглашение
        "имя канала",                     # пробелы и кириллица
        "chan-nel",                       # дефис недопустим
        "t.me/",
        "@",
        "/",
    ],
)
def test_ambiguous_telegram_input_yields_no_username(raw):
    # Ключевая гарантия: из мусора нельзя «угадать» канал. Иначе неверная
    # догадка превратилась бы в канонический https://t.me/<чужой-канал>.
    assert normalize_telegram_username(raw) == ""
    assert telegram_url(raw) == ""


@pytest.mark.parametrize(
    "raw", ["tg://resolve?domain=evil", "https://evil.com/t.me/channel", "chan-nel"]
)
def test_ambiguous_telegram_source_is_rejected_by_loader(raw, tmp_path):
    payload = _source_payload(id="tg_bad", username=raw)
    with pytest.raises(SourceRegistryError):
        load_registry(_write_registry(tmp_path, [payload]))


def test_valid_username_predicate():
    assert is_valid_telegram_username("koroleva_60") is True
    assert is_valid_telegram_username("otziviMWR") is True
    assert is_valid_telegram_username("chan-nel") is False
    assert is_valid_telegram_username("") is False


def test_telegram_url_is_canonical():
    assert telegram_url("@koroleva_60") == "https://t.me/koroleva_60"
    assert telegram_url("https://t.me/koroleva_60/7") == "https://t.me/koroleva_60"


def test_telegram_source_defined_by_url_gets_username(tmp_path):
    payload = _source_payload(id="tg_by_url", url="https://t.me/some_channel")
    payload.pop("username")
    registry = load_registry(_write_registry(tmp_path, [payload]))
    source = registry.get("tg_by_url")

    assert source is not None
    assert source.username == "some_channel"
    assert source.url == "https://t.me/some_channel"
    assert source.handle == "@some_channel"
    assert source.target == "https://t.me/some_channel"


def test_telegram_source_defined_by_handle_gets_url(tmp_path):
    payload = _source_payload(id="tg_by_handle", username="@some_channel")
    registry = load_registry(_write_registry(tmp_path, [payload]))
    source = registry.get("tg_by_handle")

    assert source is not None
    assert source.username == "some_channel"
    assert source.target == "https://t.me/some_channel"


def test_telegram_source_without_any_address_is_rejected(tmp_path):
    payload = _source_payload(id="tg_broken", username="")
    with pytest.raises(SourceRegistryError):
        parse_registry({"sources": [payload]})


def test_non_telegram_source_has_no_handle(tmp_path):
    payload = _source_payload(
        id="web_one",
        platform="web",
        url="https://example.org",
        source_type="own_web_resource",
        purpose="product_reference",
    )
    payload.pop("username")
    source = load_registry(_write_registry(tmp_path, [payload])).get("web_one")

    assert source is not None
    assert source.handle is None
    assert source.target == "https://example.org"


# ── Новые Telegram-источники в реестре ───────────────────────────────────────


def test_all_six_new_telegram_sources_are_registered():
    telegram = load_registry().by_platform("telegram", only_enabled=False)
    usernames = {source.username for source in telegram}

    for expected in NEW_TELEGRAM_USERNAMES:
        assert expected in usernames


def test_new_telegram_sources_are_enabled_and_have_canonical_urls():
    active = collection_targets(platform="telegram")
    by_username = {source.username: source for source in active}

    for expected in NEW_TELEGRAM_USERNAMES:
        source = by_username[expected]
        assert source.enabled is True
        assert source.url == f"https://t.me/{expected}"
        assert source.target == f"https://t.me/{expected}"
        assert source.handle == f"@{expected}"


def test_reviews_source_purpose():
    source = load_registry().get("telegram_otzivimwr")

    assert source is not None
    assert source.username == "otziviMWR"
    assert source.source_type == "reviews_and_stories"
    assert source.purpose == "reviews_user_stories_and_content_ideas"


def test_neutral_purpose_for_sources_without_known_content():
    registry = load_registry()
    neutral = [
        username for username in NEW_TELEGRAM_USERNAMES if username != "otziviMWR"
    ]
    by_username = {source.username: source for source in registry}

    for username in neutral:
        source = by_username[username]
        assert source.source_type == "monitored_source"
        assert source.purpose == "content_and_business_signals"


# ── Существующие источники не потеряны ───────────────────────────────────────


def test_existing_web_resources_are_present_in_registry():
    registry_urls = {
        source.url for source in load_registry().by_platform("web", only_enabled=False)
    }

    for _title, url in WEB_RESOURCE_LINKS:
        assert url in registry_urls


def test_existing_web_resources_stay_enabled():
    web_urls = {source.url for source in collection_targets(platform="web")}

    for _title, url in WEB_RESOURCE_LINKS:
        assert url in web_urls


def test_lead_radar_platform_names_are_still_supported():
    # Lead Radar исторически использует source_type "rss" и "vk".
    # Реестр обязан уметь их описывать, иначе существующие источники
    # некуда будет перенести.
    assert "rss" in KNOWN_PLATFORMS
    assert "vk" in KNOWN_PLATFORMS


# ── Конкретные каналы не зашиты в бизнес-логику ──────────────────────────────


def _business_logic_files() -> list[Path]:
    files = sorted(PROJECT_ROOT.joinpath("app").rglob("*.py"))
    files += sorted(PROJECT_ROOT.joinpath("scripts").rglob("*.py"))
    return files


def test_new_channels_are_absent_from_business_logic():
    for path in _business_logic_files():
        text = path.read_text(encoding="utf-8").lower()
        for username in NEW_TELEGRAM_USERNAMES:
            assert username.lower() not in text, (
                f"{path.relative_to(PROJECT_ROOT)} содержит конкретный источник "
                f"'{username}'. Источники живут только в config/sources.json."
            )


def test_no_telegram_source_from_registry_leaks_into_business_logic():
    # Ни один Telegram-источник реестра (ни username, ни t.me-ссылка)
    # не должен встречаться в коде — иначе добавление канала снова
    # потребует правки Python.
    telegram = load_registry().by_platform("telegram", only_enabled=False)
    files = _business_logic_files()

    for source in telegram:
        for needle in filter(None, (source.username, source.url)):
            for path in files:
                text = path.read_text(encoding="utf-8").lower()
                assert needle.lower() not in text, (
                    f"{path.relative_to(PROJECT_ROOT)} содержит '{needle}' "
                    "из реестра источников."
                )


def _code_string_literals(path: Path) -> list[str]:
    """Строковые константы файла без докстрингов (комментарии в AST не входят)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_business_logic_has_no_hardcoded_tme_links():
    # Допустима только сборка канонического URL из username (`t.me/` + значение),
    # но не готовая ссылка на конкретный канал.
    hardcoded = re.compile(r"t\.me/[A-Za-z0-9_]")
    for path in _business_logic_files():
        for literal in _code_string_literals(path):
            assert not hardcoded.search(literal), (
                f"{path.relative_to(PROJECT_ROOT)} содержит зашитую "
                f"t.me-ссылку: {literal!r}"
            )


def test_registry_data_file_is_the_only_place_with_channels():
    data_text = DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8")
    for username in NEW_TELEGRAM_USERNAMES:
        assert username in data_text
