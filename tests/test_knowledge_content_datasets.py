from __future__ import annotations

import asyncio
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.repositories.knowledge_repository import KnowledgeRepository
from app.services.knowledge_import import import_dataset, load_and_validate_dataset


PRODUCT = Path("knowledge/travel_advantage/imports/product-structure.verified-official.v1.json")
COMPENSATION = Path("knowledge/travel_advantage/imports/compensation-available.verified-official.v2.json")
COMPENSATION_V3 = Path("knowledge/travel_advantage/imports/compensation-plan.verified-official.v3.json")
PARTNER_TRAINING_DELTA = Path(
    "knowledge/travel_advantage/imports/partner-training-delta.verified-official.v1.json"
)
RETRIEVAL_RELATIONS = Path(
    "knowledge/travel_advantage/imports/retrieval-readiness-relations.v1.sql"
)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_content_datasets_are_verified_and_hash_bound() -> None:
    for path in (PRODUCT, COMPENSATION, PARTNER_TRAINING_DELTA):
        dataset = load_and_validate_dataset(path)
        assert all(source["verification_status"] == "verified_official" for source in dataset["sources"])


def test_product_facts_and_relations_are_retrievable(tmp_path: Path) -> None:
    repository = KnowledgeRepository(tmp_path / "knowledge.sqlite3")
    run(import_dataset(PRODUCT, repository))

    prices = run(repository.get_facts(fact_type="membership_total_price"))
    assert [(fact.subject_key, fact.value_number) for fact in prices] == [
        ("membership.vip", Decimal("19.97")),
        ("membership.elite", Decimal("338.97")),
        ("membership.elite_turbo", Decimal("598.97")),
    ]
    features = run(repository.get_related("ta.platform", "has_feature"))
    assert {item.stable_key for item in features} == {
        "ta.loyalty_points", "ta.travel_credits", "ta.life_experiences"
    }
    distinct = run(repository.get_related("ta.loyalty_points", "distinct_from"))
    assert [item.stable_key for item in distinct] == ["ta.travel_credits"]


def test_compensation_rows_are_canonical_and_examples_reference_them(tmp_path: Path) -> None:
    repository = KnowledgeRepository(tmp_path / "knowledge.sqlite3")
    run(import_dataset(COMPENSATION, repository))
    run(import_dataset(COMPENSATION, repository))

    daily = run(repository.get_facts(fact_type="dual_team_daily_income"))
    assert [(fact.subject_key, fact.value_number) for fact in daily] == [
        ("rank.silver", Decimal("4")), ("rank.ruby", Decimal("300"))
    ]
    example = run(repository.get_example("ta.example.ruby.downline_silver.elite_turbo"))
    assert example is not None
    assert example.fact_keys == (
        "ta.builder_bonus.ruby.elite_component.usd",
        "ta.builder_bonus.silver.personal_elite.usd",
        "ta.elite_turbo.compensation_components",
    )


def test_compensation_v3_complete_tables_and_control_values(tmp_path: Path) -> None:
    repository = KnowledgeRepository(tmp_path / "knowledge.sqlite3")
    run(import_dataset(COMPENSATION_V3, repository))

    daily = run(repository.get_facts(fact_type="dual_team_daily_income"))
    monthly = run(repository.get_facts(fact_type="dual_team_monthly_table_income"))
    points = run(repository.get_facts(fact_type="dual_team_table_points"))
    builder = run(repository.get_facts(fact_type="builder_bonus_rank_amount"))
    registration = run(repository.get_facts(fact_type="rank_registration_team_points_required"))
    rank_dual = run(repository.get_facts(fact_type="rank_dual_team_points_required"))
    missing = run(repository.get_facts(fact_type="missing_official_value"))

    assert len(daily) == len(monthly) == len(points) == 19
    assert len(builder) == 20  # 19 ranks plus the explicit no-rank row
    assert len(registration) == 19
    assert len(rank_dual) == 16
    assert {f.subject_key for f in missing} >= {
        "rank.silver.dual_team_points", "rank.gold.dual_team_points",
        "rank.platinum.dual_team_points", "vip_builder_bonus",
    }
    values = {fact.subject_key: fact.value_number for fact in daily}
    assert values["rank.silver"] == Decimal("4")
    assert values["rank.ruby"] == Decimal("300")
    assert values["rank.black_royal"] == Decimal("15000")


def test_compensation_v3_derived_examples_and_bonus_controls(tmp_path: Path) -> None:
    repository = KnowledgeRepository(tmp_path / "knowledge.sqlite3")
    run(import_dataset(COMPENSATION_V3, repository))

    expected_examples = {
        "ta.example.silver.personal_elite": Decimal("50"),
        "ta.example.silver.personal_elite_turbo": Decimal("100"),
        "ta.example.ruby.personal_elite": Decimal("110"),
        "ta.example.ruby.personal_elite_turbo": Decimal("220"),
    }
    for key, expected in expected_examples.items():
        example = run(repository.get_example(key))
        assert example is not None and example.result_number == expected

    controls = {
        fact.stable_key: fact.value_number
        for fact in run(repository.get_facts())
    }
    assert controls["ta.fast_start.max_total.usd"] == Decimal("450")
    assert controls["ta.acceleration.gold.amount.usd"] == Decimal("600")
    assert controls["ta.acceleration.platinum.amount.usd"] == Decimal("1200")
    assert controls["ta.presidential.rate.percent"] == Decimal("20")
    assert controls["ta.car_bonus.monthly.usd"] == Decimal("500")
    assert controls["ta.car_bonus.cash_alternative.usd"] == Decimal("250")
    assert controls["ta.registration_team.points.black_royal.pv"] == Decimal("350000")


def test_partner_training_delta_controls_and_idempotence(tmp_path: Path) -> None:
    repository = KnowledgeRepository(tmp_path / "knowledge.sqlite3")
    run(import_dataset(PRODUCT, repository))
    run(import_dataset(COMPENSATION_V3, repository))
    run(import_dataset(PARTNER_TRAINING_DELTA, repository))
    before = run(repository.get_facts())
    run(import_dataset(PARTNER_TRAINING_DELTA, repository))
    after = run(repository.get_facts())

    assert len(after) == len(before)
    facts = {fact.stable_key: fact for fact in after}
    assert facts["ta.travel_credits.transferable"].value_text.startswith("Можно передавать")
    assert facts["ta.loyalty_points.not_transferable"].value_text.startswith("Нельзя передавать")
    assert facts["ta.travel_credits.life_experience.not_applicable"].value_text.startswith("Не используются")
    assert facts["ta.elite_turbo.life_experience.second_guest_lp_limit"].value_number == Decimal("100")
    assert facts["ta.loyalty_points.conversion.1_to_1_usd"].value_number == Decimal("1")
    assert "пределах" in (facts["ta.loyalty_points.conversion.1_to_1_usd"].condition_text or "")
    assert facts["ta.vip.additional_travelers.limit"].value_number == Decimal("1")
    assert facts["ta.elite.additional_travelers.limit"].value_number == Decimal("4")
    assert facts["ta.membership.vip.qualification_points"].value_number == Decimal("1")
    assert facts["ta.membership.elite.pv"].value_number == Decimal("6")
    assert facts["ta.membership.elite_turbo.pv"].value_number == Decimal("6")
    assert "не 12 PV" in (facts["ta.membership.elite_turbo.no_double_pv"].value_text or "")
    assert facts["ta.best_price_guarantee.rate"].value_number == Decimal("150")
    assert facts["mwr.support.email"].value_text == "support@mwrlife.com"


def test_retrieval_readiness_relations_connect_overview_items(tmp_path: Path) -> None:
    repository = KnowledgeRepository(tmp_path / "knowledge.sqlite3")
    run(import_dataset(PRODUCT, repository))
    run(import_dataset(COMPENSATION_V3, repository))
    run(import_dataset(PARTNER_TRAINING_DELTA, repository))
    with sqlite3.connect(repository.db_path) as db:
        db.execute("PRAGMA foreign_keys = ON")
        db.executescript(RETRIEVAL_RELATIONS.read_text(encoding="utf-8"))
        db.executescript(RETRIEVAL_RELATIONS.read_text(encoding="utf-8"))
        assert not db.execute("PRAGMA foreign_key_check").fetchall()

    travel = {item.stable_key for item in run(repository.get_related("ta.platform"))}
    assert {
        "ta.guest_pass", "ta.best_price_guarantee", "ta.elite_turbo_features",
        "ta.booking_status_inventory", "ta.payments_and_support_routing",
    } <= travel
    mwr = {item.stable_key for item in run(repository.get_related("mwr.life"))}
    assert {
        "mwr.lifestyle_ambassador", "mwr.team_structures", "mwr.getting_started",
        "mwr.qualification_status",
    } <= mwr
    assert [item.stable_key for item in run(repository.get_related("mwr.getting_started"))] == [
        "mwr.partner_product_knowledge"
    ]
