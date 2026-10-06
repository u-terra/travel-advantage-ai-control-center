"""Regression test for a real prod bug in app.services.knowledge_service.

Live prod question in @VladCRM (2026-10-06, after the Xlife knowledge
import): "Я уже плачу за Travel Advantage каждый месяц. Объясни простыми
словами, зачем мне сохранять членство и что конкретно я от него получаю?"

_retrieval_policy() only matched the pre-existing membership_intent stem
("член" in "членство"), which requires just ta.membership and
ta.membership.cancellation. Nothing routed this broad "what do I get"
question to the newly imported (2026-10-06) CURRENT benefit items, so the
generated answer stayed generic (platform description + refund terms) and
never surfaced Travel Credits, Loyalty Points, or Life Experiences.

Fixed by a narrow membership_value_intent check (membership_intent AND a
benefit-value stem such as "получ"/"дает"/"дает"/"польз"/"ценност"/"выгод")
that additionally requires the current Xlife items ta.travel_credits.nature,
ta.loyalty_points.rules_2026_10_06 and ta.life_experiences.definition_2026_10_06
- bumping max_primary_items from 5 to 6 so none of them get truncated out
alongside ta.platform/ta.membership/ta.membership.cancellation;
- ranking facts attached to a bounded PRIMARY item ahead of facts merely
pulled in via related/compliance items, so a required item's own facts
(e.g. Loyalty Points redemption cap) are not crowded out of the shared
max_facts budget by an unrelated item's large fact list (ta.platform's many
category facts) when neither has token overlap with the query.

This is scoped to its own stems, so plain cancellation/objection questions
and Ambassador-role questions are unaffected.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3
from typing import Any

from app.repositories.knowledge_repository import KnowledgeRepository
from app.services.knowledge_import import import_dataset
from app.services.knowledge_service import KnowledgeBundle, KnowledgeService

PRODUCT = Path("knowledge/travel_advantage/imports/product-structure.verified-official.v1.json")
COMPENSATION = Path("knowledge/travel_advantage/imports/compensation-plan.verified-official.v3.json")
DELTA = Path("knowledge/travel_advantage/imports/partner-training-delta.verified-official.v1.json")
LIVE_CONFIRMATION = Path(
    "knowledge/travel_advantage/imports/live-confirmation.verified-official.2026-10-05.json"
)
XLIFE = Path(
    "knowledge/travel_advantage/imports/xlife-advisor-synthesis.verified-official.2026-10-06.json"
)
RELATIONS = Path("knowledge/travel_advantage/imports/retrieval-readiness-relations.v1.sql")

_PRODUCTION_QUESTION = (
    "Я уже плачу за Travel Advantage каждый месяц. Объясни простыми словами, "
    "зачем мне сохранять членство и что конкретно я от него получаю?"
)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def service(tmp_path: Path, **limits: int) -> KnowledgeService:
    repository = KnowledgeRepository(tmp_path / "knowledge.sqlite3")
    for dataset in (PRODUCT, COMPENSATION, DELTA, LIVE_CONFIRMATION, XLIFE):
        run(import_dataset(dataset, repository))
    with sqlite3.connect(repository.db_path) as db:
        db.execute("PRAGMA foreign_keys = ON")
        db.executescript(RELATIONS.read_text(encoding="utf-8"))
    return KnowledgeService(repository, **limits)


def keys(bundle: KnowledgeBundle) -> set[str]:
    return {item.stable_key for item in (*bundle.primary_items, *bundle.related_items)}


def fact_stable_keys(bundle: KnowledgeBundle) -> set[str]:
    return {fact.stable_key for fact in (*bundle.facts, *bundle.compliance_facts)}


def test_membership_value_question_retrieves_current_benefit_items(tmp_path: Path) -> None:
    knowledge = service(tmp_path)
    bundle = run(knowledge.retrieve(_PRODUCTION_QUESTION))

    bundle_keys = keys(bundle)
    assert "ta.membership" in bundle_keys
    assert "ta.travel_credits.nature" in bundle_keys
    assert "ta.loyalty_points.rules_2026_10_06" in bundle_keys
    assert "ta.life_experiences.definition_2026_10_06" in bundle_keys

    # The new benefit items must be part of the bounded PRIMARY set, not
    # merely reachable as related items - otherwise generation still has no
    # reason to narrate them.
    primary_keys = {item.stable_key for item in bundle.primary_items}
    assert {
        "ta.travel_credits.nature",
        "ta.loyalty_points.rules_2026_10_06",
        "ta.life_experiences.definition_2026_10_06",
    } <= primary_keys


def test_membership_value_question_does_not_pull_ambassador_role_content(
    tmp_path: Path,
) -> None:
    knowledge = service(tmp_path)
    bundle = run(knowledge.retrieve(_PRODUCTION_QUESTION))

    bundle_keys = keys(bundle)
    assert "mwr.member_vs_ambassador" not in bundle_keys

    bundle_fact_keys = fact_stable_keys(bundle)
    assert "mwr.ambassador.registration_fee.usd" not in bundle_fact_keys
    assert "mwr.ambassador.renewal_fee.usd" not in bundle_fact_keys
    assert "mwr.member_vs_ambassador.distinct_roles" not in bundle_fact_keys


def test_membership_value_question_surfaces_current_benefit_facts(
    tmp_path: Path,
) -> None:
    """The membership-value cross-section should carry the CURRENT (2026-10-06)
    benefit facts attached directly to the newly-required items themselves
    (Loyalty Points redemption cap, Travel Credits expiration, Life
    Experiences inclusions) - not get crowded out of the shared max_facts
    budget by ta.platform's large, unrelated category-facts list."""
    knowledge = service(tmp_path)
    bundle = run(knowledge.retrieve(_PRODUCTION_QUESTION))

    fact_keys = fact_stable_keys(bundle)
    assert "ta.loyalty_points.max_dollar_offset_per_point" in fact_keys
    assert "ta.travel_credits.never_expire_current_site_claim" in fact_keys
    assert "ta.life_experiences.flights_generally_not_included" in fact_keys


def test_plain_cancellation_question_is_unaffected_by_value_intent(tmp_path: Path) -> None:
    """A plain cancellation/refund objection has none of the benefit-value
    stems and must keep behaving exactly as before - it must not pull in
    the new benefit items just because membership_intent fired."""
    knowledge = service(tmp_path)
    bundle = run(knowledge.retrieve("Можно ли отменить членство и вернуть деньги?"))

    bundle_keys = keys(bundle)
    assert "ta.membership.cancellation" in bundle_keys
    assert "ta.travel_credits.nature" not in bundle_keys
    assert "ta.loyalty_points.rules_2026_10_06" not in bundle_keys
    assert "ta.life_experiences.definition_2026_10_06" not in bundle_keys


def test_ambassador_role_question_is_unaffected_by_value_intent(tmp_path: Path) -> None:
    """An explicit ambassador/partner-role question must still resolve to
    mwr.member_vs_ambassador and must not be redirected toward Member
    benefit content by the new value-intent rule."""
    knowledge = service(tmp_path)
    bundle = run(knowledge.retrieve("Чем Member отличается от Lifestyle Ambassador?"))

    bundle_keys = keys(bundle)
    assert "mwr.member_vs_ambassador" in bundle_keys
    assert "ta.travel_credits.nature" not in bundle_keys
    assert "ta.loyalty_points.rules_2026_10_06" not in bundle_keys
    assert "ta.life_experiences.definition_2026_10_06" not in bundle_keys
