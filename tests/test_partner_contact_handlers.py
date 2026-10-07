"""Partner Discovery v0 + Contact Discovery v0 Telegram UI integration -
read-only layer on top of the same FSM batch, no DB, no new Partner
Discovery search. app/services/contact_discovery.py itself is mocked here
(it has its own focused test suite in tests/test_contact_discovery.py)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.domain.contacts import ContactCandidate, ContactConfidence, ContactType
from app.handlers.partner_discovery import (
    on_partner_contact_back,
    on_partner_contact_find,
)
from app.keyboards import PARTNER_CONTACT_BACK, PARTNER_CONTACT_FIND, partner_discovery_card_keyboard


def run(coro):
    return asyncio.run(coro)


def _candidate_dict(name="Example Club", url="https://example-club.ru/"):
    return {
        "name": name, "url": url, "prospect_type": "travel_club",
        "query": "клуб путешественников", "evidence": "ev", "evidence_source": "page",
        "fit": "STRONG",
    }


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
    assert "https://example-club.ru/" in text
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
