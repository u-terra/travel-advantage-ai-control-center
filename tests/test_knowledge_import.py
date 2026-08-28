from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app.repositories.knowledge_repository import KnowledgeRepository
from app.services.knowledge_import import KnowledgeDatasetError, import_dataset, validate_dataset


DATASET = Path("knowledge/travel_advantage/imports/compensation-foundation.v1.json")


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def payload() -> dict[str, Any]:
    return json.loads(DATASET.read_text(encoding="utf-8"))


def test_validation_rejects_duplicate_canonical_fact() -> None:
    value = payload()
    duplicate = copy.deepcopy(value["facts"][0])
    duplicate["stable_key"] = "different.key.same.meaning"
    value["facts"].append(duplicate)
    with pytest.raises(KnowledgeDatasetError, match="Duplicate canonical fact"):
        validate_dataset(value)


def test_validation_rejects_example_without_fact_refs() -> None:
    value = payload()
    value["examples"][0]["fact_refs"] = []
    with pytest.raises(KnowledgeDatasetError, match="reference canonical facts"):
        validate_dataset(value)


def test_validation_happens_before_database_write(tmp_path: Path) -> None:
    value = payload()
    value["facts"][0]["source_ref"] = " "
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    db_path = tmp_path / "knowledge.sqlite3"

    with pytest.raises(KnowledgeDatasetError):
        run(import_dataset(invalid, KnowledgeRepository(db_path)))
    assert not db_path.exists()


def test_import_transaction_rolls_back_on_write_failure(tmp_path: Path) -> None:
    repository = KnowledgeRepository(tmp_path / "knowledge.sqlite3")
    run(import_dataset(DATASET, repository))
    value = payload()
    value["sources"][0]["title"] = "Title that must roll back"
    # Bypass public validation to exercise the repository's transaction itself.
    value["items"][0]["source_key"] = "missing.source"

    with pytest.raises(KeyError):
        run(repository.import_dataset(value))
    with sqlite3.connect(repository.db_path) as db:
        title = db.execute("SELECT title FROM knowledge_sources").fetchone()[0]
    assert title != "Title that must roll back"
