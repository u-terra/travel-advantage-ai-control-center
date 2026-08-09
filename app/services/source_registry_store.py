"""Управление реестром источников: добавление, включение, выключение.

Загрузка реестра живёт в ``app.services.source_registry`` и остаётся
read-only. Здесь — единственное место, которое реестр ИЗМЕНЯЕТ.

Хранилище остаётся data-файлом, но их два и роли у них разные:

- **seed** (``config/sources.json``) — стартовый набор, часть поставки,
  во время работы приложения не меняется;
- **runtime** (``data/sources.json``) — рабочее состояние, куда попадают
  изменения владельца. В Git не отслеживается и деплоем не переносится.

При первом обращении runtime-файл разворачивается из seed. Дальше источник
истины — только runtime: seed больше никогда не перетирает рабочие данные
молча. База данных для этого не нужна и вредна — смысл реестра в том, что
источник остаётся данными, которые видно и можно править руками.

Границы:
- ничего не скачивается и не выполняется: адрес только разбирается как строка;
- реестр не хранит токены и пароли — адрес с логином в URL отклоняется;
- запись в реестре означает РАЗРЕШЕНИЕ наблюдать источник, а не факт
  мониторинга: сбором занимается отдельный проект Travel Lead Radar.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, RLock
from typing import Any, Callable, Optional

from app.domain.sources import (
    PLATFORM_TELEGRAM,
    PURPOSE_MIXED,
    Source,
    detect_platform,
    normalize_platform,
    normalize_telegram_username,
    normalize_url,
    source_identity_key,
    telegram_url,
    url_host,
)
from app.services.source_registry import (
    SCHEMA_VERSION,
    SEED_REGISTRY_PATH,
    SourceRegistry,
    SourceRegistryError,
    clear_cache,
    parse_registry,
    runtime_registry_path,
)

log = logging.getLogger(__name__)

# Значения по умолчанию для источника, добавленного вручную. Роль и назначение
# намеренно нейтральные: содержание нового канала ещё не разобрано, и
# придумывать за него назначение нельзя.
DEFAULT_SOURCE_TYPE = "monitored_source"
DEFAULT_PURPOSE = PURPOSE_MIXED
DEFAULT_PRIORITY = 50
DEFAULT_NOTES = (
    "Добавлен вручную через Control Center. Назначение уточняется: "
    "классификация требует анализа содержимого."
)

_ID_SANITIZE_RE = re.compile(r"[^a-z0-9]+")
_MAX_ID_LENGTH = 64

# Блокировки общие для всего процесса и привязаны к файлу, а не к экземпляру
# магазина. Один объект на файл гарантировать нельзя (хендлеры, скрипты и
# тесты создают свои), а защищать нужно именно файл.
#
# Блокировка охватывает ВЕСЬ цикл read-modify-write. Одного атомарного
# os.replace недостаточно: он спасает от повреждённого файла, но не от
# потерянного обновления — два потока успели бы прочитать одно и то же
# состояние, и вторая запись отменила бы первую.
_FILE_LOCKS: dict[str, RLock] = {}
_FILE_LOCKS_GUARD = Lock()


def _lock_for(path: Path) -> RLock:
    key = str(path.resolve())
    with _FILE_LOCKS_GUARD:
        lock = _FILE_LOCKS.get(key)
        if lock is None:
            lock = RLock()
            _FILE_LOCKS[key] = lock
        return lock


class SourceAddressError(SourceRegistryError):
    """Адрес источника нельзя разобрать или он не разрешён в реестре."""


class DuplicateSourceError(SourceRegistryError):
    """Такой источник в реестре уже есть."""

    def __init__(self, message: str, *, existing: Source) -> None:
        super().__init__(message)
        self.existing = existing


class UnknownSourceError(SourceRegistryError):
    """Источника с таким id в реестре нет."""


class SourceRegistryStore:
    """Чтение и изменение рабочего файла реестра источников."""

    def __init__(
        self, path: Optional[Path] = None, *, seed_path: Optional[Path] = None
    ) -> None:
        self._path = Path(path) if path is not None else runtime_registry_path()
        self._seed_path = Path(seed_path) if seed_path is not None else SEED_REGISTRY_PATH
        self._lock = _lock_for(self._path)

    @property
    def path(self) -> Path:
        """Рабочий файл — сюда пишутся изменения."""
        return self._path

    @property
    def seed_path(self) -> Path:
        """Стартовый набор. Магазин его только читает, и только при bootstrap."""
        return self._seed_path

    # ── Bootstrap ────────────────────────────────────────────────────────────

    def ensure_bootstrapped(self) -> bool:
        """Разворачивает рабочий файл из seed, если его ещё нет.

        Возвращает True, если файл был создан именно этим вызовом.

        Существующий рабочий файл не трогается никогда: seed нужен для
        первого запуска и явного восстановления, но не для того, чтобы при
        каждом старте возвращать реестр к версии из репозитория.
        """
        with self._lock:
            if self._path.exists():
                return False

            payload = self._seed_payload()
            # Сломанный seed не должен превратиться в сломанное рабочее
            # состояние: содержимое проверяется до записи.
            parse_registry(payload)
            self._write_payload(payload)
            clear_cache()
            log.info(
                "Реестр источников развёрнут из %s в %s",
                self._seed_path,
                self._path,
            )
            return True

    def _seed_payload(self) -> dict[str, Any]:
        if not self._seed_path.exists():
            # Пустой реестр лучше отказа: без seed приложение всё равно должно
            # подниматься, просто источников пока нет.
            log.warning(
                "Стартовый реестр источников не найден: %s. "
                "Рабочий реестр создан пустым.",
                self._seed_path,
            )
            return {"schema_version": SCHEMA_VERSION, "sources": []}
        return _read_json(self._seed_path)

    # ── Чтение ───────────────────────────────────────────────────────────────

    def load(self) -> SourceRegistry:
        """Текущее содержимое рабочего реестра.

        Кеш загрузчика намеренно не используется: сразу после изменения нужен
        свежий файл, а не то, что было прочитано раньше.
        """
        return parse_registry(self._read_payload())

    def list(
        self, *, platform: Optional[str] = None, only_enabled: bool = False
    ) -> tuple[Source, ...]:
        registry = self.load()
        if platform:
            return registry.by_platform(
                normalize_platform(platform), only_enabled=only_enabled
            )
        return registry.enabled() if only_enabled else registry.all()

    def get(self, source_id: str) -> Optional[Source]:
        return self.load().get((source_id or "").strip())

    def find_by_address(self, address: str) -> Optional[Source]:
        """Источник с таким же адресом, независимо от формы записи."""
        try:
            resolved = resolve_address(address)
        except SourceAddressError:
            return None
        return self._find_by_key(self.load(), resolved.identity_key)

    # ── Изменение ────────────────────────────────────────────────────────────

    def add(
        self,
        address: str,
        *,
        name: str = "",
        platform: str = "",
        source_type: str = DEFAULT_SOURCE_TYPE,
        purpose: str = DEFAULT_PURPOSE,
        notes: str = DEFAULT_NOTES,
        enabled: bool = True,
        priority: int = DEFAULT_PRIORITY,
        source_id: str = "",
    ) -> Source:
        """Добавляет источник по адресу и возвращает его запись.

        Повторный адрес не создаёт вторую запись: поднимается
        ``DuplicateSourceError`` с уже существующим источником — в том числе
        когда тот выключен. Решение «включить его» остаётся за владельцем.
        """
        resolved = resolve_address(address, platform=platform)

        def mutate(payload: dict[str, Any]) -> str:
            registry = parse_registry(payload)
            existing = self._find_by_key(registry, resolved.identity_key)
            if existing is not None:
                state = "включён" if existing.enabled else "выключен"
                raise DuplicateSourceError(
                    f"источник с таким адресом уже есть: '{existing.id}' "
                    f"({state})",
                    existing=existing,
                )

            new_id = (source_id or "").strip() or _generate_id(
                resolved, taken={item.id for item in registry}
            )
            if registry.get(new_id) is not None:
                raise DuplicateSourceError(
                    f"источник с id '{new_id}' уже есть",
                    existing=registry.get(new_id),  # type: ignore[arg-type]
                )

            now = _now()
            record: dict[str, Any] = {
                "id": new_id,
                "name": (name or "").strip() or resolved.default_name,
                "platform": resolved.platform,
                "source_type": (source_type or DEFAULT_SOURCE_TYPE).strip(),
                "purpose": (purpose or DEFAULT_PURPOSE).strip(),
                "enabled": bool(enabled),
                "priority": int(priority),
                "notes": (notes or "").strip(),
                "created_at": now,
                "updated_at": now,
            }
            if resolved.username:
                record["username"] = resolved.username
            if resolved.url:
                record["url"] = resolved.url

            payload.setdefault("sources", []).append(record)
            return new_id

        return self._mutate(mutate)

    def set_enabled(self, source_id: str, enabled: bool) -> Source:
        """Включает или выключает источник. Повторный вызов ничего не портит."""
        key = (source_id or "").strip()

        def mutate(payload: dict[str, Any]) -> str:
            record = _find_record(payload, key)
            if bool(record.get("enabled", True)) != bool(enabled):
                record["enabled"] = bool(enabled)
                record["updated_at"] = _now()
            return key

        return self._mutate(mutate)

    def enable(self, source_id: str) -> Source:
        return self.set_enabled(source_id, True)

    def disable(self, source_id: str) -> Source:
        return self.set_enabled(source_id, False)

    def toggle(self, source_id: str) -> Source:
        """Меняет состояние источника на противоположное.

        Отдельная операция, а не «прочитать и записать обратное» на стороне
        вызывающего: между чтением и записью состояние могло измениться, и
        два одновременных нажатия отменили бы друг друга.
        """
        key = (source_id or "").strip()

        def mutate(payload: dict[str, Any]) -> str:
            record = _find_record(payload, key)
            record["enabled"] = not bool(record.get("enabled", True))
            record["updated_at"] = _now()
            return key

        return self._mutate(mutate)

    def remove(self, source_id: str) -> Source:
        """Физически удаляет запись и возвращает удалённое.

        Обычный сценарий — ``disable()``: выключенный источник сохраняет
        историю решения «сюда мы уже смотрели», а удалённый её теряет.
        """
        key = (source_id or "").strip()
        removed: list[Source] = []

        def mutate(payload: dict[str, Any]) -> None:
            # Чтение того, что удаляем, идёт внутри той же блокировки —
            # иначе вернулось бы состояние, которого уже нет.
            _find_record(payload, key)
            found = parse_registry(payload).get(key)
            if found is not None:
                removed.append(found)
            payload["sources"] = [
                record
                for record in payload.get("sources", [])
                if str(record.get("id") or "").strip() != key
            ]

        self._mutate(mutate, expect_source=False)
        if not removed:
            raise UnknownSourceError(f"источник '{key}' не найден в реестре")
        return removed[0]

    # ── Внутреннее ───────────────────────────────────────────────────────────

    @staticmethod
    def _find_by_key(registry: SourceRegistry, key: str) -> Optional[Source]:
        if not key:
            return None
        for source in registry:
            if source.identity_key == key:
                return source
        return None

    def _mutate(
        self,
        change: Callable[[dict[str, Any]], Any],
        *,
        expect_source: bool = True,
    ) -> Any:
        # Блокировка держится на всём цикле «прочитали → изменили → записали»,
        # иначе два одновременных изменения потеряли бы одно из них.
        with self._lock:
            self.ensure_bootstrapped()
            payload = self._read_payload()
            source_id = change(payload)
            # Файл проверяется ДО записи: сломанный реестр на диск не попадает.
            registry = parse_registry(payload)
            self._write_payload(payload)
            clear_cache()

        if not expect_source:
            return None
        result = registry.get(str(source_id))
        if result is None:
            raise SourceRegistryError(
                f"источник '{source_id}' не найден после изменения реестра"
            )
        return result

    def _read_payload(self) -> dict[str, Any]:
        """Сырой JSON активного реестра.

        Пока рабочего файла нет, читается seed — чтение не должно создавать
        состояние побочным эффектом. Набор источников при этом один и тот же
        и до, и после bootstrap: разойтись показанному в UI и тому, что
        увидит сбор, здесь не на чем.

        Работа идёт именно с сырым payload, а не с разобранными `Source`:
        так сохраняются комментарии-поля верхнего уровня и любые ключи,
        которых загрузчик пока не знает.
        """
        if self._path.exists():
            return _read_json(self._path)
        return self._seed_payload()

    def _write_payload(self, payload: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        temporary = self._path.with_name(self._path.name + ".tmp")
        try:
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, self._path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise SourceRegistryError(
                f"не удалось записать реестр источников: {self._path}"
            ) from exc


# ── Разбор адреса ────────────────────────────────────────────────────────────


class ResolvedAddress:
    """Разобранный адрес источника — без единого обращения к сети."""

    __slots__ = ("platform", "url", "username")

    def __init__(self, *, platform: str, url: str, username: str) -> None:
        self.platform = platform
        self.url = url
        self.username = username

    @property
    def identity_key(self) -> str:
        return source_identity_key(
            platform=self.platform, url=self.url, username=self.username
        )

    @property
    def default_name(self) -> str:
        if self.username:
            return f"@{self.username}"
        return url_host(self.url) or self.url


def resolve_address(raw: str, *, platform: str = "") -> ResolvedAddress:
    """Проверяет адрес и приводит его к канонической форме.

    Telegram принимается в любом виде (`@name`, `t.me/name`,
    `https://t.me/name/42`) и всегда сохраняется как username плюс
    канонический публичный URL. Остальные адреса требуют явной схемы
    http/https: угадывать схему за владельца — значит записать в реестр не то,
    что он дал.
    """
    value = (raw or "").strip()
    if not value:
        raise SourceAddressError("адрес источника пустой")

    resolved_platform = normalize_platform(platform) or detect_platform(value)
    if not resolved_platform:
        raise SourceAddressError(
            "адрес должен начинаться с http:// или https:// "
            "(для Telegram подойдёт также @имя_канала)"
        )

    if resolved_platform == PLATFORM_TELEGRAM:
        username = normalize_telegram_username(value)
        if not username:
            raise SourceAddressError(
                "не удалось определить публичный Telegram-канал. "
                "Приватные приглашения и закрытые каналы в реестр не добавляются"
            )
        return ResolvedAddress(
            platform=PLATFORM_TELEGRAM, url=telegram_url(username), username=username
        )

    normalized = normalize_url(value)
    if not normalized:
        raise SourceAddressError(
            "адрес должен быть корректной ссылкой http:// или https:// "
            "без логина и пароля в URL"
        )
    return ResolvedAddress(platform=resolved_platform, url=normalized, username="")


def _generate_id(resolved: ResolvedAddress, *, taken: set[str]) -> str:
    """Технический id из платформы и адреса. Конкретные каналы тут не зашиты."""
    if resolved.username:
        tail = resolved.username
    else:
        host = url_host(resolved.url)
        path = normalize_url(resolved.url).split(host, 1)[-1] if host else ""
        tail = f"{host}{path}"

    slug = _ID_SANITIZE_RE.sub("_", tail.lower()).strip("_")
    base = f"{resolved.platform}_{slug}".strip("_")[:_MAX_ID_LENGTH] or "source"

    candidate = base
    counter = 2
    while candidate in taken:
        suffix = f"_{counter}"
        candidate = base[: _MAX_ID_LENGTH - len(suffix)] + suffix
        counter += 1
    return candidate


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SourceRegistryError(
            f"не удалось прочитать реестр источников: {path}"
        ) from exc
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise SourceRegistryError(
            f"некорректный JSON в реестре источников: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise SourceRegistryError("ожидался объект с ключом 'sources'")
    return payload


def _find_record(payload: dict[str, Any], source_id: str) -> dict[str, Any]:
    for record in payload.get("sources", []):
        if isinstance(record, dict) and str(record.get("id") or "").strip() == source_id:
            return record
    raise UnknownSourceError(f"источник '{source_id}' не найден в реестре")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
