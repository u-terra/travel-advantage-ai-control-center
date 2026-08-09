from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Iterable, Optional

from app.domain.sources import (
    PLATFORM_TELEGRAM,
    Source,
    normalize_telegram_username,
    telegram_url,
)

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Реестр живёт в двух файлах с разными ролями.
#
# SEED — стартовый набор источников. Лежит в репозитории, деплоится вместе с
# кодом и во время работы приложения НЕ изменяется. Из него один раз
# разворачивается runtime-файл.
#
# RUNTIME — рабочее состояние: сюда попадают источники, добавленные владельцем.
# Файл в Git не отслеживается (`data/` в .gitignore) и деплоем не переносится,
# поэтому обновление кода не может стереть добавленные источники.
#
# Разделение существует именно ради этого: один файл не может быть
# одновременно версионируемой частью поставки и изменяемым состоянием — при
# следующем деплое версия из репозитория затёрла бы правки владельца.
SEED_REGISTRY_PATH = _PROJECT_ROOT / "config" / "sources.json"
DEFAULT_RUNTIME_REGISTRY_PATH = _PROJECT_ROOT / "data" / "sources.json"

# Путь runtime-файла можно переопределить — так же, как JOURNAL_DB_PATH.
RUNTIME_REGISTRY_PATH_ENV = "SOURCE_REGISTRY_PATH"

# Историческое имя: раньше seed и runtime были одним файлом. Оставлено, чтобы
# существующий код и тесты, которым нужен именно стартовый набор, продолжали
# работать без правок.
DEFAULT_REGISTRY_PATH = SEED_REGISTRY_PATH


def runtime_registry_path() -> Path:
    """Путь рабочего файла реестра (может не существовать до bootstrap)."""
    raw = os.environ.get(RUNTIME_REGISTRY_PATH_ENV, "").strip()
    return Path(raw) if raw else DEFAULT_RUNTIME_REGISTRY_PATH


def active_registry_path() -> Path:
    """Файл, который является источником истины прямо сейчас.

    После bootstrap это всегда runtime-файл. Пока его нет — seed, чтобы
    read-only сценарии (например, `scripts/list_sources.py`) работали на
    свежей машине и НЕ создавали рабочее состояние побочным эффектом чтения.
    """
    runtime = runtime_registry_path()
    return runtime if runtime.exists() else SEED_REGISTRY_PATH

# Версия формата data-файла. Поднимается при семантически значимом изменении
# структуры; загрузчик отказывается читать файл новее, чем понимает.
#   v1 — базовый формат, блока `collector` нет;
#   v2 — добавлен необязательный `collector` с настройками сборщика.
# v1 продолжает читаться: старый файл остаётся валидным.
SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({1, 2})
_COLLECTOR_SINCE_VERSION = 2

# Платформы, для которых в ЭТОМ репозитории есть код сбора.
# Пусто осознанно: Control Center сейчас ничего не собирает сам — он читает
# уже готовые сигналы Lead Radar. Наличие источника в реестре означает
# «за этим наблюдаем по замыслу», а не «это уже физически мониторится».
IMPLEMENTED_COLLECTOR_PLATFORMS: frozenset[str] = frozenset()

_REQUIRED_FIELDS = ("id", "name", "platform", "source_type", "purpose")

# Известные настройки сборщика и их типы. Проверяются строго, чтобы у поля
# не поменялся смысл молча: например, строка "false" в булевом флаге дала бы
# True и включила сбор там, где его выключали. Неизвестные ключи сохраняются
# как есть и реестром не интерпретируются.
_COLLECTOR_BOOL_KEYS = ("posts_enabled", "comments_enabled", "drop_promo")
_COLLECTOR_TEXT_KEYS = ("analysis_profile", "language", "vk_screen_name")
_COLLECTOR_COUNT_KEYS = ("fetch_limit", "min_score_for_llm")
_COLLECTOR_OPTIONAL_ID_KEYS = ("vk_group_id",)


class SourceRegistryError(ValueError):
    """Реестр источников не удалось прочитать или он не прошёл валидацию."""


@dataclass(frozen=True)
class SourceRegistry:
    """Загруженный реестр источников, доступный только на чтение."""

    sources: tuple[Source, ...]

    def __iter__(self):
        return iter(self.sources)

    def __len__(self) -> int:
        return len(self.sources)

    def all(self) -> tuple[Source, ...]:
        return self.sources

    def enabled(self) -> tuple[Source, ...]:
        """Только активные источники — отсортированы по priority, затем по id."""
        active = [source for source in self.sources if source.enabled]
        active.sort(key=lambda source: (source.priority, source.id))
        return tuple(active)

    def disabled(self) -> tuple[Source, ...]:
        return tuple(source for source in self.sources if not source.enabled)

    def get(self, source_id: str) -> Optional[Source]:
        for source in self.sources:
            if source.id == source_id:
                return source
        return None

    def by_platform(
        self, platform: str, *, only_enabled: bool = True
    ) -> tuple[Source, ...]:
        pool = self.enabled() if only_enabled else self.sources
        key = (platform or "").strip().lower()
        return tuple(source for source in pool if source.platform == key)

    def by_source_type(
        self, source_type: str, *, only_enabled: bool = True
    ) -> tuple[Source, ...]:
        pool = self.enabled() if only_enabled else self.sources
        key = (source_type or "").strip().lower()
        return tuple(source for source in pool if source.source_type == key)

    def platforms(self) -> tuple[str, ...]:
        return tuple(sorted({source.platform for source in self.sources}))


# ── Загрузка реестра ─────────────────────────────────────────────────────────
#
# Файл читается один раз и кешируется по (путь, mtime, размер): правка файла
# подхватывается при следующем обращении, повторное чтение диска не нужно.

_CACHE: dict[tuple[str, int, int], SourceRegistry] = {}
_CACHE_LOCK = Lock()


def _as_bool(value: object, *, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


def _as_int(value: object, *, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _parse_collector(raw: object, *, source_id: str) -> dict[str, object]:
    """Проверяет блок `collector` и возвращает его как есть.

    Реестр не толкует смысл настроек — он лишь следит, чтобы известные ключи
    имели ожидаемый тип. Неизвестные ключи сохраняются нетронутыми: их поймёт
    сборщик своей платформы.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SourceRegistryError(
            f"источник '{source_id}': 'collector' должен быть объектом"
        )

    def fail(key: str, expected: str) -> None:
        raise SourceRegistryError(
            f"источник '{source_id}': 'collector.{key}' должен быть {expected}, "
            f"получено: {raw[key]!r}"
        )

    for key in _COLLECTOR_BOOL_KEYS:
        if key in raw and not isinstance(raw[key], bool):
            fail(key, "булевым (true/false без кавычек)")

    for key in _COLLECTOR_TEXT_KEYS:
        if key in raw and (not isinstance(raw[key], str) or not raw[key].strip()):
            fail(key, "непустой строкой")

    for key in _COLLECTOR_COUNT_KEYS:
        if key in raw:
            value = raw[key]
            # bool — подкласс int, но булев флаг здесь означал бы ошибку.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                fail(key, "числом")
            if value < 0:
                fail(key, "неотрицательным числом")

    for key in _COLLECTOR_OPTIONAL_ID_KEYS:
        if key in raw and raw[key] is not None:
            if isinstance(raw[key], bool) or not isinstance(raw[key], int):
                fail(key, "целым числом или null")

    return dict(raw)


def _parse_source(raw: object, *, index: int, schema_version: int) -> Source:
    if not isinstance(raw, dict):
        raise SourceRegistryError(f"источник #{index}: ожидался объект")

    values: dict[str, str] = {}
    for field in _REQUIRED_FIELDS:
        value = str(raw.get(field) or "").strip()
        if not value:
            raise SourceRegistryError(
                f"источник #{index}: не заполнено обязательное поле '{field}'"
            )
        values[field] = value

    platform = values["platform"].lower()
    source_type = values["source_type"].lower()
    purpose = values["purpose"].lower()

    url = str(raw.get("url") or "").strip() or None
    username = str(raw.get("username") or "").strip() or None

    if platform == PLATFORM_TELEGRAM:
        # Telegram-источник можно задать любым способом: `@name`, `name`
        # или ссылкой t.me — внутри всегда остаётся нормализованный username
        # и канонический https://t.me/<username>.
        normalized = normalize_telegram_username(username or url or "")
        if not normalized:
            raise SourceRegistryError(
                f"источник '{values['id']}': не удалось определить "
                "Telegram-username из полей 'username'/'url'"
            )
        username = normalized
        url = telegram_url(normalized)
    elif not url and not username:
        raise SourceRegistryError(
            f"источник '{values['id']}': нужно указать 'url' или 'username'"
        )

    if "collector" in raw and schema_version < _COLLECTOR_SINCE_VERSION:
        raise SourceRegistryError(
            f"источник '{values['id']}': блок 'collector' появился в "
            f"schema_version {_COLLECTOR_SINCE_VERSION}, "
            f"а файл объявлен как версия {schema_version}"
        )

    return Source(
        id=values["id"],
        name=values["name"],
        platform=platform,
        source_type=source_type,
        purpose=purpose,
        enabled=_as_bool(raw.get("enabled")),
        url=url,
        username=username,
        priority=_as_int(raw.get("priority"), default=50),
        notes=str(raw.get("notes") or "").strip(),
        collector=_parse_collector(raw.get("collector"), source_id=values["id"]),
        # Отметки времени намеренно не привязаны к schema_version: читатель
        # старого кода просто их игнорирует, и поведение сбора от этого не
        # меняется — в отличие от `collector`, где потеря настройки изменила бы
        # режим работы.
        created_at=str(raw.get("created_at") or "").strip(),
        updated_at=str(raw.get("updated_at") or "").strip(),
    )


def _check_schema_version(payload: dict) -> int:
    """Проверяет `schema_version` и возвращает её.

    Отсутствие поля трактуется как версия 1: так читаются файлы, созданные
    до появления версионирования.
    """
    raw = payload.get("schema_version", 1)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise SourceRegistryError(
            f"'schema_version' должен быть целым числом, получено: {raw!r}"
        )
    if raw not in SUPPORTED_SCHEMA_VERSIONS:
        raise SourceRegistryError(
            f"неподдерживаемая версия реестра источников: {raw}. "
            f"Поддерживается: {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
        )
    return raw


def parse_registry(payload: object) -> SourceRegistry:
    """Собирает реестр из уже разобранного JSON (без обращения к диску)."""
    if not isinstance(payload, dict):
        raise SourceRegistryError("ожидался объект с ключом 'sources'")

    schema_version = _check_schema_version(payload)

    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list):
        raise SourceRegistryError("ключ 'sources' должен быть списком")

    sources: list[Source] = []
    seen: set[str] = set()
    # Два активных источника с одним адресом дали бы двойной сбор и двойной
    # счёт сигналов, поэтому это ошибка файла, а не мелочь. Выключенная копия
    # допустима: так живут кандидаты рядом с работающим источником.
    seen_addresses: dict[str, str] = {}
    for index, raw in enumerate(raw_sources, start=1):
        source = _parse_source(raw, index=index, schema_version=schema_version)
        if source.id in seen:
            raise SourceRegistryError(f"дублирующийся id источника: '{source.id}'")
        seen.add(source.id)
        if source.enabled and source.identity_key:
            previous = seen_addresses.get(source.identity_key)
            if previous is not None:
                raise SourceRegistryError(
                    f"источник '{source.id}' повторяет адрес активного "
                    f"источника '{previous}': {source.target}"
                )
            seen_addresses[source.identity_key] = source.id
        sources.append(source)

    return SourceRegistry(sources=tuple(sources))


def load_registry(path: Optional[Path] = None, *, use_cache: bool = True) -> SourceRegistry:
    """Читает реестр источников из data-файла.

    Без явного пути читается активный реестр (`active_registry_path()`):
    рабочее состояние, если оно уже развёрнуто, иначе seed. Так UI, сбор и
    консольные скрипты всегда видят один и тот же набор источников.

    Ошибки чтения и валидации поднимаются как `SourceRegistryError` — тихо
    подставлять пустой список нельзя: молчаливо пустой реестр выглядел бы как
    «источников нет», хотя на самом деле сломан файл.
    """
    registry_path = Path(path) if path is not None else active_registry_path()

    try:
        stat = registry_path.stat()
    except OSError as exc:
        raise SourceRegistryError(
            f"файл реестра источников недоступен: {registry_path}"
        ) from exc

    cache_key = (str(registry_path.resolve()), stat.st_mtime_ns, stat.st_size)
    if use_cache:
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
        if cached is not None:
            return cached

    try:
        raw_text = registry_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SourceRegistryError(
            f"не удалось прочитать реестр источников: {registry_path}"
        ) from exc

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise SourceRegistryError(
            f"некорректный JSON в реестре источников: {registry_path}"
        ) from exc

    registry = parse_registry(payload)
    if use_cache:
        with _CACHE_LOCK:
            _CACHE[cache_key] = registry
    return registry


def clear_cache() -> None:
    """Сбрасывает кеш загруженных реестров (нужно тестам и перезагрузке)."""
    with _CACHE_LOCK:
        _CACHE.clear()


# ── Точка входа для кода сбора ───────────────────────────────────────────────


def collection_targets(
    *,
    platform: Optional[str] = None,
    source_types: Optional[Iterable[str]] = None,
    registry: Optional[SourceRegistry] = None,
    path: Optional[Path] = None,
) -> tuple[Source, ...]:
    """Активные источники, которые сбор ДОЛЖЕН обрабатывать.

    Это «список намерения», а не факт мониторинга. Функция отвечает на вопрос
    «за чем мы хотим наблюдать», а не «что уже собирается»: кода сбора в этом
    репозитории нет (см. `IMPLEMENTED_COLLECTOR_PLATFORMS`), сбор живёт в
    отдельном проекте Travel Lead Radar. Возврат источника отсюда НЕ означает,
    что он уже физически читается.

    Конкретные каналы нигде в бизнес-логике не перечислены — это единственный
    способ их узнать. Источники с `enabled: false` в выборку не попадают
    никогда.

    Сбор использует источники только для анализа и генерации нового
    материала — публикации не копируются как готовый контент.
    """
    active = (registry or load_registry(path)).enabled()

    if platform:
        key = platform.strip().lower()
        active = tuple(source for source in active if source.platform == key)

    if source_types is not None:
        allowed = {str(value).strip().lower() for value in source_types}
        active = tuple(source for source in active if source.source_type in allowed)

    return tuple(active)
