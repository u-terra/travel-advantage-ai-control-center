from __future__ import annotations

import asyncio
import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.repositories.knowledge_repository import KnowledgeRepository
from app.services.knowledge_import import import_dataset


DATASET = Path("knowledge/travel_advantage/imports/compensation-foundation.v1.json")


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_repository_schema_and_retrieval(tmp_path: Path) -> None:
    repository = KnowledgeRepository(tmp_path / "knowledge.sqlite3")
    run(import_dataset(DATASET, repository))

    source = run(repository.get_source("ta.compensation.official_extract.v1"))
    item = run(repository.get_item("ta.member_bonus"))
    facts = run(repository.get_facts(item_key="ta.member_bonus"))
    example = run(repository.get_example("ta.example.silver.personal_elite"))

    assert source is not None and source.verification_status == "verified_official"
    assert source.source_reference.endswith("official-compensation-extract.md")
    assert item is not None and item.tags == ("elite", "elite turbo", "member bonus")
    assert [fact.value_number for fact in facts] == [Decimal("40"), Decimal("80")]
    assert facts[0].source_ref == "Member Bonus — базовые значения / Elite"
    assert example is not None and example.result_number == Decimal("50")
    assert example.fact_keys == (
        "ta.member_bonus.elite.usd",
        "ta.builder_bonus.silver.personal_elite.usd",
    )


def test_list_items_returns_all_active_items_across_categories(tmp_path: Path) -> None:
    repository = KnowledgeRepository(tmp_path / "knowledge.sqlite3")
    run(import_dataset(DATASET, repository))

    items = run(repository.list_items())

    assert len(items) == 2
    assert {item.stable_key for item in items} == {
        "ta.member_bonus", "ta.builder_bonus",
    }
    # каждый item приходит с тем же тегами/содержимым, что и get_item()
    direct = run(repository.get_item("ta.member_bonus"))
    listed = next(item for item in items if item.stable_key == "ta.member_bonus")
    assert listed == direct


def test_list_items_respects_limit(tmp_path: Path) -> None:
    repository = KnowledgeRepository(tmp_path / "knowledge.sqlite3")
    run(import_dataset(DATASET, repository))

    items = run(repository.list_items(limit=1))

    assert len(items) == 1


def test_repository_import_is_idempotent(tmp_path: Path) -> None:
    repository = KnowledgeRepository(tmp_path / "knowledge.sqlite3")
    run(import_dataset(DATASET, repository))
    run(import_dataset(DATASET, repository))

    with sqlite3.connect(repository.db_path) as db:
        counts = {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("knowledge_sources", "knowledge_items", "knowledge_facts",
                          "knowledge_examples", "knowledge_example_facts")
        }
    assert counts == {
        "knowledge_sources": 1,
        "knowledge_items": 2,
        "knowledge_facts": 3,
        "knowledge_examples": 1,
        "knowledge_example_facts": 2,
    }


def test_upsert_updates_canonical_fact_without_duplicate(tmp_path: Path) -> None:
    repository = KnowledgeRepository(tmp_path / "knowledge.sqlite3")
    run(import_dataset(DATASET, repository))
    payload = json.loads(DATASET.read_text(encoding="utf-8"))
    payload["facts"][0]["condition_text"] = "Уточнённое условие"
    custom = tmp_path / "updated.json"
    custom.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    run(import_dataset(custom, repository))

    facts = run(repository.get_facts(subject_key="membership.elite"))
    assert len(facts) == 1
    assert facts[0].condition_text == "Уточнённое условие"
