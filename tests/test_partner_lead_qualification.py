"""Partner Discovery 2.0: app.services.lead_qualification.qualify_candidates.

Pure-function unit tests, no FastAPI/web/Telegram wiring - the LLM provider
is faked via a tiny stub exposing .config.api_key and .generate(message=...)
-> object with .text, same shape app.chat_provider.OpenAIChatProvider /
app.services.partner_outreach.enrich_candidates already assume in their own
tests.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from app.services.lead_qualification import (
    _CONTACTABILITY_BY_CONFIDENCE,
    apply_fallback_qualification,
    qualify_candidates,
    resolve_additional_contacts,
    resolve_contact_channel,
)


def _run(coro):
    return asyncio.run(coro)


@dataclass
class _FakeConfig:
    api_key: str


class _FakeProvider:
    def __init__(self, response_text: str, *, api_key: str = "test-key"):
        self.config = _FakeConfig(api_key=api_key)
        self._response_text = response_text
        self.call_count = 0

    def generate(self, *, message: str):
        self.call_count += 1
        return _FakeResult(self._response_text)


@dataclass
class _FakeResult:
    text: str


def _candidate(
    *, name="Example", url="https://t.me/example_agent",
    prospect_type="travel_agent", evidence="Турагентство, авторские туры по Алтаю, набор группы в октябре.",
    evidence_source="page", rank_score=5,
):
    return {
        "name": name, "url": url, "prospect_type": prospect_type,
        "query": "турагент авторские туры", "evidence": evidence,
        "evidence_source": evidence_source, "fit": "STRONG", "rank_score": rank_score,
        "display_name": name, "reason": "Турагентство.",
    }


def _qualification_item(candidate: dict, **overrides) -> dict:
    item = {
        "professional_relevance": 25, "activity": 10, "commercial_potential": 18,
        "audience_quality": 10, "contactability": 10, "personal_hook_score": 12,
        "why_fit": ["Работает турагентством.", "Организует авторские туры."],
        "hook": "Сейчас набирает группу на Алтай.",
        "risk_flags": [],
        "confidence": 0.8,
        "evidence": [{"claim": "Авторские туры на Алтай.", "source_url": candidate["url"]}],
    }
    item.update(overrides)
    return item


# 1. Strong travel agent -> high score.
def test_strong_travel_agent_gets_high_score() -> None:
    candidate = _candidate()
    response = json.dumps([_qualification_item(candidate)])
    provider = _FakeProvider(response)

    [lead] = _run(qualify_candidates([candidate], provider, min_score=50, min_confidence=0.5))

    assert lead["total_score"] >= 80


# 2. Random traveler with just the word "travel" -> low score, filtered out.
def test_weak_random_profile_is_filtered_out() -> None:
    candidate = _candidate(
        prospect_type="other_travel_project",
        evidence="Люблю путешествия и фотографии.",
        evidence_source="snippet", rank_score=0,
    )
    item = _qualification_item(
        candidate,
        professional_relevance=0, activity=0, commercial_potential=0,
        audience_quality=0, contactability=0, personal_hook_score=0,
        why_fit=["Связь с путешествиями не подтверждена."], hook="", evidence=[],
    )
    provider = _FakeProvider(json.dumps([item]))

    leads = _run(qualify_candidates([candidate], provider, min_score=65, min_confidence=0.6))

    assert leads == []


# 3. Abandoned profile -> activity penalty (fallback path).
def test_abandoned_profile_gets_activity_penalty_in_fallback() -> None:
    candidate = _candidate(evidence_source="snippet")
    leads = apply_fallback_qualification([candidate])
    assert leads[0]["activity"] <= 5


# 4. No contact channel -> lower contactability / score in fallback.
def test_no_contact_lowers_score_in_fallback() -> None:
    with_contact = _candidate(url="https://t.me/example_agent")
    without_contact = _candidate(url="https://random-domain-xyz.ru/page")
    [a] = apply_fallback_qualification([with_contact])
    [b] = apply_fallback_qualification([without_contact])
    assert a["contactability"] >= b["contactability"]


# 5. Claim without a matching source_url is dropped, never kept.
def test_claim_without_source_url_is_dropped() -> None:
    candidate = _candidate()
    item = _qualification_item(
        candidate,
        evidence=[
            {"claim": "Подтверждённый факт.", "source_url": candidate["url"]},
            {"claim": "Непроверенный факт.", "source_url": "https://unrelated.example/"},
        ],
    )
    provider = _FakeProvider(json.dumps([item]))

    [lead] = _run(qualify_candidates([candidate], provider, min_score=0, min_confidence=0.0))

    claims = [e["claim"] for e in lead["evidence_claims"]]
    assert "Подтверждённый факт." in claims
    assert "Непроверенный факт." not in claims


# 6. Score below threshold -> lead filtered out.
def test_below_threshold_score_is_filtered() -> None:
    candidate = _candidate()
    item = _qualification_item(candidate, professional_relevance=5, commercial_potential=2)
    provider = _FakeProvider(json.dumps([item]))

    leads = _run(qualify_candidates([candidate], provider, min_score=65, min_confidence=0.0))

    assert leads == []


# 9. Malformed LLM JSON -> system falls back, never crashes.
def test_malformed_llm_json_falls_back_without_crashing() -> None:
    candidate = _candidate()
    provider = _FakeProvider("not valid json at all")

    leads = _run(qualify_candidates([candidate], provider, min_score=0, min_confidence=0.0))

    assert len(leads) == 1
    assert "total_score" in leads[0]


# 10. Empty candidate list -> correct empty result, no crash.
def test_empty_candidate_list_returns_empty() -> None:
    provider = _FakeProvider(json.dumps([]))
    leads = _run(qualify_candidates([], provider))
    assert leads == []


# 11. Only 2 strong candidates out of several -> returns exactly 2, no padding.
def test_only_strong_candidates_returned_no_padding() -> None:
    strong1 = _candidate(url="https://t.me/strong_one")
    strong2 = _candidate(url="https://t.me/strong_two")
    weak = _candidate(url="https://weak.example/", prospect_type="other_travel_project")

    def item_for(c, strong: bool):
        if strong:
            return _qualification_item(c)
        return _qualification_item(
            c, professional_relevance=0, commercial_potential=0,
            audience_quality=0, contactability=0, personal_hook_score=0,
            activity=0, why_fit=["Слабая связь."], hook="", evidence=[],
        )

    candidates = [strong1, weak, strong2]
    response = json.dumps([
        item_for(strong1, True), item_for(weak, False), item_for(strong2, True),
    ])
    provider = _FakeProvider(response)

    leads = _run(qualify_candidates(candidates, provider, min_score=65, min_confidence=0.6, max_results=7))

    assert len(leads) == 2
    assert {lead["url"] for lead in leads} == {"https://t.me/strong_one", "https://t.me/strong_two"}


# 12. Personal hook is dropped when not grounded in any accepted evidence claim.
def test_hook_dropped_without_grounding_evidence() -> None:
    candidate = _candidate()
    item = _qualification_item(candidate, hook="Выдуманный персональный повод.", evidence=[])
    provider = _FakeProvider(json.dumps([item]))

    [lead] = _run(qualify_candidates([candidate], provider, min_score=0, min_confidence=0.0))

    assert lead["hook"] == ""


# Confidence below threshold is filtered separately from score.
def test_low_confidence_is_filtered_even_with_high_score() -> None:
    candidate = _candidate()
    item = _qualification_item(candidate, confidence=0.2)
    provider = _FakeProvider(json.dumps([item]))

    leads = _run(qualify_candidates([candidate], provider, min_score=0, min_confidence=0.6))

    assert leads == []


# No LLM provider configured -> deterministic fallback used, never crashes.
def test_no_provider_uses_fallback() -> None:
    candidate = _candidate()
    leads = _run(qualify_candidates([candidate], None, min_score=0, min_confidence=0.0))
    assert len(leads) == 1
    assert "total_score" in leads[0]


# ── Contact channel resolution ───────────────────────────────────────────

# Confirmed Telegram contact is preserved and shown.
def test_confirmed_telegram_contact_is_preserved() -> None:
    candidate = _candidate(url="https://t.me/example_agent")
    [lead] = _run(qualify_candidates([candidate], None, min_score=0, min_confidence=0.0))

    assert lead["contact_type"] == "telegram"
    assert lead["contact_value"] == "@example_agent"
    assert lead["contact_source_url"] == "https://t.me/example_agent"
    assert lead["contact_confidence"] == "high"
    assert lead["contactability"] == 10


# Confirmed VK contact/URL is preserved.
def test_confirmed_vk_contact_is_preserved() -> None:
    candidate = _candidate(url="https://vk.com/example_club")
    [lead] = _run(qualify_candidates([candidate], None, min_score=0, min_confidence=0.0))

    assert lead["contact_type"] == "vk"
    assert lead["contact_url"] == "https://vk.com/example_club"
    assert lead["contact_source_url"] == "https://vk.com/example_club"
    assert lead["contact_confidence"] == "high"


# No contact -> contactability drops significantly and risk flag is added.
def test_no_contact_lowers_contactability_and_flags_risk() -> None:
    candidate = _candidate(
        url="https://travel.yandex.ru/some-listing",
        evidence="Обзор нескольких туроператоров без контактов.",
    )
    [lead] = _run(qualify_candidates([candidate], None, min_score=0, min_confidence=0.0))

    assert lead["contact_type"] is None
    assert lead["contactability"] == 0
    assert "no_public_contact" in lead["risk_flags"]


# A candidate without a contact loses to an otherwise-comparable candidate
# that has one, purely through the resulting total_score ranking.
def test_candidate_without_contact_loses_to_comparable_one_with_contact() -> None:
    with_contact = _candidate(url="https://t.me/with_contact", name="With contact")
    without_contact = _candidate(
        url="https://travel.yandex.ru/no-contact", name="Without contact",
        evidence="Обзор нескольких туроператоров без контактов.",
    )

    def item_for(c):
        return _qualification_item(c, evidence=[{"claim": "Факт.", "source_url": c["url"]}])

    response = json.dumps([item_for(with_contact), item_for(without_contact)])
    provider = _FakeProvider(response)

    leads = _run(qualify_candidates(
        [with_contact, without_contact], provider, min_score=0, min_confidence=0.0,
    ))

    leads_by_url = {lead["url"]: lead for lead in leads}
    assert leads_by_url["https://t.me/with_contact"]["total_score"] > \
        leads_by_url["https://travel.yandex.ru/no-contact"]["total_score"]
    assert leads[0]["url"] == "https://t.me/with_contact"


# An LLM-invented contact (if it ever tried to supply one) is never used -
# contact fields always come from resolve_contact_channel, never from the
# parsed LLM item.
def test_llm_cannot_invent_a_contact() -> None:
    candidate = _candidate(url="https://travel.yandex.ru/no-contact")
    item = _qualification_item(candidate)
    item["contact"] = {  # a hypothetical LLM-supplied contact - must be ignored
        "contact_type": "telegram", "contact_value": "@invented_handle",
        "contact_url": "https://t.me/invented_handle", "contact_source_url": candidate["url"],
    }
    provider = _FakeProvider(json.dumps([item]))

    [lead] = _run(qualify_candidates([candidate], provider, min_score=0, min_confidence=0.0))

    assert lead["contact_type"] is None
    assert lead.get("contact_value") != "@invented_handle"


# contact_source_url is required whenever a contact is accepted.
def test_accepted_contact_always_has_source_url() -> None:
    for candidate in (
        _candidate(url="https://t.me/example_agent"),
        _candidate(url="https://vk.com/example_club"),
        _candidate(url="https://example-agency.ru/"),
    ):
        contact = resolve_contact_channel(candidate)
        assert contact is not None
        assert contact["contact_source_url"]


def test_resolve_contact_channel_returns_none_without_any_channel() -> None:
    candidate = _candidate(
        url="https://travel.yandex.ru/some-listing",
        evidence="Нет упоминания каналов связи.",
    )
    assert resolve_contact_channel(candidate) is None


# ── Hard exclusion (2026-10-11 smoke-test finding) ───────────────────────

# An aggregator with score 90 must never pass the final filter, regardless
# of how high the score/confidence otherwise is.
def test_aggregator_with_high_score_is_hard_excluded() -> None:
    candidate = _candidate(url="https://some-aggregator-site.ru/listing")
    item = _qualification_item(
        candidate,
        professional_relevance=25, activity=15, commercial_potential=20,
        audience_quality=15, personal_hook_score=15,  # -> score 90 before contactability
        risk_flags=["aggregator"], confidence=0.9,
    )
    provider = _FakeProvider(json.dumps([item]))

    leads = _run(qualify_candidates([candidate], provider, min_score=50, min_confidence=0.5))

    assert leads == []


# A competitor_club with score 90 must never pass the final filter either.
def test_competitor_club_with_high_score_is_hard_excluded() -> None:
    candidate = _candidate(url="https://some-travel-club.ru/")
    item = _qualification_item(
        candidate,
        professional_relevance=25, activity=15, commercial_potential=20,
        audience_quality=15, personal_hook_score=15,
        risk_flags=["competitor_club"], confidence=0.9,
    )
    provider = _FakeProvider(json.dumps([item]))

    leads = _run(qualify_candidates([candidate], provider, min_score=50, min_confidence=0.5))

    assert leads == []


# A candidate flagged with BOTH competitor_club AND aggregator (the exact
# real smoke-test finding: "Клуб Гидов") must never pass either.
def test_candidate_with_both_competitor_club_and_aggregator_flags_is_excluded() -> None:
    candidate = _candidate(url="https://klub-gidov.example.ru/", name="Клуб Гидов")
    item = _qualification_item(
        candidate,
        professional_relevance=25, activity=15, commercial_potential=20,
        audience_quality=15, personal_hook_score=15,
        risk_flags=["competitor_club", "aggregator"], confidence=0.9,
    )
    provider = _FakeProvider(json.dumps([item]))

    leads = _run(qualify_candidates([candidate], provider, min_score=50, min_confidence=0.5))

    assert leads == []


# A generic website-only contact is weaker than a direct Telegram/VK/email
# channel - same other scores, contact channel is the only difference.
def test_generic_website_contact_is_weaker_than_telegram() -> None:
    telegram_candidate = _candidate(url="https://t.me/direct_handle", name="Telegram one")
    website_candidate = _candidate(url="https://example-agency.ru/", name="Website one")

    def item_for(c):
        return _qualification_item(c, evidence=[{"claim": "Факт.", "source_url": c["url"]}])

    response = json.dumps([item_for(telegram_candidate), item_for(website_candidate)])
    provider = _FakeProvider(response)

    leads = _run(qualify_candidates(
        [telegram_candidate, website_candidate], provider, min_score=0, min_confidence=0.0,
    ))

    by_url = {lead["url"]: lead for lead in leads}
    assert by_url["https://t.me/direct_handle"]["contactability"] > \
        by_url["https://example-agency.ru/"]["contactability"]
    assert by_url["https://t.me/direct_handle"]["total_score"] > \
        by_url["https://example-agency.ru/"]["total_score"]


def test_generic_website_contact_is_weaker_than_vk() -> None:
    vk_candidate = _candidate(url="https://vk.com/direct_handle", name="VK one")
    website_candidate = _candidate(url="https://example-agency.ru/", name="Website two")

    def item_for(c):
        return _qualification_item(c, evidence=[{"claim": "Факт.", "source_url": c["url"]}])

    response = json.dumps([item_for(vk_candidate), item_for(website_candidate)])
    provider = _FakeProvider(response)

    leads = _run(qualify_candidates(
        [vk_candidate, website_candidate], provider, min_score=0, min_confidence=0.0,
    ))

    by_url = {lead["url"]: lead for lead in leads}
    assert by_url["https://vk.com/direct_handle"]["contactability"] > \
        by_url["https://example-agency.ru/"]["contactability"]


def test_generic_website_contact_is_weaker_than_email() -> None:
    email_candidate = _candidate(
        url="https://some-blog.ru/about", name="Email one",
        evidence="Автор блога, связаться: ivan@some-blog.ru",
    )
    website_candidate = _candidate(url="https://example-agency.ru/", name="Website three")

    def item_for(c):
        return _qualification_item(c, evidence=[{"claim": "Факт.", "source_url": c["url"]}])

    response = json.dumps([item_for(email_candidate), item_for(website_candidate)])
    provider = _FakeProvider(response)

    leads = _run(qualify_candidates(
        [email_candidate, website_candidate], provider, min_score=0, min_confidence=0.0,
    ))

    assert leads  # both get a contact (email vs website) - a direct channel preference
    # Website root domain alone is never treated as a strong/direct channel.
    website_lead = next(lead for lead in leads if lead["url"] == "https://example-agency.ru/")
    assert website_lead["contact_confidence"] in ("low", None)
    assert website_lead["contactability"] <= _CONTACTABILITY_BY_CONFIDENCE["low"]


# Absent any qualifying lead, the result can legitimately be empty - never
# padded with a hard-excluded or below-threshold candidate just to show
# "something".
def test_no_qualifying_leads_returns_empty_result() -> None:
    weak = _candidate(url="https://travel.yandex.ru/some-listing")
    aggregator = _candidate(url="https://some-aggregator-site.ru/listing", name="Aggregator")

    def weak_item(c):
        return _qualification_item(
            c, professional_relevance=5, activity=0, commercial_potential=0,
            audience_quality=0, personal_hook_score=0, why_fit=["Слабая связь."],
            hook="", evidence=[], confidence=0.9,
        )

    def aggregator_item(c):
        return _qualification_item(
            c, professional_relevance=25, activity=15, commercial_potential=20,
            audience_quality=15, personal_hook_score=15,
            risk_flags=["aggregator"], confidence=0.9,
        )

    response = json.dumps([weak_item(weak), aggregator_item(aggregator)])
    provider = _FakeProvider(response)

    leads = _run(qualify_candidates(
        [weak, aggregator], provider, min_score=65, min_confidence=0.6,
    ))

    assert leads == []


# The system never pads the final TOP with hard-excluded candidates even
# when max_results would otherwise leave room for them.
def test_top_is_never_padded_with_hard_excluded_candidates() -> None:
    strong = _candidate(url="https://t.me/strong_one", name="Strong one")
    aggregator = _candidate(url="https://some-aggregator-site.ru/listing", name="Aggregator")

    def strong_item(c):
        return _qualification_item(c)

    def aggregator_item(c):
        return _qualification_item(
            c, professional_relevance=25, activity=15, commercial_potential=20,
            audience_quality=15, personal_hook_score=15,
            risk_flags=["aggregator"], confidence=0.9,
        )

    response = json.dumps([strong_item(strong), aggregator_item(aggregator)])
    provider = _FakeProvider(response)

    leads = _run(qualify_candidates(
        [strong, aggregator], provider, min_score=50, min_confidence=0.5, max_results=7,
    ))

    assert len(leads) == 1
    assert leads[0]["url"] == "https://t.me/strong_one"


# ── ICP calibration (2026-10-11 live findings) ───────────────────────────

def test_large_corporation_without_person_with_high_score_is_hard_excluded() -> None:
    candidate = _candidate(url="https://quinta.ru/", name="quinta.ru")
    item = _qualification_item(
        candidate,
        professional_relevance=25, activity=15, commercial_potential=20,
        audience_quality=15, personal_hook_score=15,
        risk_flags=["large_corporation_without_person"], confidence=0.9,
    )
    provider = _FakeProvider(json.dumps([item]))

    leads = _run(qualify_candidates([candidate], provider, min_score=50, min_confidence=0.5))

    assert leads == []


def test_small_person_led_travel_project_is_allowed() -> None:
    candidate = _candidate(
        url="https://t.me/author_handle", name="Автор",
        evidence="Автор авторских туров по Алтаю, веду канал, лично сопровождаю группы.",
    )
    item = _qualification_item(candidate, risk_flags=[])
    provider = _FakeProvider(json.dumps([item]))

    leads = _run(qualify_candidates([candidate], provider, min_score=50, min_confidence=0.5))

    assert len(leads) == 1
    assert leads[0]["risk_flags"] == []


def test_community_without_leader_does_not_outrank_person_led_lead() -> None:
    person_led = _candidate(url="https://t.me/person_lead", name="Person")
    community = _candidate(url="https://guild-example.ru/", name="Гильдия гидов")

    def person_item(c):
        return _qualification_item(c, professional_relevance=25, personal_hook_score=15)

    def community_item(c):
        # Same evidence tier, but no named leader - calibrated lower per
        # the ICP prompt guidance (community without an explicit leader
        # scores more conservatively than a person-led project).
        return _qualification_item(c, professional_relevance=18, personal_hook_score=10)

    response = json.dumps([person_item(person_led), community_item(community)])
    provider = _FakeProvider(response)

    leads = _run(qualify_candidates(
        [person_led, community], provider, min_score=0, min_confidence=0.0,
    ))

    by_url = {lead["url"]: lead for lead in leads}
    assert by_url["https://t.me/person_lead"]["total_score"] > \
        by_url["https://guild-example.ru/"]["total_score"]


def test_top_not_padded_with_large_corporation() -> None:
    strong = _candidate(url="https://t.me/strong_two", name="Strong two")
    corp = _candidate(url="https://quinta.ru/", name="quinta.ru")

    def corp_item(c):
        return _qualification_item(
            c, professional_relevance=25, activity=15, commercial_potential=20,
            audience_quality=15, personal_hook_score=15,
            risk_flags=["large_corporation_without_person"], confidence=0.9,
        )

    response = json.dumps([_qualification_item(strong), corp_item(corp)])
    provider = _FakeProvider(response)

    leads = _run(qualify_candidates(
        [strong, corp], provider, min_score=50, min_confidence=0.5, max_results=7,
    ))

    assert len(leads) == 1
    assert leads[0]["url"] == "https://t.me/strong_two"


# ── Direct contact extraction/normalization (2026-10-11 live Onvisor bug:
# evidence contained a Telegram mention/phone/email, but the candidate's
# own url being a plain website used to win by being checked first) ──────

def test_telegram_in_evidence_beats_website_url() -> None:
    candidate = _candidate(
        url="https://onvisor.ru/",
        evidence="Анонсы экспедиций в нашем канале Telegram t.me/onvisor_travel +7 925 750-33-44 go@trip.onvisor.ru",
    )
    contact = resolve_contact_channel(candidate)
    assert contact["contact_type"] == "telegram"
    assert contact["contact_value"] == "@onvisor_travel"


def test_email_in_evidence_ranks_above_website() -> None:
    candidate = _candidate(
        url="https://example-agency.ru/",
        evidence="Авторские туры. Пишите: info@example-agency.ru",
    )
    contact = resolve_contact_channel(candidate)
    assert contact["contact_type"] == "email"
    assert contact["contact_value"] == "info@example-agency.ru"
    assert _CONTACTABILITY_BY_CONFIDENCE[contact["contact_confidence"]] > \
        _CONTACTABILITY_BY_CONFIDENCE["low"]


def test_phone_in_evidence_beats_website() -> None:
    candidate = _candidate(
        url="https://example-agency.ru/",
        evidence="Авторские туры. Звоните: +7 925 750-33-44",
    )
    contact = resolve_contact_channel(candidate)
    assert contact["contact_type"] == "phone"
    assert contact["contact_value"]
    assert _CONTACTABILITY_BY_CONFIDENCE[contact["contact_confidence"]] > \
        _CONTACTABILITY_BY_CONFIDENCE["low"]


def test_no_direct_contact_falls_back_to_website() -> None:
    candidate = _candidate(
        url="https://example-agency.ru/",
        evidence="Авторские туры по России, групповые поездки.",
    )
    contact = resolve_contact_channel(candidate)
    assert contact["contact_type"] == "website"
    assert contact["contact_confidence"] == "low"


def test_multiple_contacts_are_deduped_and_capped() -> None:
    candidate = _candidate(
        url="https://onvisor.ru/",
        evidence=(
            "Канал Telegram t.me/onvisor_travel, пишите t.me/onvisor_travel "
            "или на go@trip.onvisor.ru, звоните +7 925 750-33-44"
        ),
    )
    primary = resolve_contact_channel(candidate)
    additional = resolve_additional_contacts(candidate, primary)

    assert primary["contact_type"] == "telegram"
    values = [primary["contact_value"]] + [c["contact_value"] for c in additional]
    assert len(values) == len(set(values))  # no duplicates
    assert len(additional) <= 2
    types = {c["contact_type"] for c in additional}
    assert "telegram" not in types  # the duplicate t.me mention is deduped out


def test_onvisor_like_fixture_is_not_website_only() -> None:
    """Regression fixture modeled on the real live Onvisor case: evidence
    names a Telegram channel, a phone, and an email, but the candidate's
    own url is a plain website - the final candidate must surface a
    direct channel, not just the generic website."""
    candidate = _candidate(
        url="https://www.onvisor.ru/", name="Onvisor",
        evidence=(
            "Авторские туры по России и миру | Onvisor Анонсы экспедиций: "
            "Курилы, Камчатка, Чукотка, ЮАР — в нашем канале Telegram "
            "t.me/onvisor_travel +7 925 750-33-44 go@trip.onvisor.ru"
        ),
    )
    contact = resolve_contact_channel(candidate)
    assert contact["contact_type"] != "website"


def test_rendering_card_shows_direct_contact_not_website() -> None:
    from app.handlers.partner_discovery import _render_card

    candidate = _candidate(
        url="https://www.onvisor.ru/", name="Onvisor",
        evidence="В нашем канале Telegram t.me/onvisor_travel go@trip.onvisor.ru",
    )
    [lead] = _run(qualify_candidates([candidate], None, min_score=0, min_confidence=0.0))
    text, _ = _render_card([lead], 0)
    assert "Telegram: @onvisor_travel" in text
    assert "Сайт: onvisor.ru" not in text.split("Telegram:")[0]


# Diagnostics: rejection/acceptance logs must name the candidate, its score
# and the thresholds actually in force (2026-10-12: web returned 0 and the
# journal could not say which candidate failed why).
def test_decision_logs_name_candidate_score_and_thresholds(caplog) -> None:
    import logging

    candidate = _candidate(url="https://rejected.example/")
    item = _qualification_item(candidate, professional_relevance=5, commercial_potential=2)
    provider = _FakeProvider(json.dumps([item]))

    with caplog.at_level(logging.INFO, logger="app.services.lead_qualification"):
        leads = _run(qualify_candidates([candidate], provider, min_score=65, min_confidence=0.0))

    assert leads == []
    text = caplog.text
    assert "thresholds min_score=65 min_confidence=0.00" in text
    assert "reason=low_score" in text and "url=https://rejected.example/" in text
