"""Partner Discovery v0 Telegram UI tests - read-only, no DB, no new search
queries beyond calling the already-tested PartnerDiscoveryService.find_candidates
(mocked here; app/services/partner_discovery.py has its own test suite)."""

from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import app.main as main_module
from app.domain.partners import WorkspaceContext
from app.domain.prospects import EvidenceSource, ProspectCandidate, ProspectFit, ProspectType
from app.handlers.partner_discovery import (
    _build_enrichment_prompt,
    _build_explanation,
    _clean_name,
    _enrich_candidates,
    _ENRICHMENT_INSTRUCTIONS,
    _rank_for_club_partner_fit,
    _render_card,
    _TA_AFFILIATED_ENRICHMENT_CLAUSE,
    on_find_partners,
    on_partner_discovery_more,
    on_partner_discovery_next,
)
from app.keyboards import BTN_FIND_PARTNERS, active_main_menu, main_menu


def run(coro):
    return asyncio.run(coro)


def context(workspace_id: int = 42, telegram_user_id: int = 100) -> WorkspaceContext:
    return WorkspaceContext(telegram_user_id, workspace_id, "owner", "active")


def profile(business_type: str = "club_partner"):
    return SimpleNamespace(business_type=business_type)


def partner_repository(business_type: str | None = "club_partner"):
    resolved = None if business_type is None else profile(business_type)
    return SimpleNamespace(get_business_profile=AsyncMock(return_value=resolved))


def candidate(name="Aventuramania", url="https://aventuramania.ru/", score=6):
    return ProspectCandidate(
        name=name,
        url=url,
        prospect_type=ProspectType.TRAVEL_AGENT,
        query="турагент авторские туры",
        evidence="Лицензированный туристический оператор, авторские туры",
        evidence_source=EvidenceSource.PAGE,
        fit=ProspectFit.STRONG,
        rank_score=score,
    )


class Message:
    def __init__(self, text: str = BTN_FIND_PARTNERS, chat_id: int = 555) -> None:
        self.text = text
        self.answers = []
        self.chat = SimpleNamespace(id=chat_id)

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        return SimpleNamespace(chat=self.chat, message_id=1)


class State:
    def __init__(self, data=None) -> None:
        self.data = data or {}

    async def get_data(self):
        return self.data

    async def update_data(self, **kwargs):
        self.data.update(kwargs)


class Callback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = Message()
        self._answers = []

    async def answer(self, text=None, **kwargs):
        self._answers.append((text, kwargs))


def web_search_service(provider) -> SimpleNamespace:
    return SimpleNamespace(provider=provider)


# ── 1-2. Button visibility (keyboards.py) ─────────────────────────────────


def test_find_partners_button_absent_by_default():
    markup = main_menu()
    texts = {btn.text for row in markup.keyboard for btn in row}
    assert BTN_FIND_PARTNERS not in texts


def test_find_partners_button_shown_when_requested():
    markup = main_menu(show_partner_discovery=True)
    texts = {btn.text for row in markup.keyboard for btn in row}
    assert BTN_FIND_PARTNERS in texts


def test_active_main_menu_passes_flag_through_for_both_menu_versions():
    for v2_enabled in (False, True):
        markup = active_main_menu(v2_enabled, show_partner_discovery=True)
        texts = {btn.text for row in markup.keyboard for btn in row}
        assert BTN_FIND_PARTNERS in texts


# ── Handler: visibility gate (defense in depth, business_type only) ───────


def test_on_find_partners_hidden_for_ordinary_travel_agent():
    message = Message()
    repo = partner_repository(business_type="travel_agent")
    run(on_find_partners(message, State(), context(), repo, web_search_service(object())))
    assert len(message.answers) == 1
    text, _ = message.answers[0]
    assert "недоступно" in text.lower()


def test_on_find_partners_hidden_without_business_profile():
    message = Message()
    repo = partner_repository(business_type=None)
    run(on_find_partners(message, State(), context(), repo, web_search_service(object())))
    text, _ = message.answers[0]
    assert "недоступно" in text.lower()


def test_on_find_partners_requires_workspace():
    message = Message()
    repo = partner_repository()
    run(on_find_partners(message, State(), None, repo, web_search_service(object())))
    text, _ = message.answers[0]
    assert "недоступно" in text.lower()


# ── Handler: returns cards for club_partner ────────────────────────────────


def test_on_find_partners_returns_card_for_club_partner():
    message = Message()
    state = State()
    repo = partner_repository(business_type="club_partner")
    candidates = [candidate("A", "https://a.ru/"), candidate("B", "https://b.ru/")]

    with patch(
        "app.handlers.partner_discovery.PartnerDiscoveryService.find_candidates",
        AsyncMock(return_value=candidates),
    ):
        run(on_find_partners(message, state, context(), repo, web_search_service(object())))

    assert len(message.answers) == 1
    text, kwargs = message.answers[0]
    assert "1/2" in text
    assert "A" in text
    assert "https://a.ru/" in text
    assert state.data["partner_discovery_index"] == 0
    assert len(state.data["partner_discovery_candidates"]) == 2


# ── "Следующий" walks the batch, never calls search again ─────────────────


def test_next_shows_second_candidate_without_new_search():
    candidates_data = [
        {
            "name": "A", "url": "https://a.ru/", "prospect_type": "travel_agent",
            "query": "q", "evidence": "ev", "evidence_source": "page", "fit": "STRONG",
        },
        {
            "name": "B", "url": "https://b.ru/", "prospect_type": "guide",
            "query": "q", "evidence": "ev2", "evidence_source": "snippet", "fit": "STRONG",
        },
    ]
    state = State({"partner_discovery_candidates": candidates_data, "partner_discovery_index": 0})
    callback = Callback("partner_discovery:next")

    with patch(
        "app.handlers.partner_discovery.PartnerDiscoveryService.find_candidates",
        AsyncMock(side_effect=AssertionError("must not search again")),
    ):
        run(on_partner_discovery_next(callback, state))

    assert len(callback.message.answers) == 1
    text, _ = callback.message.answers[0]
    assert "2/2" in text
    assert "B" in text
    assert state.data["partner_discovery_index"] == 1


def test_next_after_last_candidate_shows_batch_exhausted():
    candidates_data = [{
        "name": "A", "url": "https://a.ru/", "prospect_type": "travel_agent",
        "query": "q", "evidence": "ev", "evidence_source": "page", "fit": "STRONG",
    }]
    state = State({"partner_discovery_candidates": candidates_data, "partner_discovery_index": 0})
    callback = Callback("partner_discovery:next")

    run(on_partner_discovery_next(callback, state))

    text, _ = callback.message.answers[0]
    assert "все найденные кандидаты" in text.lower()


# ── 0-results and provider-error cases ─────────────────────────────────────


def test_on_find_partners_zero_results_shows_plain_message():
    message = Message()
    repo = partner_repository()

    with patch(
        "app.handlers.partner_discovery.PartnerDiscoveryService.find_candidates",
        AsyncMock(return_value=[]),
    ):
        run(on_find_partners(message, State(), context(), repo, web_search_service(object())))

    text, _ = message.answers[0]
    assert "traceback" not in text.lower()
    assert "error" not in text.lower()
    assert "не нашлось" in text.lower()


def test_on_find_partners_provider_error_shows_friendly_message():
    message = Message()
    repo = partner_repository()

    with patch(
        "app.handlers.partner_discovery.PartnerDiscoveryService.find_candidates",
        AsyncMock(side_effect=RuntimeError("yandex api down")),
    ):
        run(on_find_partners(message, State(), context(), repo, web_search_service(object())))

    text, _ = message.answers[0]
    assert text == "Не удалось выполнить поиск сейчас. Попробуйте немного позже."


def test_on_find_partners_no_provider_configured_shows_friendly_message():
    message = Message()
    repo = partner_repository()
    run(on_find_partners(message, State(), context(), repo, web_search_service(None)))
    text, _ = message.answers[0]
    assert text == "Не удалось выполнить поиск сейчас. Попробуйте немного позже."


# ── "Найти ещё" explicitly starts a new search ─────────────────────────────


def test_find_more_runs_a_new_search():
    state = State()
    callback = Callback("partner_discovery:more")
    candidates = [candidate("Fresh", "https://fresh.ru/")]

    with patch(
        "app.handlers.partner_discovery.PartnerDiscoveryService.find_candidates",
        AsyncMock(return_value=candidates),
    ) as mocked:
        run(on_partner_discovery_more(callback, state, web_search_service(object())))

    mocked.assert_awaited_once()
    text, _ = callback.message.answers[0]
    assert "Fresh" in text
    assert state.data["partner_discovery_index"] == 0


# ── Card presentation fix: name cleanup, "Соответствие:", explanation ──────


def test_clean_name_picks_brand_when_tagline_comes_first():
    assert _clean_name("Авентурамания - туроператор", "https://aventuramania.ru/") == "Авентурамания"


def test_clean_name_picks_brand_when_tagline_comes_second():
    assert _clean_name(
        "Гильдия гидов - Магазин Путешествий",
        "https://magput.ru/tours/gildiya-gidov-magput",
    ) == "Магазин Путешествий"


def test_clean_name_picks_brand_out_of_noisy_vk_title():
    raw = (
        "Duh Travel. Авторские путешествия | Мы в реестре ТУРОПЕРАТОРОВ 2989"
        "ДУХ ПРИКЛЮЧЕНИЙ – это команда | 2026 | ВКонтакте"
    )
    assert _clean_name(raw, "https://vk.ru/duh_travel") == "Duh Travel"


def test_clean_name_passes_through_already_clean_brand():
    assert _clean_name("In Russia Today", "https://inrussiatoday.ru/") == "In Russia Today"


def test_clean_name_falls_back_to_domain_when_name_is_a_url():
    assert _clean_name("https://example.ru/page", "https://example.ru/page") == "example.ru"


def test_card_first_line_is_name_not_url():
    candidates = [{
        "name": "Авторские туры - Магазин Путешествий",
        "url": "https://magput.ru/tours/avtorskie-tury-magput",
        "prospect_type": "travel_agent",
        "query": "q", "evidence": "Авторские туры по России", "evidence_source": "snippet",
        "fit": "STRONG",
    }]
    text, _ = _render_card(candidates, 0)
    first_content_line = text.split("\n")[2]
    assert first_content_line == "Магазин Путешествий"
    assert "https://" not in first_content_line


def test_fit_strong_rendered_as_vysokoe_internal_enum_unchanged():
    candidates = [{
        "name": "A", "url": "https://a.ru/", "prospect_type": "travel_agent",
        "query": "q", "evidence": "ev", "evidence_source": "page", "fit": "STRONG",
    }]
    text, _ = _render_card(candidates, 0)
    assert "Соответствие: высокое" in text
    assert "Fit:" not in text
    assert "STRONG" not in text
    # The stored dict itself still carries the raw enum value, unchanged.
    assert candidates[0]["fit"] == "STRONG"


def test_fit_possible_rendered_as_vozmozhnoe():
    candidates = [{
        "name": "A", "url": "https://a.ru/", "prospect_type": "other_travel_project",
        "query": "q", "evidence": "ev", "evidence_source": "snippet", "fit": "POSSIBLE",
    }]
    text, _ = _render_card(candidates, 0)
    assert "Соответствие: возможное" in text


def test_explanation_strips_phone_menu_and_rassrochka_noise():
    noisy_evidence = (
        "Календарь путешествий Зарубежные туры и круизы Корпоративные "
        "путешествия Отзывы Контакты Платформа для турагентов МЕНЮ 4.9 "
        "звонить писать +7 (495) 150-19-99 Возможности гибкой оплаты от "
        "Сбербанка Комфортными платежами (кредит) В рассрочку до 4 месяцев"
    )
    explanation = _build_explanation("travel_agent", noisy_evidence)
    assert "+7" not in explanation
    assert "495" not in explanation
    assert "рассрочк" not in explanation.lower()
    assert "кредит" not in explanation.lower()
    assert "отзывы" not in explanation.lower()
    assert "контакты" not in explanation.lower()
    assert "меню" not in explanation.lower()


def test_explanation_strips_email():
    explanation = _build_explanation(
        "guide", "Пишите нам: info@example.ru для записи на экскурсии",
    )
    assert "@" not in explanation


def test_explanation_from_snippet_evidence_is_used_directly():
    explanation = _build_explanation(
        "travel_club",
        "Клуб путешественников с 20-летней историей, более 30 собственных гидов.",
    )
    assert "20-летней историей" in explanation


def test_explanation_falls_back_to_type_sentence_when_evidence_is_unusable():
    explanation = _build_explanation("travel_agent", "")
    assert explanation == "Работает как турагентство/туроператор."


def test_explanation_never_claims_partnership_or_income_interest():
    candidates_evidence = [
        ("travel_agent", "Лицензированный туристический оператор, авторские туры"),
        ("guide", "Гиды и экскурсоводы, более 100 туров проведено"),
        ("travel_creator", "Блог о путешествиях, 230+ туров"),
    ]
    forbidden = ("партнёрств", "партнерств", "доход", "выгодн", "travel advantage", "заработ")
    for prospect_type, evidence in candidates_evidence:
        explanation = _build_explanation(prospect_type, evidence)
        lowered = explanation.lower()
        for word in forbidden:
            assert word not in lowered


def test_explanation_at_most_two_sentences():
    explanation = _build_explanation(
        "tour_organizer",
        "Организуем авторские туры по России и за рубежом для небольших групп.",
    )
    sentence_count = explanation.count(". ") + (1 if explanation.endswith(".") else 0)
    assert sentence_count <= 2


# ── LLM enrichment: one call per batch, display_name/reason, safe fallback ──


def raw_candidate(
    name="Авторские туры - Магазин Путешествий",
    url="https://magput.ru/tours/avtorskie-tury-magput",
    prospect_type="travel_agent",
    evidence="Авторские туры по России, Турфирмам: +7 (495) 215-20-22",
    evidence_source="snippet",
    fit="STRONG",
):
    return {
        "name": name, "url": url, "prospect_type": prospect_type,
        "query": "q", "evidence": evidence, "evidence_source": evidence_source, "fit": fit,
    }


class FakeEnrichmentProvider:
    """Stands in for app.chat_provider.OpenAIChatProvider: same .generate()
    and .config.api_key surface, no real network call."""

    def __init__(self, response_text: str | None = None, raises: Exception | None = None,
                 api_key: str = "sk-test"):
        self._response_text = response_text
        self._raises = raises
        self.config = SimpleNamespace(api_key=api_key)
        self.calls = 0

    def generate(self, *, message: str, **kwargs):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return SimpleNamespace(text=self._response_text)


def test_enrichment_calls_llm_exactly_once_for_a_five_candidate_batch():
    candidates = [raw_candidate(url=f"https://x{i}.ru/") for i in range(5)]
    response = json.dumps([
        {"display_name": f"Brand {i}", "reason": f"Делает travel-вещи номер {i}."}
        for i in range(5)
    ])
    provider = FakeEnrichmentProvider(response_text=response)

    enriched = run(_enrich_candidates(candidates, provider))

    assert provider.calls == 1
    assert len(enriched) == 5
    assert enriched[3]["display_name"] == "Brand 3"


def test_enrichment_uses_llm_display_name_and_reason():
    candidates = [raw_candidate()]
    response = json.dumps([
        {"display_name": "Магазин Путешествий", "reason": "Организует авторские туры по России."},
    ])
    provider = FakeEnrichmentProvider(response_text=response)

    enriched = run(_enrich_candidates(candidates, provider))

    assert enriched[0]["display_name"] == "Магазин Путешествий"
    assert enriched[0]["reason"] == "Организует авторские туры по России."


def test_enrichment_reason_is_concise():
    candidates = [raw_candidate()]
    response = json.dumps([
        {"display_name": "Магазин Путешествий", "reason": "Организует авторские туры."},
    ])
    provider = FakeEnrichmentProvider(response_text=response)

    enriched = run(_enrich_candidates(candidates, provider))

    assert len(enriched[0]["reason"]) < 200


def test_card_does_not_show_internal_type_label():
    candidates = [{**raw_candidate(), "display_name": "Магазин Путешествий", "reason": "Факт."}]
    text, _ = _render_card(candidates, 0)
    assert "Тип:" not in text
    # internal prospect_type is untouched in the stored dict
    assert candidates[0]["prospect_type"] == "travel_agent"


def test_invalid_json_falls_back_safely():
    candidates = [raw_candidate()]
    provider = FakeEnrichmentProvider(response_text="this is not json at all")

    enriched = run(_enrich_candidates(candidates, provider))

    assert enriched[0]["display_name"] == "Магазин Путешествий"
    assert "+7" not in enriched[0]["reason"]


def test_wrong_item_count_falls_back_safely():
    candidates = [raw_candidate(), raw_candidate(url="https://other.ru/")]
    response = json.dumps([{"display_name": "Only One", "reason": "Reason."}])
    provider = FakeEnrichmentProvider(response_text=response)

    enriched = run(_enrich_candidates(candidates, provider))

    assert len(enriched) == 2
    assert enriched[0]["display_name"] == "Магазин Путешествий"
    assert enriched[1]["display_name"] != "Only One"


def test_provider_exception_falls_back_safely():
    candidates = [raw_candidate()]
    provider = FakeEnrichmentProvider(raises=RuntimeError("network down"))

    enriched = run(_enrich_candidates(candidates, provider))

    assert enriched[0]["display_name"] == "Магазин Путешествий"
    assert enriched[0]["reason"]


def test_no_provider_configured_falls_back_safely():
    candidates = [raw_candidate()]
    enriched = run(_enrich_candidates(candidates, None))
    assert enriched[0]["display_name"] == "Магазин Путешествий"


def test_fallback_never_contains_raw_phone_email_or_menu_fragments():
    noisy = raw_candidate(
        evidence=(
            "Меню Контакты Отзывы +7 (495) 150-19-99 info@example.ru "
            "Рассрочка 4 месяца Вход в ЛК Организуем авторские туры по России"
        ),
    )
    enriched = run(_enrich_candidates([noisy], None))
    reason = enriched[0]["reason"].lower()
    assert "+7" not in reason
    assert "@" not in reason
    assert "меню" not in reason
    assert "рассрочк" not in reason
    assert "вход в лк" not in reason


def test_malformed_item_missing_fields_falls_back_for_whole_batch():
    candidates = [raw_candidate(), raw_candidate(url="https://other.ru/")]
    response = json.dumps([
        {"display_name": "Brand A", "reason": "Reason A."},
        {"display_name": "", "reason": "Reason B."},
    ])
    provider = FakeEnrichmentProvider(response_text=response)

    enriched = run(_enrich_candidates(candidates, provider))

    assert enriched[0]["display_name"] != "Brand A"


# ── Regression: production incident - DI name mismatch silently disabled
# enrichment (handler param was "enrichment_llm_provider", dispatcher key
# was "partner_enrichment_llm_provider" - aiogram injects by exact name
# match, so the handler always got its own default of None and silently
# fell back, with no log anywhere explaining why). ────────────────────────


def test_handler_param_name_matches_dispatcher_injection_key():
    source = inspect.getsource(main_module)
    assert 'dp["partner_enrichment_llm_provider"]' in source

    find_partners_params = inspect.signature(on_find_partners).parameters
    assert "partner_enrichment_llm_provider" in find_partners_params
    assert "enrichment_llm_provider" not in find_partners_params

    find_more_params = inspect.signature(on_partner_discovery_more).parameters
    assert "partner_enrichment_llm_provider" in find_more_params
    assert "enrichment_llm_provider" not in find_more_params


def test_on_find_partners_actually_uses_injected_provider_by_keyword():
    """Calls the handler the way aiogram's DI actually does - by keyword,
    using the dispatcher's own key name - rather than positionally with a
    hand-picked variable name. This is the exact gap that let the
    production name-mismatch regress silently: every prior test called the
    handler with a Python variable the AUTHOR named correctly, never with
    the real dispatcher key string."""
    message = Message()
    state = State()
    repo = partner_repository(business_type="club_partner")
    response = json.dumps([{"display_name": "Авентурамания", "reason": "Организует туры."}])
    provider = FakeEnrichmentProvider(response_text=response)

    with patch(
        "app.handlers.partner_discovery.PartnerDiscoveryService.find_candidates",
        AsyncMock(return_value=[candidate("Aventuramania", "https://aventuramania.ru/")]),
    ):
        run(on_find_partners(**{
            "message": message,
            "state": state,
            "workspace_context": context(),
            "partner_repository": repo,
            "web_search_service": web_search_service(object()),
            "partner_enrichment_llm_provider": provider,
        }))

    assert provider.calls == 1
    text, _ = message.answers[0]
    assert "Авентурамания" in text


# ── display_name enrichment fix: extract brand from title/evidence, never
# default to hostname when a real name is present (live Telegram defect:
# aventuramania.ru/inrussiatoday.ru shown instead of the actual brand). ────


def test_prompt_includes_title_for_each_candidate():
    candidates = [raw_candidate(name="Авентурамания - туроператор", url="https://aventuramania.ru/")]
    prompt = _build_enrichment_prompt(candidates)
    assert "TITLE" in prompt
    assert "Авентурамания - туроператор" in prompt
    assert "EVIDENCE" in prompt


def test_prompt_instructs_title_priority_over_hostname():
    assert "TITLE" in _ENRICHMENT_INSTRUCTIONS
    assert "hostname" in _ENRICHMENT_INSTRUCTIONS
    assert "крайний случай" in _ENRICHMENT_INSTRUCTIONS
    # the exact two live-defect brands are called out as worked examples
    assert "Авентурамания" in _ENRICHMENT_INSTRUCTIONS
    assert "In Russia Today" in _ENRICHMENT_INSTRUCTIONS
    assert "aventuramania.ru" in _ENRICHMENT_INSTRUCTIONS  # named as the WRONG answer


def test_prompt_forbids_inventing_a_name_not_in_title_or_evidence():
    assert "Ничего не придумывай" in _ENRICHMENT_INSTRUCTIONS


def test_enrichment_uses_brand_from_title_aventuramania():
    candidates = [raw_candidate(
        name="Авентурамания - туроператор", url="https://aventuramania.ru/",
        evidence="Лицензированный туристический оператор, авторские туры",
    )]
    response = json.dumps([
        {"display_name": "Авентурамания", "reason": "Лицензированный туроператор, авторские туры."},
    ])
    provider = FakeEnrichmentProvider(response_text=response)

    enriched = run(_enrich_candidates(candidates, provider))

    assert enriched[0]["display_name"] == "Авентурамания"
    assert enriched[0]["display_name"] != "aventuramania.ru"


def test_enrichment_uses_brand_from_title_in_russia_today():
    candidates = [raw_candidate(
        name="In Russia Today - туры по России", url="https://inrussiatoday.ru/",
        evidence="Туры по России, групповые и индивидуальные, команда гидов",
    )]
    response = json.dumps([
        {"display_name": "In Russia Today", "reason": "Организует туры по России для групп и индивидуально."},
    ])
    provider = FakeEnrichmentProvider(response_text=response)

    enriched = run(_enrich_candidates(candidates, provider))

    assert enriched[0]["display_name"] == "In Russia Today"
    assert enriched[0]["display_name"] != "inrussiatoday.ru"


def test_enrichment_falls_back_to_hostname_when_no_brand_in_title_or_evidence():
    candidates = [raw_candidate(
        name="", url="https://example-travel.ru/page",
        evidence="Описание страницы без явного названия проекта или бренда",
    )]
    response = json.dumps([
        {"display_name": "example-travel.ru", "reason": "Travel-проект без явного публичного названия."},
    ])
    provider = FakeEnrichmentProvider(response_text=response)

    enriched = run(_enrich_candidates(candidates, provider))

    assert enriched[0]["display_name"] == "example-travel.ru"


# ── club_partner fit rationale + demotion (2026-10-07) ──────────────────────

def test_personalized_type_fallback_explanation_states_potential_fit_signal():
    """guide/travel_creator/travel_club get an explicit WHY-potentially-fit
    rationale (own audience/community) - never a claim of existing interest."""
    for prospect_type in ("guide", "travel_creator", "travel_club"):
        explanation = _build_explanation(prospect_type, "")
        lowered = explanation.lower()
        assert "аудитор" in lowered or "сообщество" in lowered or "конкретных людей" in lowered


def test_generic_business_type_fallback_explanation_unchanged():
    """travel_agent/tour_organizer/other stay exactly as before - this task
    ranks them below personalized candidates, it does not invent a false
    personal-fit claim for them."""
    assert _build_explanation("travel_agent", "") == "Работает как турагентство/туроператор."


def test_fallback_explanations_still_never_claim_partnership_or_income_interest():
    forbidden = ("партнёрств", "партнерств", "доход", "выгодн", "travel advantage", "заработ", "заинтересован")
    for prospect_type in ("guide", "travel_creator", "travel_club", "travel_agent", "tour_organizer"):
        lowered = _build_explanation(prospect_type, "").lower()
        for word in forbidden:
            assert word not in lowered


def test_rank_for_club_partner_fit_demotes_generic_business_below_personalized():
    candidates = [
        {"prospect_type": "travel_agent", "name": "Quinta"},
        {"prospect_type": "travel_club", "name": "Magput"},
        {"prospect_type": "other_travel_project", "name": "SomeSite"},
        {"prospect_type": "guide", "name": "SoloGuide"},
    ]
    ranked = _rank_for_club_partner_fit(candidates)
    assert [c["name"] for c in ranked] == ["Magput", "SoloGuide", "Quinta", "SomeSite"]


def test_rank_for_club_partner_fit_is_stable_within_each_tier():
    candidates = [
        {"prospect_type": "travel_agent", "name": "A"},
        {"prospect_type": "guide", "name": "B"},
        {"prospect_type": "travel_agent", "name": "C"},
        {"prospect_type": "guide", "name": "D"},
    ]
    ranked = _rank_for_club_partner_fit(candidates)
    assert [c["name"] for c in ranked] == ["B", "D", "A", "C"]


def test_enrichment_prompt_includes_ta_clause_only_when_ta_affiliated():
    """The base prompt already names "Travel Advantage" once, in its own
    forbidden-claims list (-> utterать, что ему нужен Travel Advantage) -
    that bare substring check isn't a valid signal. What must differ is the
    dedicated TA-affiliated clause itself."""
    candidates = [raw_candidate()]
    prompt_with_ta = _build_enrichment_prompt(candidates, ta_affiliated=True)
    prompt_without_ta = _build_enrichment_prompt(candidates, ta_affiliated=False)
    assert _TA_AFFILIATED_ENRICHMENT_CLAUSE.strip() in prompt_with_ta
    assert _TA_AFFILIATED_ENRICHMENT_CLAUSE.strip() not in prompt_without_ta


def test_find_partners_passes_ta_affiliated_profile_into_enrichment_prompt():
    """End-to-end: a club_partner workspace that is ALSO ta_affiliated gets
    the TA-specific enrichment clause in the actual LLM prompt sent."""
    message = Message()
    state = State()
    ws_context = context()
    repo_profile = SimpleNamespace(business_type="club_partner", ta_affiliated=True)
    repo = SimpleNamespace(get_business_profile=AsyncMock(return_value=repo_profile))

    captured = {}

    class CapturingProvider(FakeEnrichmentProvider):
        def generate(self, *, message: str, **kwargs):
            captured["prompt"] = message
            return super().generate(message=message, **kwargs)

    response = json.dumps([{"display_name": "Brand", "reason": "Работает с путешественниками."}])
    provider = CapturingProvider(response_text=response)
    service_candidates = [candidate()]

    with patch(
        "app.handlers.partner_discovery.PartnerDiscoveryService.find_candidates",
        AsyncMock(return_value=service_candidates),
    ):
        run(on_find_partners(
            message, state, ws_context, repo,
            web_search_service(object()), provider,
        ))

    assert provider.calls == 1
    assert _TA_AFFILIATED_ENRICHMENT_CLAUSE.strip() in captured["prompt"]


def test_find_partners_without_ta_affiliation_omits_ta_clause():
    message = Message()
    state = State()
    ws_context = context()
    repo_profile = SimpleNamespace(business_type="club_partner", ta_affiliated=False)
    repo = SimpleNamespace(get_business_profile=AsyncMock(return_value=repo_profile))

    captured = {}

    class CapturingProvider(FakeEnrichmentProvider):
        def generate(self, *, message: str, **kwargs):
            captured["prompt"] = message
            return super().generate(message=message, **kwargs)

    response = json.dumps([{"display_name": "Brand", "reason": "Работает с путешественниками."}])
    provider = CapturingProvider(response_text=response)
    service_candidates = [candidate()]

    with patch(
        "app.handlers.partner_discovery.PartnerDiscoveryService.find_candidates",
        AsyncMock(return_value=service_candidates),
    ):
        run(on_find_partners(
            message, state, ws_context, repo,
            web_search_service(object()), provider,
        ))

    assert _TA_AFFILIATED_ENRICHMENT_CLAUSE.strip() not in captured["prompt"]
