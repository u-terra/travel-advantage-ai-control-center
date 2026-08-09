"""Разделение стартового набора и рабочего состояния реестра источников.

Главное требование: следующий деплой не должен стирать источники, которые
владелец добавил через бота.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from app.config import load_settings
from app.services.source_registry import (
    DEFAULT_RUNTIME_REGISTRY_PATH,
    RUNTIME_REGISTRY_PATH_ENV,
    SEED_REGISTRY_PATH,
    active_registry_path,
    load_registry,
    runtime_registry_path,
)
from app.services.source_registry_store import SourceRegistryStore


def _seed(path: Path, *ids: str) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "description": "Стартовый набор",
                "sources": [
                    {
                        "id": source_id,
                        "name": source_id,
                        "platform": "web",
                        "url": f"https://example.com/{source_id}",
                        "source_type": "monitored_source",
                        "purpose": "mixed",
                    }
                    for source_id in ids
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def paths(tmp_path: Path) -> tuple[Path, Path]:
    return _seed(tmp_path / "seed.json", "seed_one"), tmp_path / "runtime.json"


def _store(paths: tuple[Path, Path]) -> SourceRegistryStore:
    seed, runtime = paths
    return SourceRegistryStore(runtime, seed_path=seed)


# ── Bootstrap ────────────────────────────────────────────────────────────────


def test_missing_runtime_file_is_created_from_seed(paths) -> None:
    seed, runtime = paths
    assert not runtime.exists()

    assert _store(paths).ensure_bootstrapped() is True

    assert runtime.exists()
    assert [source.id for source in _store(paths).list()] == ["seed_one"]


def test_bootstrap_does_not_touch_the_seed(paths) -> None:
    seed, _ = paths
    before = seed.read_text(encoding="utf-8")

    store = _store(paths)
    store.ensure_bootstrapped()
    store.add("https://example.com/new")
    store.disable("seed_one")

    assert seed.read_text(encoding="utf-8") == before


def test_existing_runtime_file_is_never_overwritten(paths) -> None:
    store = _store(paths)
    store.ensure_bootstrapped()
    store.disable("seed_one")
    added = store.add("https://example.com/added")

    assert _store(paths).ensure_bootstrapped() is False

    reopened = _store(paths)
    assert reopened.get(added.id) is not None
    assert reopened.get("seed_one").enabled is False


def test_repeated_bootstrap_is_idempotent(paths) -> None:
    store = _store(paths)

    assert store.ensure_bootstrapped() is True
    assert store.ensure_bootstrapped() is False
    assert store.ensure_bootstrapped() is False
    assert len(store.list()) == 1


def test_a_broken_seed_does_not_become_broken_runtime_state(tmp_path: Path) -> None:
    seed = tmp_path / "seed.json"
    seed.write_text('{"schema_version": 2, "sources": "не список"}', encoding="utf-8")
    runtime = tmp_path / "runtime.json"

    with pytest.raises(Exception):
        SourceRegistryStore(runtime, seed_path=seed).ensure_bootstrapped()

    assert not runtime.exists()


def test_a_missing_seed_still_gives_a_working_registry(tmp_path: Path) -> None:
    store = SourceRegistryStore(
        tmp_path / "runtime.json", seed_path=tmp_path / "absent.json"
    )

    assert store.ensure_bootstrapped() is True
    assert store.list() == ()
    assert store.add("https://example.com/a") is not None


# ── Deploy: обновление кода не теряет источники ──────────────────────────────


def test_a_new_seed_version_does_not_overwrite_runtime_data(paths) -> None:
    """Имитация деплоя: seed в репозитории обновился, рабочий файл — нет."""
    seed, runtime = paths
    store = _store(paths)
    store.ensure_bootstrapped()
    owner_source = store.add("https://example.com/added-by-owner")
    store.disable("seed_one")

    # Деплой привёз новую версию стартового набора.
    _seed(seed, "seed_one", "seed_two_from_deploy")

    after_deploy = _store(paths)
    after_deploy.ensure_bootstrapped()

    ids = {source.id for source in after_deploy.list()}
    assert owner_source.id in ids, "источник владельца потерян при обновлении seed"
    assert "seed_two_from_deploy" not in ids
    assert after_deploy.get("seed_one").enabled is False


def test_runtime_state_survives_a_fresh_store_instance(paths) -> None:
    added = _store(paths).add("https://example.com/added")

    assert _store(paths).get(added.id) is not None


# ── Единый источник истины ───────────────────────────────────────────────────


def test_seed_is_active_until_bootstrap(tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "runtime.json"
    monkeypatch.setenv(RUNTIME_REGISTRY_PATH_ENV, str(runtime))

    assert active_registry_path() == SEED_REGISTRY_PATH


def test_runtime_becomes_active_after_bootstrap(tmp_path: Path, monkeypatch) -> None:
    seed = _seed(tmp_path / "seed.json", "seed_one")
    runtime = tmp_path / "runtime.json"
    monkeypatch.setenv(RUNTIME_REGISTRY_PATH_ENV, str(runtime))

    SourceRegistryStore(seed_path=seed).ensure_bootstrapped()

    assert active_registry_path() == runtime


def test_store_and_plain_loader_see_the_same_sources(
    tmp_path: Path, monkeypatch
) -> None:
    # То, что показывает UI, и то, что прочитает сбор, — один набор.
    seed = _seed(tmp_path / "seed.json", "seed_one")
    runtime = tmp_path / "runtime.json"
    monkeypatch.setenv(RUNTIME_REGISTRY_PATH_ENV, str(runtime))
    store = SourceRegistryStore(seed_path=seed)
    added = store.add("https://example.com/added")

    from_loader = load_registry(use_cache=False)

    assert {source.id for source in store.list()} == {
        source.id for source in from_loader
    }
    assert from_loader.get(added.id) is not None


def test_reading_never_creates_runtime_state(tmp_path: Path, monkeypatch) -> None:
    # Просмотр списка не должен разворачивать рабочий файл побочным эффектом.
    seed = _seed(tmp_path / "seed.json", "seed_one")
    runtime = tmp_path / "runtime.json"
    monkeypatch.setenv(RUNTIME_REGISTRY_PATH_ENV, str(runtime))

    store = SourceRegistryStore(seed_path=seed)
    assert [source.id for source in store.list()] == ["seed_one"]
    assert store.get("seed_one") is not None
    load_registry(use_cache=False)

    assert not runtime.exists()


def test_first_write_bootstraps_by_itself(paths) -> None:
    seed, runtime = paths
    store = _store(paths)

    store.add("https://example.com/added")

    assert runtime.exists()
    assert {source.id for source in store.list()} == {"seed_one", "web_example_com_added"}


# ── Пути ─────────────────────────────────────────────────────────────────────


def test_seed_and_runtime_are_different_files() -> None:
    assert SEED_REGISTRY_PATH != DEFAULT_RUNTIME_REGISTRY_PATH
    assert SEED_REGISTRY_PATH.name == "sources.json"
    assert SEED_REGISTRY_PATH.parent.name == "config"
    assert DEFAULT_RUNTIME_REGISTRY_PATH.parent.name == "data"


def test_runtime_path_is_injectable(tmp_path: Path, monkeypatch) -> None:
    custom = tmp_path / "elsewhere" / "sources.json"
    monkeypatch.setenv(RUNTIME_REGISTRY_PATH_ENV, str(custom))

    assert runtime_registry_path() == custom
    assert SourceRegistryStore().path == custom


def test_settings_expose_the_runtime_path(tmp_path: Path, monkeypatch) -> None:
    custom = tmp_path / "settings" / "sources.json"
    monkeypatch.setenv(RUNTIME_REGISTRY_PATH_ENV, str(custom))
    monkeypatch.setenv("BOT_TOKEN", "dummy-token")
    monkeypatch.setenv("ADMIN_TELEGRAM_ID", "1")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "1")

    assert load_settings().sources_registry_path == custom


def test_store_creates_missing_parent_directories(tmp_path: Path) -> None:
    # На чистом сервере каталога data/ может не быть.
    seed = _seed(tmp_path / "seed.json", "seed_one")
    runtime = tmp_path / "absent-dir" / "nested" / "sources.json"

    SourceRegistryStore(runtime, seed_path=seed).ensure_bootstrapped()

    assert runtime.exists()


def test_runtime_registry_is_ignored_by_git() -> None:
    import subprocess

    project_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["git", "check-ignore", str(DEFAULT_RUNTIME_REGISTRY_PATH)],
        cwd=project_root,
        capture_output=True,
    )

    assert result.returncode == 0, (
        "рабочий реестр источников должен игнорироваться Git, "
        "иначе состояние попадёт в поставку"
    )


# ── Потеря обновлений ────────────────────────────────────────────────────────


def test_concurrent_adds_do_not_lose_each_other(paths) -> None:
    """Каждое из параллельных добавлений должно остаться в файле."""
    store = _store(paths)
    store.ensure_bootstrapped()
    barrier = threading.Barrier(8)
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            barrier.wait()
            store.add(f"https://example.com/parallel-{index}")
        except BaseException as exc:  # noqa: BLE001 - переносим в основной поток
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(store.list()) == 9  # seed_one + 8 добавленных


def test_concurrent_writes_through_separate_store_objects_are_safe(paths) -> None:
    # Блокировка привязана к файлу, а не к экземпляру: хендлеры, скрипты и
    # тесты создают свои объекты магазина.
    seed, runtime = paths
    SourceRegistryStore(runtime, seed_path=seed).ensure_bootstrapped()
    barrier = threading.Barrier(6)
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            store = SourceRegistryStore(runtime, seed_path=seed)
            barrier.wait()
            store.add(f"https://example.com/separate-{index}")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(SourceRegistryStore(runtime, seed_path=seed).list()) == 7


def test_concurrent_toggles_do_not_cancel_each_other(paths) -> None:
    """Нечётное число переключений обязано изменить состояние.

    Именно здесь ловится потерянное обновление: если бы каждый поток читал
    состояние сам и записывал обратное, часть переключений исчезла бы.
    """
    store = _store(paths)
    store.ensure_bootstrapped()
    assert store.get("seed_one").enabled is True

    barrier = threading.Barrier(7)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait()
            store.toggle("seed_one")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(7)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert store.get("seed_one").enabled is False


def test_a_failed_change_leaves_the_previous_state(paths) -> None:
    store = _store(paths)
    added = store.add("https://example.com/added")

    with pytest.raises(Exception):
        store.add("https://example.com/added")  # дубликат

    assert len(store.list()) == 2
    assert store.get(added.id) is not None
