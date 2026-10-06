"""Partner Discovery v0 Telegram UI tests - read-only, no DB, no new search
queries beyond calling the already-tested PartnerDiscoveryService.find_candidates
(mocked here; app/services/partner_discovery.py has its own test suite)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.domain.partners import WorkspaceContext
from app.domain.prospects import EvidenceSource, ProspectCandidate, ProspectFit, ProspectType
from app.handlers.partner_discovery import (
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
