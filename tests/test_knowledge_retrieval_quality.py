from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tests.test_knowledge_retrieval import fact_keys, keys, run, service


@dataclass(frozen=True)
class EvalCase:
    query: str
    required_items: frozenset[str] = frozenset()
    required_facts: frozenset[str] = frozenset()
    status: str = "PASS"
    expect_empty: bool = False
    forbid_compensation: bool = False


CASES = (
    EvalCase("сколько сильвер получает в день", frozenset({"ta.dual_team_income"})),
    EvalCase("руби сколько платят", frozenset({"ta.dual_team_income", "ta.builder_bonus", "ta.rank_qualification"}), status="PARTIAL"),
    EvalCase("что мне даст турбо", frozenset({"ta.membership", "ta.elite_turbo_features"})),
    EvalCase("элит турбо это 12 pv?", frozenset({"ta.membership", "mwr.qualification_status"}), frozenset({"ta.membership.elite_turbo.no_double_pv"})),
    EvalCase("баллы можно вывести?", frozenset({"ta.loyalty_points"}), frozenset({"ta.loyalty_points.no_cash_exchange"})),
    EvalCase("тревел кредиты можно подарить?", frozenset({"ta.travel_credits", "ta.points_transfer_and_use_delta"}), frozenset({"ta.travel_credits.transferable"})),
    EvalCase("поинт это доллар?", frozenset({"ta.loyalty_points", "ta.points_transfer_and_use_delta"}), frozenset({"ta.loyalty_points.conversion.1_to_1_usd"})),
    EvalCase("лайф экспириенс можно весь оплатить баллами?", frozenset({"ta.life_experiences", "ta.loyalty_points"})),
    EvalCase("гостевой доступ это что", frozenset({"ta.guest_pass"})),
    EvalCase("гарантия 150 процентов как работает", frozenset({"ta.best_price_guarantee"}), frozenset({"ta.best_price_guarantee.rate", "ta.compliance.no_absolute_lowest_price"})),
    EvalCase("у меня бронь зависла что делать", frozenset({"ta.booking_status_inventory", "ta.support"})),
    EvalCase("куда писать если комиссию не начислили", frozenset({"ta.payments_and_support_routing"}), frozenset({"mwr.support.email"})),
    EvalCase("как стать сильвер", frozenset({"ta.rank_qualification"})),
    EvalCase("что такое бинар", frozenset({"mwr.team_structures"})),
    EvalCase("двойная команда и регистрационная одно и то же?", frozenset({"mwr.team_structures"})),
    EvalCase("я точно буду получать 300 долларов если стану руби?", frozenset({"ta.dual_team_income", "mwr.claims_and_staleness_compliance"}), frozenset({"mwr.compliance.no_specific_income_guarantee"}), status="PARTIAL"),
    EvalCase("какой сейчас есть лайф экспириенс", frozenset({"ta.life_experiences", "mwr.claims_and_staleness_compliance"}), frozenset({"ta.compliance.current_information_check"})),
    EvalCase("сколько сейчас стоит elite", frozenset({"ta.membership", "mwr.claims_and_staleness_compliance"}), frozenset({"ta.compliance.current_information_check"})),
    EvalCase("можно криптой оплатить", frozenset({"ta.payments_and_support_routing"}), frozenset({"ta.payments.crypto.availability"})),
    EvalCase("что новому партнеру делать сначала", frozenset({"mwr.getting_started", "mwr.partner_product_knowledge"})),
    EvalCase("мвр академия что это", frozenset({"mwr.getting_started"}), status="PARTIAL"),
    EvalCase("life cycle что это", frozenset({"mwr.getting_started"}), status="PARTIAL"),
    EvalCase("Travel Advantge что это", frozenset({"ta.platform"})),
    EvalCase("loyality points vs travle credtis", frozenset({"ta.loyalty_points", "ta.travel_credits"})),
    EvalCase("Guset Pass?", frozenset({"ta.guest_pass"})),
    EvalCase("Best Price Garantee 150%", frozenset({"ta.best_price_guarantee"})),
    EvalCase("бронь пока пендинг, это нормально?", frozenset({"ta.booking_status_inventory", "ta.support"})),
    EvalCase("confirmation не пришел после оплаты", frozenset({"ta.booking_status_inventory", "ta.support"})),
    EvalCase("support по отелю куда?", frozenset({"ta.support"})),
    EvalCase("commission qualification куда эскалировать", frozenset({"ta.payments_and_support_routing"})),
    EvalCase("VIP или Elite — в чем разница", frozenset({"ta.membership"})),
    EvalCase("flex traveller входит куда", frozenset({"ta.elite_turbo_features"})),
    EvalCase("100 points за второго гостя в Life Experience", frozenset({"ta.elite_turbo_features", "ta.life_experiences"}), frozenset({"ta.elite_turbo.life_experience.second_guest_lp_limit"})),
    EvalCase("можно ли отдать loyalty points другу", frozenset({"ta.loyalty_points"}), frozenset({"ta.loyalty_points.not_transferable"})),
    EvalCase("travel credits работают на life experience?", frozenset({"ta.travel_credits", "ta.life_experiences"}), frozenset({"ta.travel_credits.life_experience.not_applicable"})),
    EvalCase("1 LP равен 1 USD?", frozenset({"ta.loyalty_points", "ta.points_transfer_and_use_delta"}), frozenset({"ta.loyalty_points.conversion.1_to_1_usd"})),
    EvalCase("6 pv у turbo или больше?", frozenset({"ta.membership", "mwr.qualification_status"})),
    EvalCase("12 pv turbo из-за двух компонентов?", frozenset({"mwr.qualification_status"}), frozenset({"ta.membership.elite_turbo.no_double_pv"})),
    EvalCase("Registration Team используется в Builder Bonus?", frozenset({"mwr.team_structures", "ta.builder_bonus"})),
    EvalCase("Dual Team: left и right зачем", frozenset({"mwr.team_structures"})),
    EvalCase("условия silver: left right и личные", frozenset({"ta.rank_qualification"})),
    EvalCase("Gold сколько PV нужно", frozenset({"ta.rank_qualification"})),
    EvalCase("Ruby daily limit", frozenset({"ta.dual_team_income"})),
    EvalCase("Ruby Builder Bonus сколько", frozenset({"ta.builder_bonus"})),
    EvalCase("Black Royal daily cap", frozenset({"ta.dual_team_income"})),
    EvalCase("Car Bonus 500 — это что", frozenset({"ta.car_bonus"})),
    EvalCase("Fast Start максимум", frozenset({"ta.fast_start"})),
    EvalCase("Acceleration Gold weekly", frozenset({"ta.acceleration_bonus"})),
    EvalCase("Presidential Bonus формула", frozenset({"ta.presidential_bonus"})),
    EvalCase("доход гарантирован?", frozenset({"mwr.claims_and_staleness_compliance"}), frozenset({"mwr.compliance.no_specific_income_guarantee"})),
    EvalCase("пассивный доход за неделю обещают?", frozenset({"mwr.claims_and_staleness_compliance"})),
    EvalCase("monthly Dual Team таблица — это прогноз?", frozenset({"ta.dual_team_income", "mwr.claims_and_staleness_compliance"})),
    EvalCase("какие актуальные акции сейчас", frozenset({"mwr.claims_and_staleness_compliance"}), frozenset({"ta.compliance.current_information_check"})),
    EvalCase("есть места в текущем Life Experience?", frozenset({"ta.life_experiences", "mwr.claims_and_staleness_compliance"})),
    EvalCase("цена отеля сегодня какая", frozenset({"mwr.claims_and_staleness_compliance"})),
    EvalCase("VIP180 еще актуальный?", frozenset({"mwr.claims_and_staleness_compliance"}), frozenset({"ta.membership.legacy_variants"})),
    EvalCase("какие криптовалюты принимаете сейчас", frozenset({"ta.payments_and_support_routing"})),
    EvalCase("как поставить приложение и зайти в Biz Center", frozenset({"mwr.getting_started"})),
    EvalCase("кто такой Ambassador", frozenset({"mwr.lifestyle_ambassador"})),
    EvalCase("Sponsor обязан учить новичка?", frozenset({"mwr.sponsor_training_responsibility"})),
    EvalCase("Guest Pass и крипта — расскажи оба", frozenset({"ta.guest_pass", "ta.payments_and_support_routing"})),
    EvalCase("Travel Credits передать, а points вывести можно?", frozenset({"ta.travel_credits", "ta.loyalty_points", "ta.points_transfer_and_use_delta"})),
    EvalCase("Как приготовить борщ?", expect_empty=True, forbid_compensation=True),
    EvalCase("Погода в Самаре завтра", expect_empty=True, forbid_compensation=True),
    EvalCase("Почему Python выдает TypeError", expect_empty=True, forbid_compensation=True),
    EvalCase("как заработать в интернете", expect_empty=True, forbid_compensation=True),
    EvalCase("баллы футбольного матча", expect_empty=True, forbid_compensation=True),
    EvalCase("binary tree для команды разработчиков", expect_empty=True, forbid_compensation=True),
    EvalCase("Ruby язык программирования", expect_empty=True, forbid_compensation=True),
    EvalCase("Elite Dangerous это игра?", expect_empty=True, forbid_compensation=True),
)


COMPENSATION_ITEMS = {
    "ta.acceleration_bonus", "ta.builder_bonus", "ta.car_bonus", "ta.dual_team_income",
    "ta.fast_start", "ta.member_bonus", "ta.personal_member_residual",
    "ta.presidential_bonus", "ta.rank_achievement", "ta.rank_qualification",
}


def test_adversarial_retrieval_corpus(tmp_path: Path) -> None:
    knowledge = service(tmp_path)
    statuses: list[str] = []
    for case in CASES:
        bundle = run(knowledge.retrieve(case.query))
        item_keys = keys(bundle)
        found_facts = fact_keys(bundle)
        assert case.required_items <= item_keys, case.query
        assert case.required_facts <= found_facts, case.query
        if case.expect_empty:
            assert not item_keys, case.query
            assert not found_facts, case.query
        if case.forbid_compensation:
            assert not item_keys.intersection(COMPENSATION_ITEMS), case.query
        statuses.append(case.status)

    assert len(CASES) >= 50
    assert statuses.count("FAIL") == 0


def test_false_positive_controls_are_empty(tmp_path: Path) -> None:
    knowledge = service(tmp_path)
    controls = [case for case in CASES if case.expect_empty]
    false_positives = [case.query for case in controls if keys(run(knowledge.retrieve(case.query)))]
    assert false_positives == []
