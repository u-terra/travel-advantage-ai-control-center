"""Bounded, structured projection of a retrieved bundle for generation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from app.domain.knowledge import KnowledgeFact
from app.services.knowledge_service import KnowledgeBundle

_MAX_ITEMS = 4
_MAX_FACTS = 3
_MAX_ITEM_CONTENT_CHARS = 48
_MAX_SOURCE_REF_CHARS = 16
_MAX_PROVENANCE = 1
_MAX_VERIFIED_CLAIMS = 1
# Live prod bug: for a broad "why keep membership / what do I get" question,
# _retrieval_policy() (app.services.knowledge_service) now returns ta.platform
# and ta.membership.cancellation as REQUIRED primary items ahead of the
# question's actual current benefit items (Travel Credits/Loyalty Points/
# Life Experiences) - see membership_value_intent there. A plain [:N] slice
# here then kept the two generic overview items and dropped every benefit
# item. Worse: this whole [SOURCE FACTS - DATA] section is dropped WHOLE by
# app.services.generation_request_builder.build_client_reply_provider_request
# (the actual persona this question resolves to) when it does not fit the
# remaining prompt budget, which a naive larger projection only made more
# likely - so the real fix is choosing the RIGHT few items/facts within a
# small byte budget, not simply including more. ta.platform's own defining
# facts already reach the model separately via verified_claims (see
# _verified_claim_facts' core_definition_fact_types), so demoting its item
# card here costs nothing.
_GENERIC_OVERVIEW_CATEGORIES = frozenset({"travel_platform", "cancellation_refund"})
_LOW_PRIORITY_FACT_TYPES = frozenset({
    "membership_plan_superseded_notice", "platform_service_category",
    "verification_pending_note", "membership_total_due_today_displayed",
    "membership_activation_fee_displayed",
})
# Live prod bug, next round: within a chosen item's own facts, a purely
# restrictive/negative fact (e.g. ta.loyalty_points.non_transferable -
# fact_type transferability_rule) could still happen to come first in
# KnowledgeService's own fact order, pushing the actual "what do I get"
# facts (LP amounts, Guest Passes, additional travelers, membership fees,
# Travel Credits never-expire) out of the tiny _MAX_FACTS budget entirely.
# Within own_item_facts specifically, these positive/actionable fact types
# are now bubbled to the front (stable sort - ties keep their prior order).
_VALUE_FACT_TYPES = frozenset({
    "loyalty_points_award", "guest_passes_allocation",
    "additional_travelers_included", "redemption_rate_cap",
    "membership_recurring_fee", "membership_activation_fee",
    "membership_total_due_today", "addon_price", "expiration_policy",
    "loyalty_points_transfer_cap",
})
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
    items = sorted(
        bundle.primary_items,
        key=lambda item: item.category in _GENERIC_OVERVIEW_CATEGORIES,
    )[:_MAX_ITEMS]
    item_ids = {item.id for item in items}
    # Live prod bug, next round: within the tiny _MAX_FACTS budget, the two
    # generic ta.platform.type/.use "core definition" facts always claimed 2
    # of 3 slots - useful for "What is Travel Advantage?" (ta.platform IS one
    # of the shown items there), but pure waste for a membership-value
    # question where ta.platform was already demoted out of `items` above:
    # its defining fact already reaches the model separately via
    # verified_claims regardless. Skipping this tier here frees the budget
    # for the actual current benefit facts (Elite LP, Guest Passes,
    # additional travelers, Travel Credits) instead.
    include_core_definition = any(item.stable_key == "ta.platform" for item in items)
    facts = _priority_facts(
        bundle.facts, limit=_MAX_FACTS, item_ids=item_ids,
        include_core_definition=include_core_definition,
    )
    compliance_keys = {fact.stable_key for fact in bundle.compliance_facts}
    official_facts = tuple(fact for fact in facts if fact.stable_key not in compliance_keys)
    source_facts = {"official_knowledge": {
        "items": tuple({
            "stable_key": item.stable_key, "title": item.title,
            "category": item.category,
            "content": _truncate_at_word_boundary(item.content, _MAX_ITEM_CONTENT_CHARS),
            "source_ref": _truncate_at_word_boundary(item.source_ref, _MAX_SOURCE_REF_CHARS),
        } for item in items),
        "canonical_facts": tuple(_fact_data(fact) for fact in official_facts),
        "provenance": tuple({
            "stable_key": source.stable_key,
            "source_reference": source.source_reference,
            "verification_status": source.verification_status,
        } for source in bundle.sources[:_MAX_PROVENANCE]),
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


def _truncate_at_word_boundary(text: str, limit: int) -> str:
    """Slice ``text`` to ``limit`` chars without splitting a word/identifier
    in half (live prod bug: a plain [:limit] slice turned "VIP180" into
    "VIP18"). Extends a few chars past ``limit`` to finish whatever word the
    hard cut would otherwise land inside; falls back to a hard cut only for
    an abnormally long single token so the budget can never blow up."""
    if len(text) <= limit:
        return text
    end = limit
    while end < len(text) and not text[end].isspace() and end - limit <= 10:
        end += 1
    return text[:end].rstrip()


def _fact_data(fact: KnowledgeFact) -> dict[str, Any]:
    values = {
        "stable_key": fact.stable_key, "fact_type": fact.fact_type,
        "subject_key": fact.subject_key,
        "qualifier_key": fact.qualifier_key, "qualifier_value": fact.qualifier_value,
        "value_number": _decimal(fact.value_number), "value_text": fact.value_text,
        "range_min": _decimal(fact.range_min), "range_max": _decimal(fact.range_max),
        "unit": fact.unit, "currency": fact.currency, "period": fact.period,
        "condition": fact.condition_text,
        "source_ref": _truncate_at_word_boundary(fact.source_ref, _MAX_SOURCE_REF_CHARS),
    }
    return {key: value for key, value in values.items() if value is not None}


def _verified_claim_facts(
    facts: tuple[KnowledgeFact, ...],
) -> tuple[KnowledgeFact, ...]:
    return _priority_facts(facts, limit=_MAX_VERIFIED_CLAIMS)


def _priority_facts(
    facts: tuple[KnowledgeFact, ...], *, limit: int,
    item_ids: frozenset[int] | None = None,
    include_core_definition: bool = True,
) -> tuple[KnowledgeFact, ...]:
    core_definition_fact_types = ("platform_type", "platform_use") if include_core_definition else ()
    priority_markers = (
        "conversion", "transfer", "member_bonus", "builder_bonus",
        "compensation", "guarantee",
    )
    # Live prod bug: within the small _MAX_FACTS budget, facts belonging to
    # the items build_knowledge_generation_context actually chose to show
    # (item_ids) must outrank facts from items it demoted/excluded - a broad
    # membership-value question used to let generic platform-category facts
    # (ta.category.hotels/cruises/...) and a legacy superseded-plan notice
    # crowd out the current Loyalty Points/Travel Credits/Life Experiences
    # facts that belong to the items actually surfaced above. None when the
    # caller has no item selection of its own (e.g. _verified_claim_facts,
    # which re-ranks an already-chosen, tiny fact set).
    # Same bug, other half: even among a chosen item's own facts, low-value
    # meta/legacy-notice fact types (a superseded-plan notice, the generic
    # platform service-category list) happened to sort ahead - alphabetically
    # or by KnowledgeService's own tie-break - of the actual current benefit
    # facts (Elite LP amounts, Guest Passes, Travel Credits rules) within the
    # same tiny limit. These carry no new grounding value for a typical
    # question and are deferred to the very end instead.
    ranked_facts = tuple(fact for fact in facts if fact.fact_type not in _LOW_PRIORITY_FACT_TYPES)
    deferred_facts = tuple(fact for fact in facts if fact.fact_type in _LOW_PRIORITY_FACT_TYPES)
    own_item_facts = tuple(sorted(
        (fact for fact in ranked_facts if item_ids is not None and fact.item_id in item_ids),
        key=lambda fact: fact.fact_type not in _VALUE_FACT_TYPES,
    )) if item_ids else ()
    prioritized = (
        *(fact for fact in ranked_facts if fact.fact_type in core_definition_fact_types),
        *own_item_facts,
        *ranked_facts[:1],
        *(fact for fact in ranked_facts if any(marker in fact.stable_key for marker in priority_markers)),
        *ranked_facts[1:],
        *deferred_facts,
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
