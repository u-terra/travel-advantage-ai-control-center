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
RELATIONS = Path("knowledge/travel_advantage/imports/retrieval-readiness-relations.v1.sql")


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def service(tmp_path: Path, **limits: int) -> KnowledgeService:
    repository = KnowledgeRepository(tmp_path / "knowledge.sqlite3")
    for dataset in (PRODUCT, COMPENSATION, DELTA):
        run(import_dataset(dataset, repository))
    with sqlite3.connect(repository.db_path) as db:
        db.execute("PRAGMA foreign_keys = ON")
        db.executescript(RELATIONS.read_text(encoding="utf-8"))
    return KnowledgeService(repository, **limits)


def keys(bundle: KnowledgeBundle) -> set[str]:
    return {item.stable_key for item in (*bundle.primary_items, *bundle.related_items)}


def fact_keys(bundle: KnowledgeBundle) -> set[str]:
    return {fact.stable_key for fact in (*bundle.facts, *bundle.compliance_facts)}


def test_search_text_ranking_tags_deduplication_and_empty_query(tmp_path: Path) -> None:
    knowledge = service(tmp_path)
    repository = knowledge.repository

    assert run(repository.search_text("ta.guest_pass", 3))[0].stable_key == "ta.guest_pass"
    assert run(repository.search_text("Guest Pass", 3))[0].stable_key == "ta.guest_pass"
    assert run(repository.search_text("зависло бронирование", 3))[0].stable_key == "ta.booking_status_inventory"
    results = run(repository.search_text("loyalty points travel credits", 10))
    assert len({item.stable_key for item in results}) == len(results)
    assert run(repository.search_text("   ")) == []
    assert run(repository.search_text("что это и как")) == []
    assert run(repository.search_text("Travel Advantage", 0)) == []


def test_bundle_is_bounded_and_provenance_is_compact(tmp_path: Path) -> None:
    knowledge = service(tmp_path, max_primary_items=4, max_related_items=6, max_facts=12)
    bundle = run(knowledge.retrieve("Расскажи подробно о Travel Advantage"))

    assert len(bundle.primary_items) <= 4
    assert len(bundle.related_items) <= 6
    assert len(bundle.facts) + len(bundle.compliance_facts) <= 12
    assert bundle.sources
    assert all(source.source_reference for source in bundle.sources)
    assert all(source.verification_status == "verified_official" for source in bundle.sources)


def test_safety_critical_retrieval(tmp_path: Path) -> None:
    knowledge = service(tmp_path)

    income = run(knowledge.retrieve("Сколько я гарантированно заработаю через месяц?"))
    assert "mwr.claims_and_staleness_compliance" in keys(income)
    assert "mwr.compliance.no_specific_income_guarantee" in fact_keys(income)
    assert not any(f.fact_type == "dual_team_monthly_table_income" for f in income.facts)

    price = run(knowledge.retrieve("Travel Advantage всегда дешевле Booking?"))
    assert "ta.best_price_guarantee" in keys(price)
    assert "ta.best_price_guarantee.rate" in fact_keys(price)
    assert "ta.compliance.no_absolute_lowest_price" in fact_keys(price)

    current = run(knowledge.retrieve("Какой сейчас актуальный Life Experience?"))
    assert "ta.life_experiences" in keys(current)
    assert "ta.compliance.current_information_check" in fact_keys(current)

    silver = run(knowledge.retrieve("Что получает Silver с продажи Elite Turbo?"))
    assert {"ta.member_bonus", "ta.builder_bonus"} <= keys(silver)
    assert {
        "ta.member_bonus.elite_turbo.usd",
        "ta.builder_bonus.silver.personal_elite.usd",
        "ta.elite_turbo.compensation_components",
    } <= fact_keys(silver)

    benefits = run(knowledge.retrieve("Чем Loyalty Points отличаются от Travel Credits?"))
    assert {"ta.loyalty_points", "ta.travel_credits", "ta.points_transfer_and_use_delta"} <= keys(benefits)
    assert {
        "ta.loyalty_points.not_transferable",
        "ta.travel_credits.transferable",
        "ta.loyalty_points.conversion.1_to_1_usd",
        "ta.travel_credits.life_experience.not_applicable",
    } <= fact_keys(benefits)


def test_broad_retrieval_expands_overview_without_dumping_database(tmp_path: Path) -> None:
    knowledge = service(tmp_path)
    travel = run(knowledge.retrieve("Расскажи подробно о Travel Advantage"))
    assert "ta.platform" in keys(travel)
    assert {
        "ta.guest_pass", "ta.best_price_guarantee", "ta.elite_turbo_features",
        "ta.booking_status_inventory",
    } <= keys(travel)
    assert len(travel.facts) + len(travel.compliance_facts) <= knowledge.max_facts

    partner = run(knowledge.retrieve("Что должен знать новый партнёр MWR Life?"))
    assert {
        "mwr.life", "mwr.getting_started", "mwr.partner_product_knowledge",
        "mwr.lifestyle_ambassador",
    } <= keys(partner)


def test_ruby_is_explicitly_ambiguous(tmp_path: Path) -> None:
    bundle = run(service(tmp_path).retrieve("Какие выплаты у Ruby?"))
    assert bundle.potentially_ambiguous
    assert {"ta.dual_team_income", "ta.builder_bonus", "ta.rank_qualification"} <= keys(bundle)
    assert bundle.ambiguity_reasons


def test_all_25_retrieval_scenarios(tmp_path: Path) -> None:
    knowledge = service(tmp_path)
    scenarios = [
        ("Что такое Travel Advantage?", {"ta.platform"}, "PASS"),
        ("Расскажи подробно о Travel Advantage", {"ta.platform", "ta.guest_pass"}, "PASS"),
        ("Что должен знать новый партнёр MWR Life?", {"mwr.getting_started", "mwr.partner_product_knowledge"}, "PASS"),
        ("Чем Travel Advantage отличается от MWR Life?", {"ta.platform", "mwr.life"}, "PASS"),
        ("Какие есть тарифы и чем отличается Elite Turbo?", {"ta.membership", "ta.elite_turbo_features"}, "PASS"),
        ("Что такое Loyalty Points?", {"ta.loyalty_points"}, "PASS"),
        ("Чем Loyalty Points отличаются от Travel Credits?", {"ta.loyalty_points", "ta.travel_credits"}, "PASS"),
        ("Можно ли передавать Travel Credits другому человеку?", {"ta.travel_credits", "ta.points_transfer_and_use_delta"}, "PASS"),
        ("Что такое Guest Pass?", {"ta.guest_pass"}, "PASS"),
        ("Что такое 150% Best Price Guarantee?", {"ta.best_price_guarantee"}, "PASS"),
        ("Что делать, если зависло бронирование?", {"ta.booking_status_inventory", "ta.support"}, "PASS"),
        ("Куда обращаться по проблеме с бронью?", {"ta.support"}, "PASS"),
        ("Куда обращаться по комиссии?", {"ta.payments_and_support_routing"}, "PASS"),
        ("Что такое Registration Team и Dual Team?", {"mwr.team_structures"}, "PASS"),
        ("Как закрывается Silver?", {"ta.rank_qualification"}, "PASS"),
        ("Что получает Silver с продажи Elite Turbo?", {"ta.member_bonus", "ta.builder_bonus"}, "PASS"),
        ("Как работает Builder Bonus?", {"ta.builder_bonus"}, "PASS"),
        ("Какие выплаты у Ruby?", {"ta.dual_team_income", "ta.builder_bonus", "ta.rank_qualification"}, "PARTIAL"),
        ("Что такое MWR Academy?", {"mwr.getting_started"}, "PARTIAL"),
        ("Что такое L.I.F.E. Cycle?", {"mwr.getting_started"}, "PARTIAL"),
        ("С чего начать новому партнёру?", {"mwr.getting_started"}, "PASS"),
        ("Можно ли оплачивать криптовалютой?", {"ta.payments_and_support_routing"}, "PASS"),
        ("Travel Advantage всегда дешевле Booking?", {"ta.best_price_guarantee"}, "PASS"),
        ("Сколько я гарантированно заработаю через месяц?", {"mwr.claims_and_staleness_compliance"}, "PASS"),
        ("Какой сейчас актуальный Life Experience?", {"ta.life_experiences", "mwr.claims_and_staleness_compliance"}, "PASS"),
    ]
    results: list[str] = []
    for question, expected, status in scenarios:
        bundle = run(knowledge.retrieve(question))
        assert expected <= keys(bundle), question
        if status == "PARTIAL" and "Ruby" not in question:
            assert bundle.missing_definitions, question
        if "Ruby" in question:
            assert bundle.potentially_ambiguous
        results.append(status)
    assert results.count("PASS") == 22
    assert results.count("PARTIAL") == 3
    assert results.count("FAIL") == 0
