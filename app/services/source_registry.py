from __future__ import annotations

import json
import logging
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

# Единственное место, где перечислены конкретные источники, — этот data-файл.
# Чтобы добавить новый канал, правится только он: Python-код менять не нужно.
DEFAULT_REGISTRY_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "sources.json"
)

# Версия формата data-файла. Поднимается только при несовместимом изменении
# структуры; загрузчик отказывается читать файл новее, чем понимает.
SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({SCHEMA_VERSION})

# Платформы, для которых в ЭТОМ репозитории есть код сбора.
# Пусто осознанно: Control Center сейчас ничего не собирает сам — он читает
# уже готовые сигналы Lead Radar. Наличие источника в реестре означает
# «за этим наблюдаем по замыслу», а не «это уже физически мониторится».
IMPLEMENTED_COLLECTOR_PLATFORMS: frozenset[str] = frozenset()

_REQUIRED_FIELDS = ("id", "name", "platform", "source_type", "purpose")


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


def _parse_source(raw: object, *, index: int) -> Source:
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
    )


def _check_schema_version(payload: dict) -> None:
    """Проверяет `schema_version`. Отсутствие поля трактуется как версия 1."""
    raw = payload.get("schema_version", SCHEMA_VERSION)
    try:
        version = int(raw)
    except (TypeError, ValueError):
        raise SourceRegistryError(
            f"'schema_version' должен быть целым числом, получено: {raw!r}"
        ) from None
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise SourceRegistryError(
            f"неподдерживаемая версия реестра источников: {version}. "
            f"Поддерживается: {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
        )


def parse_registry(payload: object) -> SourceRegistry:
    """Собирает реестр из уже разобранного JSON (без обращения к диску)."""
    if not isinstance(payload, dict):
        raise SourceRegistryError("ожидался объект с ключом 'sources'")

    _check_schema_version(payload)

    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list):
        raise SourceRegistryError("ключ 'sources' должен быть списком")

    sources: list[Source] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_sources, start=1):
        source = _parse_source(raw, index=index)
        if source.id in seen:
            raise SourceRegistryError(f"дублирующийся id источника: '{source.id}'")
        seen.add(source.id)
        sources.append(source)

    return SourceRegistry(sources=tuple(sources))


def load_registry(path: Optional[Path] = None, *, use_cache: bool = True) -> SourceRegistry:
    """Читает реестр источников из data-файла.

    По умолчанию используется `config/sources.json`. Ошибки чтения и
    валидации поднимаются как `SourceRegistryError` — тихо подставлять
    пустой список нельзя: молчаливо пустой реестр выглядел бы как
    «источников нет», хотя на самом деле сломан файл.
    """
    registry_path = Path(path) if path is not None else DEFAULT_REGISTRY_PATH

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
