"""Regression tests for a real prod bug in app.services.knowledge_service.

"Зачем платить за членство каждый месяц, если я и без клуба могу сам
бронировать отели и покупать билеты? Помоги мне ответить честно, без
давления." matched only "booking" (from "бронировать") in _retrieval_policy -
there was no rule at all for "членство"/"клуб"/"подписка", so booking-backend
facts (pending/additional verification/supplier update delay) became the
entire required knowledge core instead of membership value/cancellation/
Member-vs-Ambassador facts, even though the question is about membership and
booking is only mentioned inside a comparison.

Fixed by:
- a minimal membership_intent stem check in _retrieval_policy() that adds
  ta.membership/ta.membership.cancellation as required items;
- suppressing the "booking" in query -> required booking-backend rule when
  membership_intent is also explicit (a genuine booking-only question keeps
  matching it unchanged);
- ru->en aliases ("членство"/"членский"/"клуб"/"подписка" -> "membership")
  in app.repositories.knowledge_repository._SEARCH_TOKEN_ALIASES, so the
  free-text search_text() fallback can also find these items by their
  existing "membership" tag.

Follow-up prod bug (same question class): membership_intent ALSO
unconditionally required mwr.member_vs_ambassador - the answer then drifted
into "you can register as Ambassador without a travel service"/Compensation
Plan, irrelevant to a plain membership-value objection. mwr.member_vs_
ambassador is now required only on its own, separate ambassador_role_intent
(ambassador/амбассадор/partner/регистрац/роль) - never just because
membership_intent fired.
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
RELATIONS = Path("knowledge/travel_advantage/imports/retrieval-readiness-relations.v1.sql")

_PRODUCTION_QUESTION = (
    "Человек говорит: «Зачем мне платить за членство каждый месяц, если я и "
    "без клуба могу сам бронировать отели и покупать билеты?» Помоги мне "
    "ответить честно, без давления."
)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def service(tmp_path: Path, **limits: int) -> KnowledgeService:
    repository = KnowledgeRepository(tmp_path / "knowledge.sqlite3")
    for dataset in (PRODUCT, COMPENSATION, DELTA, LIVE_CONFIRMATION):
        run(import_dataset(dataset, repository))
    with sqlite3.connect(repository.db_path) as db:
        db.execute("PRAGMA foreign_keys = ON")
        db.executescript(RELATIONS.read_text(encoding="utf-8"))
    return KnowledgeService(repository, **limits)


def keys(bundle: KnowledgeBundle) -> set[str]:
    return {item.stable_key for item in (*bundle.primary_items, *bundle.related_items)}


def fact_stable_keys(bundle: KnowledgeBundle) -> set[str]:
    return {fact.stable_key for fact in (*bundle.facts, *bundle.compliance_facts)}


# --- A: the exact production report -----------------------------------------


def test_membership_objection_question_gets_membership_facts_not_booking_core(
    tmp_path: Path,
) -> None:
    knowledge = service(tmp_path)
    bundle = run(knowledge.retrieve(_PRODUCTION_QUESTION))

    bundle_keys = keys(bundle)
    assert "ta.membership" in bundle_keys
    assert "ta.membership.cancellation" in bundle_keys

    # Live prod bug (follow-up): this plain membership-value question has no
    # ambassador/partner/registration/role intent of its own - the answer
    # must not drift into "register as Ambassador without a travel service"/
    # Compensation Plan. mwr.member_vs_ambassador is a DIFFERENT topic (the
    # Member vs Ambassador role distinction) and must not be required here.
    assert "mwr.member_vs_ambassador" not in bundle_keys

    # The booking-backend items must not be part of the REQUIRED core for
    # this question - search_text() may still surface them as a weak
    # lexical match, but they must not crowd out membership items from the
    # bounded primary_items list.
    assert "ta.membership" in {item.stable_key for item in bundle.primary_items}


# --- B: a genuine booking-only question must be unaffected ------------------


def test_pure_booking_question_still_gets_booking_facts(tmp_path: Path) -> None:
    knowledge = service(tmp_path)
    bundle = run(knowledge.retrieve("Почему бронирование после оплаты pending?"))

    bundle_keys = keys(bundle)
    assert "ta.booking_status_inventory" in bundle_keys
    assert "ta.support" in bundle_keys


# --- C: cancellation/refund objection -----------------------------------


def test_cancellation_question_gets_cancellation_facts(tmp_path: Path) -> None:
    knowledge = service(tmp_path)
    bundle = run(knowledge.retrieve("Можно ли отменить членство и вернуть деньги?"))

    bundle_keys = keys(bundle)
    assert "ta.membership.cancellation" in bundle_keys
    assert {
        "ta.membership.cancellation.allowed",
        "ta.membership.refund.14_day_window",
    } <= fact_stable_keys(bundle)


# --- D: Member vs Lifestyle Ambassador ---------------------------------


def test_member_vs_ambassador_question_gets_the_right_item(tmp_path: Path) -> None:
    knowledge = service(tmp_path)
    bundle = run(knowledge.retrieve("Чем Member отличается от Lifestyle Ambassador?"))

    assert "mwr.member_vs_ambassador" in keys(bundle)
    assert "mwr.member_vs_ambassador.distinct_roles" in fact_stable_keys(bundle)


# --- E: free-text search fallback alone (no _retrieval_policy keyword) ------


def test_search_text_fallback_finds_membership_by_russian_word(tmp_path: Path) -> None:
    knowledge = service(tmp_path)
    results = run(knowledge.repository.search_text("членство", 5))
    assert any(item.stable_key == "ta.membership" for item in results)


def test_search_text_fallback_finds_membership_via_club_and_subscription(
    tmp_path: Path,
) -> None:
    knowledge = service(tmp_path)
    for word in ("клуб", "подписка"):
        results = run(knowledge.repository.search_text(word, 5))
        assert any(item.stable_key == "ta.membership" for item in results), word


# --- Regression guard: suppression is scoped to the "booking" catch-all ----


def test_membership_intent_suppresses_only_the_generic_booking_catch_all(
    tmp_path: Path,
) -> None:
    """membership_intent only ever gates the bare "booking" in query rule -
    it must not remove membership facts, and a question that mentions
    booking alongside an explicit membership word still gets the membership
    core (the generic booking-backend catch-all is what yields, not the
    other way around)."""
    knowledge = service(tmp_path)
    bundle = run(knowledge.retrieve(
        "Зачем платить за подписку, если бронировать можно и так?"
    ))
    bundle_keys = keys(bundle)
    assert "ta.membership" in bundle_keys
    assert "ta.booking_status_inventory" not in bundle_keys


# --- Regression guard: member_vs_ambassador requires its own, separate intent


def test_production_membership_question_does_not_pull_in_ambassador_registration(
    tmp_path: Path,
) -> None:
    """The exact production report, second half: a plain membership-value
    objection must not drag Ambassador-registration/Compensation Plan facts
    into the required core - that is a different topic (the Member vs
    Ambassador role distinction), not what was asked."""
    knowledge = service(tmp_path)
    bundle = run(knowledge.retrieve(_PRODUCTION_QUESTION))

    bundle_keys = keys(bundle)
    assert "mwr.member_vs_ambassador" not in bundle_keys

    bundle_fact_keys = fact_stable_keys(bundle)
    assert "mwr.ambassador.registration_without_travel_service" not in bundle_fact_keys
    assert "mwr.membership.not_mandatory_for_ambassador_registration" not in bundle_fact_keys
    assert "mwr.member_vs_ambassador.distinct_roles" not in bundle_fact_keys


def test_ambassador_role_intent_still_requires_member_vs_ambassador(
    tmp_path: Path,
) -> None:
    """Requirement (4): an explicit ambassador/partner/registration/role
    question must still get mwr.member_vs_ambassador - only the blanket
    membership_intent trigger was removed, not the item itself."""
    knowledge = service(tmp_path)
    for question in (
        "Чем Member отличается от Lifestyle Ambassador?",
        "Можно ли зарегистрироваться как ambassador без travel membership?",
        "Какая роль у партнёра в этой программе?",
    ):
        bundle = run(knowledge.retrieve(question))
        assert "mwr.member_vs_ambassador" in keys(bundle), question
