from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from app.domain.knowledge import VERIFICATION_STATUSES
from app.repositories.knowledge_repository import KnowledgeRepository


DATASET_VERSION = 1


class KnowledgeDatasetError(ValueError):
    pass


def load_and_validate_dataset(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KnowledgeDatasetError(f"Cannot read knowledge dataset: {exc}") from exc
    validate_dataset(value)
    _validate_local_source_hashes(value)
    return value


def validate_dataset(dataset: Any) -> None:
    if not isinstance(dataset, dict):
        raise KnowledgeDatasetError("Dataset root must be an object")
    if dataset.get("dataset_version") != DATASET_VERSION:
        raise KnowledgeDatasetError(f"dataset_version must be {DATASET_VERSION}")
    _only_keys(dataset, {"dataset_version", "sources", "items", "facts", "examples"}, "dataset")
    for collection in ("sources", "items", "facts", "examples"):
        if not isinstance(dataset.get(collection), list):
            raise KnowledgeDatasetError(f"{collection} must be a list")

    sources = _indexed(dataset["sources"], "sources")
    items = _indexed(dataset["items"], "items")
    facts = _indexed(dataset["facts"], "facts")
    _indexed(dataset["examples"], "examples")

    source_fields = {"stable_key", "title", "source_type", "source_name", "source_reference",
                     "version", "effective_date", "verification_status", "content_hash"}
    for source in sources.values():
        _only_keys(source, source_fields, f"source {source['stable_key']}")
        _required_text(source, source_fields - {"version", "effective_date"})
        if source["verification_status"] not in VERIFICATION_STATUSES:
            raise KnowledgeDatasetError("Unsupported verification_status")
        if len(source["content_hash"]) != 64:
            raise KnowledgeDatasetError("content_hash must be a SHA-256 hex digest")
        try:
            bytes.fromhex(source["content_hash"])
        except ValueError as exc:
            raise KnowledgeDatasetError("content_hash must be hexadecimal") from exc

    item_fields = {"stable_key", "category", "title", "content", "source_key", "source_ref",
                   "status", "sort_order", "tags", "related"}
    for item in items.values():
        _only_keys(item, item_fields, f"item {item['stable_key']}")
        _required_text(item, {"stable_key", "category", "title", "content", "source_key", "source_ref"})
        _references(item["source_key"], sources, "source_key")
        if not isinstance(item.get("tags", []), list) or not all(_nonempty(x) for x in item.get("tags", [])):
            raise KnowledgeDatasetError("item tags must be non-empty strings")
        for relation in item.get("related", []):
            if set(relation) != {"relation_type", "item_key"}:
                raise KnowledgeDatasetError("Invalid item relation")
            _references(relation["item_key"], items, "related item_key")

    fact_fields = {"stable_key", "item_key", "source_key", "fact_type", "subject_type",
                   "subject_key", "qualifier_key", "qualifier_value", "value_number", "value_text",
                   "range_min", "range_max", "unit", "currency", "period", "condition_text",
                   "source_ref", "sort_order"}
    canonical: set[tuple[Any, ...]] = set()
    for fact in facts.values():
        _only_keys(fact, fact_fields, f"fact {fact['stable_key']}")
        _required_text(fact, {"stable_key", "item_key", "source_key", "fact_type",
                              "subject_type", "subject_key", "source_ref"})
        _references(fact["item_key"], items, "item_key")
        _references(fact["source_key"], sources, "source_key")
        choices = [fact.get("value_number") is not None, fact.get("value_text") is not None,
                   fact.get("range_min") is not None or fact.get("range_max") is not None]
        if sum(choices) != 1:
            raise KnowledgeDatasetError("Fact requires exactly one number, text, or complete range")
        if choices[2] and (fact.get("range_min") is None or fact.get("range_max") is None):
            raise KnowledgeDatasetError("Fact range requires both range_min and range_max")
        for key in ("value_number", "range_min", "range_max"):
            if fact.get(key) is not None:
                _as_decimal(fact[key], key)
        identity = (fact["fact_type"], fact["subject_key"], fact.get("qualifier_key"),
                    fact.get("qualifier_value"), fact.get("unit"), fact.get("currency"), fact.get("period"))
        if identity in canonical:
            raise KnowledgeDatasetError(f"Duplicate canonical fact identity: {identity}")
        canonical.add(identity)

    example_fields = {"stable_key", "item_key", "title", "scenario", "explanation",
                      "result_number", "currency", "source_key", "source_ref", "fact_refs"}
    for example in dataset["examples"]:
        _only_keys(example, example_fields, f"example {example['stable_key']}")
        _required_text(example, {"stable_key", "item_key", "title", "scenario", "explanation",
                                 "source_key", "source_ref"})
        _references(example["item_key"], items, "item_key")
        _references(example["source_key"], sources, "source_key")
        if example.get("result_number") is not None:
            _as_decimal(example["result_number"], "result_number")
        refs = example.get("fact_refs")
        if not isinstance(refs, list) or not refs:
            raise KnowledgeDatasetError("Every example must reference canonical facts")
        for ref in refs:
            if not isinstance(ref, dict) or set(ref) - {"fact_key", "role"} or "fact_key" not in ref:
                raise KnowledgeDatasetError("Invalid example fact_ref")
            _references(ref["fact_key"], facts, "fact_key")


async def import_dataset(path: Path, repository: KnowledgeRepository) -> None:
    dataset = load_and_validate_dataset(path)
    await repository.init()
    await repository.import_dataset(dataset)


def source_content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _validate_local_source_hashes(dataset: Mapping[str, Any]) -> None:
    for source in dataset["sources"]:
        reference = Path(source["source_reference"])
        if reference.is_file():
            actual = source_content_hash(reference.read_bytes())
            if actual != source["content_hash"].lower():
                raise KnowledgeDatasetError(
                    f"content_hash mismatch for local source: {reference}"
                )


def _indexed(rows: list[Any], name: str) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not _nonempty(row.get("stable_key")):
            raise KnowledgeDatasetError(f"Every {name} row needs stable_key")
        if row["stable_key"] in result:
            raise KnowledgeDatasetError(f"Duplicate stable_key: {row['stable_key']}")
        result[row["stable_key"]] = row
    return result


def _only_keys(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise KnowledgeDatasetError(f"Unknown fields in {label}: {sorted(unknown)}")


def _required_text(value: Mapping[str, Any], fields: set[str]) -> None:
    for field in fields:
        if not _nonempty(value.get(field)):
            raise KnowledgeDatasetError(f"{field} must be a non-empty string")


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _references(key: str, mapping: Mapping[str, Any], label: str) -> None:
    if key not in mapping:
        raise KnowledgeDatasetError(f"Unknown {label}: {key}")


def _as_decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise KnowledgeDatasetError(f"{label} must be numeric")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise KnowledgeDatasetError(f"{label} must be numeric") from exc
    if not result.is_finite():
        raise KnowledgeDatasetError(f"{label} must be finite")
    return result
