"""Bounded, structured projection of a retrieved bundle for generation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from app.domain.knowledge import KnowledgeFact
from app.services.knowledge_service import KnowledgeBundle

_MAX_ITEMS = 4
_MAX_FACTS = 3
_MAX_ITEM_CONTENT_CHARS = 40
# Live prod bug: the single sentence that actually answers "what does Elite
# get" ("Elite: 120 LP при enrollment + 120 LP при каждом успешном
# ежемесячном rebill...") lives entirely inside ta.loyalty_points.rules_
# 2026_10_06's own item content, past the generic _MAX_ITEM_CONTENT_CHARS
# cutoff - the model only ever saw "...Elite: 120 LP при enrollment" and
# never the monthly-rebill half. This item alone gets a larger content
# window (traded against a smaller default elsewhere, not a bigger total
# budget) so that sentence survives whole.
_ITEM_CONTENT_CHARS_OVERRIDES: Mapping[str, int] = {
    "ta.loyalty_points.rules_2026_10_06": 95,
}
_MAX_SOURCE_REF_CHARS = 10
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
#
# Live prod bug, next round: "Travel Credits never expire" (expiration_
# policy) is secondary metadata, not practical value - it kept winning a
# fact slot over genuinely actionable facts (Guest Passes allocation) that
# belong to the same chosen item. Removed from this set so it no longer
# outranks them; its item's own content still mentions it in passing if
# there is room, it just doesn't force out a better fact. Pricing facts
# (monthly fee, activation fee, total due today) were removed too - they
# answer "what do I pay", not "what do I get", and for a value/benefit
# question they only displaced a real benefit fact (Guest Passes) for the
# last of the tiny fact-budget slots.
_VALUE_FACT_TYPES = frozenset({
    "loyalty_points_award", "guest_passes_allocation",
    "additional_travelers_included", "redemption_rate_cap",
    "addon_price", "loyalty_points_transfer_cap",
})
# Live prod bug, next round: raw English lifecycle terms from the knowledge
# text itself ("enrollment", "redemption", bare "membership") reached the
# model as-is inside item content/fact text and then leaked straight into
# the Russian answer - the client-reply synthesis instruction alone only
# ever had a chance to catch this if the model happened to comply. This
# normalizes the grounding text itself at the generation-context layer, so
# the jargon is never even offered to the model. Deliberately NOT applied to
# machine-readable fields (stable_key, fact_type, subject_key, qualifier_*)
# which are identifiers, not prose, and must stay exact for provenance.
_JARGON_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    # "при X" almost always wants the Russian prepositional case - checked
    # ahead of the bare-word fallback below so e.g. "при enrollment" becomes
    # the grammatical "при подключении", not "при подключение".
    (re.compile(r"\bпри enrollment\b", re.IGNORECASE), "при подключении"),
    (re.compile(r"\benrollment\b", re.IGNORECASE), "подключение"),
    (re.compile(r"\bredemption\b", re.IGNORECASE), "списание баллов"),
    (re.compile(r"\bmembership\b", re.IGNORECASE), "членство"),
)
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
            "stable_key": item.stable_key,
            "title": _localize_jargon(item.title),
            "category": item.category,
            "content": _truncate_at_word_boundary(
                _localize_jargon(item.content),
                _ITEM_CONTENT_CHARS_OVERRIDES.get(item.stable_key, _MAX_ITEM_CONTENT_CHARS),
            ),
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


def _localize_jargon(text: str | None) -> str | None:
    """Replace raw English lifecycle terms with their normal Russian
    equivalent wherever one exists, so grounding text can never hand the
    model jargon to leak into a Russian answer (see _JARGON_REPLACEMENTS)."""
    if not text:
        return text
    for pattern, replacement in _JARGON_REPLACEMENTS:
        text = pattern.sub(replacement, text)
    return text


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
        "value_number": _decimal(fact.value_number),
        "value_text": _localize_jargon(fact.value_text),
        "range_min": _decimal(fact.range_min), "range_max": _decimal(fact.range_max),
        "unit": fact.unit, "currency": fact.currency, "period": fact.period,
        "condition": _localize_jargon(fact.condition_text),
        "source_ref": _truncate_at_word_boundary(fact.source_ref, _MAX_SOURCE_REF_CHARS),
    }
    return {key: value for key, value in values.items() if value is not None}


def _verified_claim_facts(
    facts: tuple[KnowledgeFact, ...],
) -> tuple[KnowledgeFact, ...]:
    return _priority_facts(facts, limit=_MAX_VERIFIED_CLAIMS)


def _strongest_per_value_fact_type(
    facts: tuple[KnowledgeFact, ...],
) -> tuple[KnowledgeFact, ...]:
    """Live prod bug, next round: two VALUE_FACT_TYPES facts of the same
    kind but different subjects (e.g. "1 additional traveler" on VIP vs "4
    additional travelers" on Elite) could both be candidates, and the
    weaker one (VIP's) happened to sort first and spend one of the tiny
    fact-budget slots - leaving no room for a genuinely different benefit
    (Guest Passes). For numeric value facts sharing a fact_type, only the
    strongest (highest value_number) survives, at the position of its
    group's first occurrence; every other fact type/subject is untouched."""
    best_by_type: dict[str, KnowledgeFact] = {}
    for fact in facts:
        if fact.fact_type in _VALUE_FACT_TYPES and fact.value_number is not None:
            current = best_by_type.get(fact.fact_type)
            if current is None or fact.value_number > current.value_number:
                best_by_type[fact.fact_type] = fact
    result: list[KnowledgeFact] = []
    seen_types: set[str] = set()
    for fact in facts:
        if fact.fact_type in _VALUE_FACT_TYPES and fact.value_number is not None:
            if fact.fact_type in seen_types:
                continue
            seen_types.add(fact.fact_type)
            result.append(best_by_type[fact.fact_type])
        else:
            result.append(fact)
    return tuple(result)


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
    own_item_facts = _strongest_per_value_fact_type(tuple(sorted(
        (fact for fact in ranked_facts if item_ids is not None and fact.item_id in item_ids),
        key=lambda fact: fact.fact_type not in _VALUE_FACT_TYPES,
    ))) if item_ids else ()
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
        value = _localize_jargon(fact.value_text)
    elif fact.value_number is not None:
        value = f"{_decimal(fact.value_number)}{_unit_suffix(fact)}"
    else:
        value = f"{_decimal(fact.range_min)}–{_decimal(fact.range_max)}{_unit_suffix(fact)}"
    qualifier = (
        f"; {fact.qualifier_key}={fact.qualifier_value}"
        if fact.qualifier_key and fact.qualifier_value else ""
    )
    condition = (
        f"; условие: {_localize_jargon(fact.condition_text)}" if fact.condition_text else ""
    )
    return f"{fact.subject_key}: {value}{qualifier}{condition}"


def _unit_suffix(fact: KnowledgeFact) -> str:
    values = tuple(value for value in (fact.currency, fact.unit, fact.period) if value)
    return f" {' / '.join(values)}" if values else ""


def _decimal(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None
