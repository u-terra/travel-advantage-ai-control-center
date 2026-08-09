"""Общие фикстуры тестов."""

from __future__ import annotations

import pytest

from app.services.source_registry import (
    RUNTIME_REGISTRY_PATH_ENV,
    clear_cache,
)


@pytest.fixture(autouse=True)
def isolated_runtime_registry(tmp_path_factory, monkeypatch):
    """Уводит рабочий реестр источников во временный каталог.

    Две причины:

    1. тест не должен создавать `data/sources.json` в рабочей копии —
       иначе один прогон менял бы поведение следующего;
    2. проверки стартового набора должны читать именно `config/sources.json`,
       независимо от того, запускал ли разработчик бота локально. Файл по
       временному пути не создаётся, поэтому активным реестром остаётся seed.
    """
    runtime = tmp_path_factory.mktemp("registry") / "sources.json"
    monkeypatch.setenv(RUNTIME_REGISTRY_PATH_ENV, str(runtime))
    clear_cache()
    yield
    clear_cache()
