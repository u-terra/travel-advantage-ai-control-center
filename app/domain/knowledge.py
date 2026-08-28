from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


VERIFICATION_STATUSES = ("verified_official", "superseded", "withdrawn")


@dataclass(frozen=True)
class KnowledgeSource:
    id: int
    stable_key: str
    title: str
    source_type: str
    source_name: str
    source_reference: str
    version: str | None
    effective_date: str | None
    verification_status: str
    content_hash: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class KnowledgeItem:
    id: int
    stable_key: str
    category: str
    title: str
    content: str
    source_id: int
    source_ref: str
    status: str
    sort_order: int
    tags: tuple[str, ...]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class KnowledgeFact:
    id: int
    stable_key: str
    item_id: int
    source_id: int
    fact_type: str
    subject_type: str
    subject_key: str
    qualifier_key: str | None
    qualifier_value: str | None
    value_number: Decimal | None
    value_text: str | None
    range_min: Decimal | None
    range_max: Decimal | None
    unit: str | None
    currency: str | None
    period: str | None
    condition_text: str | None
    source_ref: str
    sort_order: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class KnowledgeExample:
    id: int
    stable_key: str
    item_id: int
    title: str
    scenario: str
    explanation: str
    result_number: Decimal | None
    currency: str | None
    source_id: int
    source_ref: str
    fact_keys: tuple[str, ...]
    created_at: str
    updated_at: str
