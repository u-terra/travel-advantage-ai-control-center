from __future__ import annotations

from types import MappingProxyType

from app.domain.business_profiles import BusinessClaim, BusinessContext, BusinessProfile
from app.orchestration.context import ConversationTurn
from app.orchestration.request import SYSTEM_ROUTING_RULES, build_orchestration_request
from app.routing.modules import MODULE_DESCRIPTION, Module


def _profile(*, ta_affiliated=False) -> BusinessProfile:
    context = BusinessContext(
        specializations=("Круизы",), destinations=("Италия",),
        audiences=("Семьи",), markets=("RU",),
        positioning=MappingProxyType({
            "statement": "Персональный подбор туров без переплат",
            "value_proposition": "value", "differentiators": (),
        }),
        communication=MappingProxyType({"tone": "Тёплый", "style": "", "preferred_terms": (), "banned_formulations": ()}),
        goals=("Заявки",),
        content_preferences=MappingProxyType({"formats": ("post",), "channels": (), "topics": ()}),
        public_contacts=MappingProxyType({"website": "https://example.com"}),
        claims=(BusinessClaim("Проверенный факт", "verified", "evidence", "now", "now"),),
    )
    return BusinessProfile(
        1, 10, "Мой travel-бизнес", "agency", "desc", "usable", 1, 3,
        context, "now", "now", ta_affiliated=ta_affiliated,
    )


def test_splits_leading_instruction_from_pasted_material():
    text = "Перепиши этот текст: Пришла повестка. Нужно проверить ограничения."
    request = build_orchestration_request(text)
    assert request.user_instruction == "Перепиши этот текст"
    assert request.pasted_material == "Пришла повестка. Нужно проверить ограничения."
    # The instruction section must not silently absorb the quoted material.
    assert "повестка" not in request.user_instruction


def test_short_text_with_no_separator_is_treated_as_a_bare_instruction():
    """No ":"/newline and short (e.g. "Напиши пост про Travel Advantage") -
    a genuine direct command, not quoted material to file away as data."""
    text = "Напиши пост про Travel Advantage"
    request = build_orchestration_request(text)
    assert request.user_instruction == text
    assert request.pasted_material == ""


def test_long_text_with_no_separator_is_treated_as_bare_pasted_material():
    """No ":"/newline but long (case E: a pasted post with no command) -
    a source-analysis candidate, not something to read as an instruction."""
    text = (
        "Хотим поделиться свежим кейсом клиента. Семья из Москвы слетала в "
        "Анталию на десять дней и нашла отель через наш сервис. Итоговая "
        "стоимость проживания оказалась заметно меньше, чем на популярных "
        "туристических сайтах, а трансфер получилось согласовать отдельно "
        "и тоже дешевле обычного. Делимся деталями, чтобы показать, как "
        "сравнение предложений помогает сэкономить при планировании "
        "поездки заранее и без лишних сложностей для всей семьи в дороге."
    )
    assert len(text) >= 400
    request = build_orchestration_request(text)
    assert request.pasted_material == text
    assert request.user_instruction == ""


def test_conversation_turns_split_by_role():
    turns = (
        ConversationTurn(role="user", text="Найди сигналы про Турцию"),
        ConversationTurn(role="assistant", text="Radar предложил пост-идею: X", module="AI Lead Radar"),
        ConversationTurn(role="user", text="А почему именно эта идея?"),
    )
    request = build_orchestration_request("текущий вопрос", turns=turns)
    assert request.past_conversation == ("Найди сигналы про Турцию", "А почему именно эта идея?")
    assert request.past_assistant_result == ("[AI Lead Radar] Radar предложил пост-идею: X",)


def test_no_turns_gives_empty_conversation_sections():
    request = build_orchestration_request("что угодно")
    assert request.past_conversation == ()
    assert request.past_assistant_result == ()


def test_business_context_is_compact_not_full_profile():
    request = build_orchestration_request("пост", business_profile=_profile(ta_affiliated=True))
    assert request.context_data["business_type"] == "agency"
    assert request.context_data["ta_affiliated"] == "true"
    assert "Персональный подбор" in request.context_data["positioning"]
    # Not leaked: full profile internals that are irrelevant to intent
    # classification (claims, contacts, tone dict, etc.).
    assert "claims" not in request.context_data
    assert "public_contacts" not in request.context_data
    assert len(request.context_data) <= 5


def test_missing_business_profile_gives_empty_context_data():
    request = build_orchestration_request("пост", business_profile=None)
    assert request.context_data == {}


def test_module_catalog_reuses_existing_module_descriptions():
    request = build_orchestration_request("пост")
    assert request.module_catalog == {m.value: text for m, text in MODULE_DESCRIPTION.items()}
    assert Module.CONTENT_FACTORY.value in request.module_catalog


def test_system_rules_are_short_and_not_a_keyword_dictionary():
    request = build_orchestration_request("пост")
    assert request.system_rules == SYSTEM_ROUTING_RULES
    # A handful of fixed rules, not hundreds of keywords.
    assert 1 <= len(request.system_rules) <= 15
    for rule in request.system_rules:
        assert isinstance(rule, str) and rule.strip()


def test_system_rules_flag_income_promises_regardless_of_module():
    """Live shadow testing found the model setting safety_required=false on
    rewrite/create_content requests that contained an income guarantee
    inside the pasted material - the action verb alone was treated as
    sufficient signal. This rule makes the risky-content signal explicit."""
    matching = [r for r in SYSTEM_ROUTING_RULES if "доход" in r or "заработ" in r]
    assert matching, "no rule covers income/profit promises"
    assert any("safety_required=true" in r for r in matching)
    assert any("regardless of primary_module or intent" in r for r in matching)


def test_system_rules_flag_competitor_price_comparisons_regardless_of_module():
    matching = [r for r in SYSTEM_ROUTING_RULES if "Booking" in r or "Airbnb" in r]
    assert matching, "no rule covers competitor/price comparisons"
    assert any("safety_required=true" in r for r in matching)
    assert any("regardless of primary_module or intent" in r for r in matching)


def test_system_rules_flag_guaranteed_outcome_claims_regardless_of_module():
    matching = [r for r in SYSTEM_ROUTING_RULES if "guaranteed" in r.lower()]
    assert matching, "no rule covers guaranteed-outcome claims"
    assert any("safety_required=true" in r for r in matching)
    assert any("regardless of primary_module or intent" in r for r in matching)


def test_fsm_state_is_passed_through():
    request = build_orchestration_request("пост", fsm_state="TextReview:waiting_for_text")
    assert request.fsm_state == "TextReview:waiting_for_text"


def test_fsm_state_defaults_to_none():
    request = build_orchestration_request("пост")
    assert request.fsm_state is None
