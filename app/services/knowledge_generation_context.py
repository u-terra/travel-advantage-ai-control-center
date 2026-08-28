"""Bounded, structured projection of a retrieved bundle for generation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from app.domain.knowledge import KnowledgeFact
from app.services.knowledge_service import KnowledgeBundle

_MAX_ITEMS = 3
_MAX_FACTS = 12
_MAX_ITEM_CONTENT_CHARS = 180
_MAX_VERIFIED_CLAIMS = 10
_COMPENSATION_FACT_TYPES = frozenset({
    "builder_bonus_rank_amount", "differential_builder_rule",
    "member_bonus", "compensation_component_count", "monthly_income_amount",
})
_COMPENSATION_CONSTRAINT = (
    "Официальные размеры, формулы и примеры Compensation Plan можно объяснять "
    "только как механику плана. Не превращай их в обещание, прогноз или "
    "гарантию фактического дохода конкретного человека и не указывай срок, "
    "за который пользователь якобы обязательно достигнет такого результата."
)


@dataclass(frozen=True)
class KnowledgeGenerationContext:
    source_facts: Mapping[str, Any]
    verified_claims: tuple[Mapping[str, Any], ...]
    constraints: tuple[str, ...]


def build_knowledge_generation_context(bundle: KnowledgeBundle) -> KnowledgeGenerationContext:
    """Project only data already present in the bounded retrieval result."""
    items = bundle.primary_items[:_MAX_ITEMS]
    facts = _priority_facts(bundle.facts, limit=_MAX_FACTS)
    compliance_keys = {fact.stable_key for fact in bundle.compliance_facts}
    official_facts = tuple(fact for fact in facts if fact.stable_key not in compliance_keys)
    source_facts = {"official_knowledge": {
        "items": tuple({
            "stable_key": item.stable_key, "title": item.title,
            "category": item.category,
            "content": item.content[:_MAX_ITEM_CONTENT_CHARS],
            "source_ref": item.source_ref,
        } for item in items),
        "canonical_facts": tuple(_fact_data(fact) for fact in official_facts),
        "provenance": tuple({
            "stable_key": source.stable_key,
            "source_reference": source.source_reference,
            "verification_status": source.verification_status,
        } for source in bundle.sources[:_MAX_ITEMS]),
    }}
    claim_facts = _verified_claim_facts(official_facts)
    verified_claims = tuple({
        "text": _fact_statement(fact),
        "verification_status": "verified",
        "evidence_reference": fact.source_ref,
    } for fact in claim_facts)
    constraints = tuple(
        "Официальное compliance-правило "
        f"[{fact.stable_key}; источник: {fact.source_ref}]: {_fact_statement(fact)}"
        for fact in bundle.compliance_facts
    )
    if any(fact.fact_type in _COMPENSATION_FACT_TYPES for fact in facts):
        constraints = (*constraints, _COMPENSATION_CONSTRAINT)
    return KnowledgeGenerationContext(source_facts, verified_claims, constraints)


def _fact_data(fact: KnowledgeFact) -> dict[str, Any]:
    values = {
        "stable_key": fact.stable_key, "fact_type": fact.fact_type,
        "subject_key": fact.subject_key,
        "qualifier_key": fact.qualifier_key, "qualifier_value": fact.qualifier_value,
        "value_number": _decimal(fact.value_number), "value_text": fact.value_text,
        "range_min": _decimal(fact.range_min), "range_max": _decimal(fact.range_max),
        "unit": fact.unit, "currency": fact.currency, "period": fact.period,
        "condition": fact.condition_text, "source_ref": fact.source_ref,
    }
    return {key: value for key, value in values.items() if value is not None}


def _verified_claim_facts(
    facts: tuple[KnowledgeFact, ...],
) -> tuple[KnowledgeFact, ...]:
    return _priority_facts(facts, limit=_MAX_VERIFIED_CLAIMS)


def _priority_facts(
    facts: tuple[KnowledgeFact, ...], *, limit: int,
) -> tuple[KnowledgeFact, ...]:
    priority_markers = (
        "conversion", "transfer", "member_bonus", "builder_bonus",
        "compensation", "guarantee",
    )
    prioritized = (
        *facts[:5],
        *(fact for fact in facts if any(marker in fact.stable_key for marker in priority_markers)),
    )
    result: list[KnowledgeFact] = []
    seen: set[str] = set()
    for fact in prioritized:
        if fact.stable_key in seen:
            continue
        seen.add(fact.stable_key)
        result.append(fact)
        if len(result) == limit:
            break
    return tuple(result)


def _fact_statement(fact: KnowledgeFact) -> str:
    if fact.value_text is not None:
        value = fact.value_text
    elif fact.value_number is not None:
        value = f"{_decimal(fact.value_number)}{_unit_suffix(fact)}"
    else:
        value = f"{_decimal(fact.range_min)}–{_decimal(fact.range_max)}{_unit_suffix(fact)}"
    qualifier = (
        f"; {fact.qualifier_key}={fact.qualifier_value}"
        if fact.qualifier_key and fact.qualifier_value else ""
    )
    condition = f"; условие: {fact.condition_text}" if fact.condition_text else ""
    return f"{fact.subject_key}: {value}{qualifier}{condition}"


def _unit_suffix(fact: KnowledgeFact) -> str:
    values = tuple(value for value in (fact.currency, fact.unit, fact.period) if value)
    return f" {' / '.join(values)}" if values else ""


def _decimal(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None
