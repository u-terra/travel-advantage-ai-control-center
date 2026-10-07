"""Partner Discovery v0 + Contact Discovery v0 Telegram UI integration -
read-only layer on top of the same FSM batch, no DB, no new Partner
Discovery search. app/services/contact_discovery.py itself is mocked here
(it has its own focused test suite in tests/test_contact_discovery.py)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.domain.contacts import ContactCandidate, ContactConfidence, ContactType
from app.domain.partners import WorkspaceContext
from app.handlers.partner_discovery import (
    _looks_like_draft_command,
    _looks_like_invitation_link_command,
    on_draft_command_text,
    on_invitation_link_command_text,
    on_partner_contact_back,
    on_partner_contact_draft,
    on_partner_contact_find,
)
from app.keyboards import (
    PARTNER_CONTACT_BACK,
    PARTNER_CONTACT_DRAFT,
    PARTNER_CONTACT_FIND,
    partner_contact_card_keyboard,
    partner_discovery_card_keyboard,
)


def run(coro):
    return asyncio.run(coro)


def _candidate_dict(name="Example Club", url="https://example-club.ru/", display_name=None):
    candidate = {
        "name": name, "url": url, "prospect_type": "travel_club",
        "query": "клуб путешественников", "evidence": "ev", "evidence_source": "page",
        "fit": "STRONG",
    }
    if display_name is not None:
        candidate["display_name"] = display_name
    return candidate


class Message:
    def __init__(self) -> None:
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        return SimpleNamespace(message_id=1)


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

    async def answer(self, text=None, **kwargs):
        pass


def _person_contact(confidence: ContactConfidence) -> ContactCandidate:
    return ContactCandidate(
        contact_type=ContactType.PERSON,
        display_name="Иван Иванов",
        role="основатель",
        project_name="Example Club",
        public_url="https://example-club.ru/",
        evidence_text="Иван Иванов - основатель Example Club.",
        evidence_url="https://example-club.ru/about",
        confidence=confidence,
        provenance="official_page",
    )


def _project_contact() -> ContactCandidate:
    return ContactCandidate(
        contact_type=ContactType.PROJECT,
        display_name="Example Club",
        project_name="Example Club",
        public_url="https://example-club.ru/",
        evidence_text="",
        evidence_url="https://example-club.ru/",
        provenance="person_not_found",
    )


# ── 1. Button is present on the prospect card ───────────────────────────────

def test_contact_find_button_present_on_prospect_card():
    markup = partner_discovery_card_keyboard("https://a.ru/", has_more_in_batch=True)
    callback_datas = {btn.callback_data for row in markup.inline_keyboard for btn in row if btn.callback_data}
    assert PARTNER_CONTACT_FIND in callback_datas


# ── 2. Handler uses the current prospect from FSM state ────────────────────

def test_handler_passes_current_prospect_to_find_contact():
    candidates_data = [_candidate_dict("A", "https://a.ru/"), _candidate_dict("B", "https://b.ru/")]
    state = State({"partner_discovery_candidates": candidates_data, "partner_discovery_index": 1})
    callback = Callback(PARTNER_CONTACT_FIND)

    captured = {}

    async def _fake_find_contact(prospect, *, llm_provider, web_search_provider):
        captured["prospect"] = prospect
        return _project_contact()

    with patch("app.handlers.partner_discovery.find_contact", _fake_find_contact):
        run(on_partner_contact_find(callback, state))

    assert captured["prospect"].name == "B"
    assert captured["prospect"].url == "https://b.ru/"


# ── 2b. UX fix (2026-10-07): prospect passed to find_contact uses the ──────
# already-enriched display_name, not the raw/possibly-URL-ish name ─────────

def test_handler_passes_enriched_display_name_as_prospect_name():
    """Real production bug: ProspectCandidate.name is the raw search
    result title (occasionally the bare URL when a result had no real
    title) - the PERSON card's "Проект:" line must show the pretty,
    already-enriched name ("Гильдия гидов Magput") shown everywhere else
    for this same prospect, not that raw value."""
    candidates_data = [_candidate_dict(
        name="https://magput.ru/tours/gildiya-gidov-magput",
        url="https://magput.ru/tours/gildiya-gidov-magput",
        display_name="Гильдия гидов Magput",
    )]
    state = State({"partner_discovery_candidates": candidates_data, "partner_discovery_index": 0})
    callback = Callback(PARTNER_CONTACT_FIND)

    captured = {}

    async def _fake_find_contact(prospect, *, llm_provider, web_search_provider):
        captured["prospect"] = prospect
        return _project_contact()

    with patch("app.handlers.partner_discovery.find_contact", _fake_find_contact):
        run(on_partner_contact_find(callback, state))

    assert captured["prospect"].name == "Гильдия гидов Magput"


def test_handler_falls_back_to_raw_name_when_no_display_name():
    """No enrichment ran (e.g. no LLM provider) - falls back to the raw
    name exactly as before this fix, same as test_handler_passes_current_
    prospect_to_find_contact above."""
    candidates_data = [_candidate_dict(name="Raw Name", url="https://a.ru/")]
    state = State({"partner_discovery_candidates": candidates_data, "partner_discovery_index": 0})
    callback = Callback(PARTNER_CONTACT_FIND)

    captured = {}

    async def _fake_find_contact(prospect, *, llm_provider, web_search_provider):
        captured["prospect"] = prospect
        return _project_contact()

    with patch("app.handlers.partner_discovery.find_contact", _fake_find_contact):
        run(on_partner_contact_find(callback, state))

    assert captured["prospect"].name == "Raw Name"


# ── 3. PERSON / HIGH renders correctly ──────────────────────────────────────

def test_person_high_confidence_card():
    state = State({"partner_discovery_candidates": [_candidate_dict()], "partner_discovery_index": 0})
    callback = Callback(PARTNER_CONTACT_FIND)

    with patch(
        "app.handlers.partner_discovery.find_contact",
        AsyncMock(return_value=_person_contact(ContactConfidence.HIGH)),
    ):
        run(on_partner_contact_find(callback, state))

    assert len(callback.message.answers) == 1
    text, kwargs = callback.message.answers[0]
    assert "Найден публичный контакт" in text
    assert "Иван Иванов" in text
    assert "основатель" in text
    assert "Example Club" in text
    assert "https://example-club.ru/about" in text
    assert "высокое" in text
    markup = kwargs["reply_markup"]
    urls = {btn.url for row in markup.inline_keyboard for btn in row if btn.url}
    assert "https://example-club.ru/about" in urls
    callback_datas = {btn.callback_data for row in markup.inline_keyboard for btn in row if btn.callback_data}
    assert PARTNER_CONTACT_BACK in callback_datas


# ── 4. PERSON / MEDIUM renders correctly ────────────────────────────────────

def test_person_medium_confidence_card():
    state = State({"partner_discovery_candidates": [_candidate_dict()], "partner_discovery_index": 0})
    callback = Callback(PARTNER_CONTACT_FIND)

    with patch(
        "app.handlers.partner_discovery.find_contact",
        AsyncMock(return_value=_person_contact(ContactConfidence.MEDIUM)),
    ):
        run(on_partner_contact_find(callback, state))

    text, _ = callback.message.answers[0]
    assert "среднее" in text
    assert "высокое" not in text


# ── 5. PROJECT fallback renders correctly ───────────────────────────────────

def test_project_fallback_card():
    state = State({"partner_discovery_candidates": [_candidate_dict()], "partner_discovery_index": 0})
    callback = Callback(PARTNER_CONTACT_FIND)

    with patch(
        "app.handlers.partner_discovery.find_contact",
        AsyncMock(return_value=_project_contact()),
    ):
        run(on_partner_contact_find(callback, state))

    text, kwargs = callback.message.answers[0]
    assert "не найден" in text.lower()
    assert "не существует" not in text.lower()
    assert "Example Club" in text
    # UX fix (2026-10-07): the URL must appear only once - via the
    # "🌐 Открыть проект" button, not duplicated into the card text.
    assert "https://example-club.ru/" not in text
    markup = kwargs["reply_markup"]
    urls = {btn.url for row in markup.inline_keyboard for btn in row if btn.url}
    assert "https://example-club.ru/" in urls


# ── 6. Contact saved as plain dict in FSM, no DB ────────────────────────────

def test_contact_saved_as_plain_dict_in_state():
    state = State({"partner_discovery_candidates": [_candidate_dict()], "partner_discovery_index": 0})
    callback = Callback(PARTNER_CONTACT_FIND)

    with patch(
        "app.handlers.partner_discovery.find_contact",
        AsyncMock(return_value=_person_contact(ContactConfidence.HIGH)),
    ):
        run(on_partner_contact_find(callback, state))

    stored = state.data["partner_discovery_contact"]
    assert isinstance(stored, dict)
    assert stored["display_name"] == "Иван Иванов"


# ── 7. Back returns the same prospect card without a new search ────────────

def test_back_renders_same_prospect_without_new_search():
    candidates_data = [_candidate_dict("A", "https://a.ru/"), _candidate_dict("B", "https://b.ru/")]
    state = State({"partner_discovery_candidates": candidates_data, "partner_discovery_index": 1})
    callback = Callback(PARTNER_CONTACT_BACK)

    with patch(
        "app.handlers.partner_discovery.PartnerDiscoveryService.find_candidates",
        AsyncMock(side_effect=AssertionError("must not search again")),
    ):
        run(on_partner_contact_back(callback, state))

    assert len(callback.message.answers) == 1
    text, _ = callback.message.answers[0]
    assert "B" in text
    assert "https://b.ru/" in text


# ── 8. find_contact exception -> friendly error, no traceback ──────────────

def test_find_contact_exception_shows_friendly_error():
    state = State({"partner_discovery_candidates": [_candidate_dict()], "partner_discovery_index": 0})
    callback = Callback(PARTNER_CONTACT_FIND)

    with patch(
        "app.handlers.partner_discovery.find_contact",
        AsyncMock(side_effect=RuntimeError("boom")),
    ):
        run(on_partner_contact_find(callback, state))

    assert len(callback.message.answers) == 1
    text, _ = callback.message.answers[0]
    assert "Не удалось проверить публичные контакты" in text
    assert "boom" not in text
    assert "RuntimeError" not in text


# ── 9. Phone/email never reach the rendered card ────────────────────────────

def test_phone_and_email_never_rendered():
    contaminated = ContactCandidate(
        contact_type=ContactType.PERSON,
        display_name="Иван Иванов +7 (900) 123-45-67",
        role="основатель ivan@example.com",
        project_name="Example Club",
        public_url="https://example-club.ru/",
        evidence_text="Звоните +7 900 123 45 67 или пишите ivan@example.com",
        evidence_url="https://example-club.ru/about",
        confidence=ContactConfidence.HIGH,
        provenance="official_page",
    )
    state = State({"partner_discovery_candidates": [_candidate_dict()], "partner_discovery_index": 0})
    callback = Callback(PARTNER_CONTACT_FIND)

    with patch(
        "app.handlers.partner_discovery.find_contact",
        AsyncMock(return_value=contaminated),
    ):
        run(on_partner_contact_find(callback, state))

    text, _ = callback.message.answers[0]
    assert "ivan@example.com" not in text
    assert "123-45-67" not in text
    assert "123 45 67" not in text


# ── 10. Ordinary discovery flow is unaffected ───────────────────────────────

def test_prospect_card_keyboard_still_has_open_and_next_buttons():
    markup = partner_discovery_card_keyboard("https://a.ru/", has_more_in_batch=True)
    urls = {btn.url for row in markup.inline_keyboard for btn in row if btn.url}
    callback_datas = {btn.callback_data for row in markup.inline_keyboard for btn in row if btn.callback_data}
    assert "https://a.ru/" in urls
    assert "partner_discovery:next" in callback_datas
    assert PARTNER_CONTACT_FIND in callback_datas


def test_no_candidates_in_state_is_a_safe_noop():
    state = State({})
    callback = Callback(PARTNER_CONTACT_FIND)

    run(on_partner_contact_find(callback, state))

    assert callback.message.answers == []


# ── 11. Outreach draft v0 ────────────────────────────────────────────────────

def _workspace(workspace_id: int = 1) -> WorkspaceContext:
    return WorkspaceContext(100, workspace_id, "owner", "active")


def _partner_repository(
    ta_affiliated: bool = False, business_name: str | None = None,
    ta_enrollment_url: str | None = None,
):
    profile = SimpleNamespace(
        business_type="club_partner", ta_affiliated=ta_affiliated, business_name=business_name,
        ta_enrollment_url=ta_enrollment_url,
    )
    return SimpleNamespace(get_business_profile=AsyncMock(return_value=profile))


def _person_contact_dict() -> dict:
    return {
        "contact_type": "person",
        "display_name": "Ярмоленко Людмила Борисовна",
        "role": "экскурсовод",
        "project_name": "Магазин Путешествий",
        "public_url": "https://magput.ru/tours/gildiya-gidov-magput",
        "evidence_text": "Ярмоленко Людмила Борисовна - экскурсовод Гильдии гидов Magput.",
        "evidence_url": "https://magput.ru/?viewprog=63718",
        "confidence": "MEDIUM",
        "provenance": "professional_search",
    }


def test_draft_button_present_only_on_person_card():
    person_markup = partner_contact_card_keyboard("person", "https://a.ru/")
    project_markup = partner_contact_card_keyboard("project", "https://a.ru/")
    person_callbacks = {
        btn.callback_data for row in person_markup.inline_keyboard for btn in row if btn.callback_data
    }
    project_callbacks = {
        btn.callback_data for row in project_markup.inline_keyboard for btn in row if btn.callback_data
    }
    assert PARTNER_CONTACT_DRAFT in person_callbacks
    assert PARTNER_CONTACT_DRAFT not in project_callbacks


def test_draft_button_callback_produces_fallback_draft_without_llm_provider():
    state = State({"partner_discovery_contact": _person_contact_dict()})
    callback = Callback(PARTNER_CONTACT_DRAFT)

    run(on_partner_contact_draft(
        callback, state, partner_repository=_partner_repository(), workspace_context=_workspace(),
    ))

    text, _ = callback.message.answers[0]
    # Greeted by "Имя Отчество" (patronymic extraction), not the full ФИО -
    # see _greeting_name.
    assert "Людмила Борисовна" in text
    assert "экскурсовод" in text
    assert "Магазин Путешествий" in text
    assert "проверьте" in text.lower()  # draft header, review-before-send framing


def test_draft_unavailable_without_a_contact_in_state():
    state = State({})
    callback = Callback(PARTNER_CONTACT_DRAFT)

    run(on_partner_contact_draft(
        callback, state, partner_repository=_partner_repository(), workspace_context=_workspace(),
    ))

    text, _ = callback.message.answers[0]
    assert "Нет найденного контакта" in text


def test_draft_unavailable_for_project_fallback_contact():
    project_dict = {
        "contact_type": "project", "display_name": "Example Club", "project_name": "Example Club",
        "public_url": "https://example-club.ru/", "evidence_text": "", "evidence_url": "https://example-club.ru/",
        "confidence": None, "provenance": "person_not_found",
    }
    state = State({"partner_discovery_contact": project_dict})
    callback = Callback(PARTNER_CONTACT_DRAFT)

    run(on_partner_contact_draft(
        callback, state, partner_repository=_partner_repository(), workspace_context=_workspace(),
    ))

    text, _ = callback.message.answers[0]
    assert "Нет найденного контакта" in text


def test_draft_mentions_travel_advantage_only_when_ta_affiliated():
    state_ta = State({"partner_discovery_contact": _person_contact_dict()})
    ta_callback = Callback(PARTNER_CONTACT_DRAFT)
    run(on_partner_contact_draft(
        ta_callback, state_ta,
        partner_repository=_partner_repository(ta_affiliated=True), workspace_context=_workspace(),
    ))
    ta_text, _ = ta_callback.message.answers[0]

    state_no_ta = State({"partner_discovery_contact": _person_contact_dict()})
    no_ta_callback = Callback(PARTNER_CONTACT_DRAFT)
    run(on_partner_contact_draft(
        no_ta_callback, state_no_ta,
        partner_repository=_partner_repository(ta_affiliated=False), workspace_context=_workspace(),
    ))
    no_ta_text, _ = no_ta_callback.message.answers[0]

    assert "Travel Advantage" in ta_text
    assert "Travel Advantage" not in no_ta_text


def test_draft_never_promises_income_or_results():
    state = State({"partner_discovery_contact": _person_contact_dict()})
    callback = Callback(PARTNER_CONTACT_DRAFT)

    run(on_partner_contact_draft(
        callback, state, partner_repository=_partner_repository(ta_affiliated=True), workspace_context=_workspace(),
    ))

    text, _ = callback.message.answers[0]
    lowered = text.lower()
    for forbidden in ("доход", "гаранти", "окупаем", "заработ", "вы заинтересован", "ей нужен travel advantage"):
        assert forbidden not in lowered


def test_draft_is_fully_gender_neutral():
    """2026-10-07 fix: no self-referential verb/construction may decline by
    the sender's (unknown) grammatical gender - "увидел(а)"/"решил(а)" and
    similar are banned outright, not just their "(а)" suffix form."""
    state = State({"partner_discovery_contact": _person_contact_dict()})
    callback = Callback(PARTNER_CONTACT_DRAFT)

    run(on_partner_contact_draft(
        callback, state,
        partner_repository=_partner_repository(ta_affiliated=True, business_name="Südwind Travel Club"),
        workspace_context=_workspace(),
    ))

    text, _ = callback.message.answers[0]
    lowered = text.lower()
    forbidden_gendered_forms = (
        "(а)", "нашёл", "нашла", "увидел", "увидела", "хотел", "хотела",
        "решил", "решила", "был бы", "была бы", "рад(", "рада",
    )
    for forbidden in forbidden_gendered_forms:
        assert forbidden not in lowered, f"found gendered form: {forbidden!r} in draft"


def test_draft_uses_sender_business_name_when_known():
    state = State({"partner_discovery_contact": _person_contact_dict()})
    callback = Callback(PARTNER_CONTACT_DRAFT)

    run(on_partner_contact_draft(
        callback, state,
        partner_repository=_partner_repository(ta_affiliated=True, business_name="Südwind Travel Club"),
        workspace_context=_workspace(),
    ))

    text, _ = callback.message.answers[0]
    assert "Südwind Travel Club" in text


def test_draft_omits_sender_business_name_when_unknown_without_inventing_one():
    state = State({"partner_discovery_contact": _person_contact_dict()})
    callback = Callback(PARTNER_CONTACT_DRAFT)

    run(on_partner_contact_draft(
        callback, state,
        partner_repository=_partner_repository(ta_affiliated=True, business_name=None),
        workspace_context=_workspace(),
    ))

    text, _ = callback.message.answers[0]
    assert "Представляю проект" not in text
    assert "«None»" not in text


def test_draft_differs_across_two_tenants_with_the_same_code_path():
    """Proves no hardcoding: same code, two different (profile, contact)
    inputs -> two different drafts, each reflecting ITS OWN tenant's
    business_name and ITS OWN found PERSON - never Vladimir/a fixed
    project/a fixed recipient baked into the template."""
    other_person = {
        "contact_type": "person",
        "display_name": "Петров Андрей Викторович",
        "role": "организатор туров",
        "project_name": "Клуб Путешественников Атлас",
        "public_url": "https://atlas-club.example/",
        "evidence_text": "Петров Андрей Викторович - организатор туров клуба Атлас.",
        "evidence_url": "https://atlas-club.example/about",
        "confidence": "MEDIUM",
        "provenance": "professional_search",
    }

    state_a = State({"partner_discovery_contact": _person_contact_dict()})
    callback_a = Callback(PARTNER_CONTACT_DRAFT)
    run(on_partner_contact_draft(
        callback_a, state_a,
        partner_repository=_partner_repository(ta_affiliated=True, business_name="Südwind Travel Club"),
        workspace_context=_workspace(1),
    ))
    text_a, _ = callback_a.message.answers[0]

    state_b = State({"partner_discovery_contact": other_person})
    callback_b = Callback(PARTNER_CONTACT_DRAFT)
    run(on_partner_contact_draft(
        callback_b, state_b,
        partner_repository=_partner_repository(ta_affiliated=True, business_name="Atlas Partners"),
        workspace_context=_workspace(2),
    ))
    text_b, _ = callback_b.message.answers[0]

    assert "Людмила Борисовна" in text_a and "Südwind Travel Club" in text_a
    assert "Андрей Викторович" in text_b and "Atlas Partners" in text_b
    assert text_a != text_b
    assert "Андрей Викторович" not in text_a
    assert "Südwind Travel Club" not in text_b


# ── 12. Free-text draft commands continue the same contact context ─────────

def test_draft_command_filter_requires_person_contact_in_state():
    message = SimpleNamespace(text="Подготовь письмо Ярмоленко")
    state_with_person = State({"partner_discovery_contact": _person_contact_dict()})
    state_empty = State({})

    assert run(_looks_like_draft_command(message, state_with_person)) is True
    assert run(_looks_like_draft_command(message, state_empty)) is False


def test_draft_command_filter_rejects_unrelated_text():
    message = SimpleNamespace(text="Привет, как дела?")
    state_with_person = State({"partner_discovery_contact": _person_contact_dict()})

    assert run(_looks_like_draft_command(message, state_with_person)) is False


def test_draft_command_filter_rejects_project_fallback_contact():
    project_dict = {
        "contact_type": "project", "display_name": "Example Club", "project_name": "Example Club",
        "public_url": "https://example-club.ru/", "evidence_text": "", "evidence_url": "https://example-club.ru/",
        "confidence": None, "provenance": "person_not_found",
    }
    message = SimpleNamespace(text="Что ей написать?")
    state = State({"partner_discovery_contact": project_dict})

    assert run(_looks_like_draft_command(message, state)) is False


def test_on_draft_command_text_produces_the_same_draft_as_the_button():
    class TextMessage:
        def __init__(self, text: str) -> None:
            self.text = text
            self.answers = []

        async def answer(self, text, **kwargs):
            self.answers.append((text, kwargs))

    message = TextMessage("Подготовь письмо к первому контакту Ярмоленко")
    state = State({"partner_discovery_contact": _person_contact_dict()})

    run(on_draft_command_text(
        message, state, partner_repository=_partner_repository(), workspace_context=_workspace(),
    ))

    text, _ = message.answers[0]
    assert "Людмила Борисовна" in text


# ── 13. Ordinary discovery flow still unaffected by the new draft button ───

def test_prospect_card_keyboard_unaffected_by_draft_feature():
    markup = partner_discovery_card_keyboard("https://a.ru/", has_more_in_batch=True)
    callback_datas = {btn.callback_data for row in markup.inline_keyboard for btn in row if btn.callback_data}
    assert PARTNER_CONTACT_DRAFT not in callback_datas


# ── 14. Multi-tenant ta_enrollment_url - cold draft never includes a link ──

class TextMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


def test_cold_draft_never_contains_a_url_even_with_ta_enrollment_url_set():
    """Task requirement #1: the FIRST cold-open message to a freshly-found
    PERSON never auto-inserts ta_enrollment_url, even when it's set."""
    state = State({"partner_discovery_contact": _person_contact_dict()})
    callback = Callback(PARTNER_CONTACT_DRAFT)

    run(on_partner_contact_draft(
        callback, state,
        partner_repository=_partner_repository(
            ta_affiliated=True, business_name="Südwind Travel Club",
            ta_enrollment_url="https://mwrlife.example/enroll/workspace-a",
        ),
        workspace_context=_workspace(1),
    ))

    text, _ = callback.message.answers[0]
    assert "http" not in text.lower()


def test_workspace_a_and_b_get_their_own_enrollment_url_never_mixed():
    """Task requirement: tenant-specific link, never another workspace's."""
    state_a = State({"partner_discovery_contact": _person_contact_dict()})
    message_a = TextMessage("дай ему мою ссылку")
    run(on_invitation_link_command_text(
        message_a, state_a,
        partner_repository=_partner_repository(
            ta_affiliated=True, ta_enrollment_url="https://mwrlife.example/enroll/workspace-a",
        ),
        workspace_context=_workspace(1),
    ))
    text_a, _ = message_a.answers[0]

    other_person = {
        "contact_type": "person", "display_name": "Петров Андрей Викторович",
        "role": "организатор туров", "project_name": "Клуб Путешественников Атлас",
        "public_url": "https://atlas-club.example/", "evidence_text": "ev",
        "evidence_url": "https://atlas-club.example/about", "confidence": "MEDIUM",
        "provenance": "professional_search",
    }
    state_b = State({"partner_discovery_contact": other_person})
    message_b = TextMessage("отправь ссылку для регистрации")
    run(on_invitation_link_command_text(
        message_b, state_b,
        partner_repository=_partner_repository(
            ta_affiliated=True, ta_enrollment_url="https://mwrlife.example/enroll/workspace-b",
        ),
        workspace_context=_workspace(2),
    ))
    text_b, _ = message_b.answers[0]

    assert "https://mwrlife.example/enroll/workspace-a" in text_a
    assert "https://mwrlife.example/enroll/workspace-b" not in text_a
    assert "https://mwrlife.example/enroll/workspace-b" in text_b
    assert "https://mwrlife.example/enroll/workspace-a" not in text_b


def test_missing_enrollment_url_does_not_fall_back_to_anything():
    """Task requirement: empty ta_enrollment_url must never get a global/
    shared/other-tenant fallback - only a clear message telling the owner
    to set their own link first."""
    state = State({"partner_discovery_contact": _person_contact_dict()})
    message = TextMessage("подготовь приглашение со ссылкой")

    run(on_invitation_link_command_text(
        message, state,
        partner_repository=_partner_repository(ta_affiliated=True, ta_enrollment_url=None),
        workspace_context=_workspace(1),
    ))

    text, _ = message.answers[0]
    assert "http" not in text.lower()
    assert "укажите свою" in text.lower()


def test_explicit_invitation_request_uses_current_workspace_link():
    state = State({"partner_discovery_contact": _person_contact_dict()})
    message = TextMessage("Подготовь приглашение со ссылкой")

    run(on_invitation_link_command_text(
        message, state,
        partner_repository=_partner_repository(
            ta_affiliated=True, ta_enrollment_url="https://mwrlife.example/enroll/workspace-a",
        ),
        workspace_context=_workspace(1),
    ))

    text, _ = message.answers[0]
    assert "https://mwrlife.example/enroll/workspace-a" in text
    assert "Людмила Борисовна" in text


def test_invitation_link_filter_requires_person_contact_in_state():
    message = SimpleNamespace(text="дай ему мою ссылку")
    state_with_person = State({"partner_discovery_contact": _person_contact_dict()})
    state_empty = State({})

    assert run(_looks_like_invitation_link_command(message, state_with_person)) is True
    assert run(_looks_like_invitation_link_command(message, state_empty)) is False


def test_invitation_link_filter_does_not_steal_cold_draft_commands():
    """The two natural-language intents must not collide - a plain
    "подготовь письмо" must never be caught by the link-specific filter."""
    message = SimpleNamespace(text="Подготовь письмо Ярмоленко")
    state = State({"partner_discovery_contact": _person_contact_dict()})

    assert run(_looks_like_invitation_link_command(message, state)) is False
    assert run(_looks_like_draft_command(message, state)) is True
