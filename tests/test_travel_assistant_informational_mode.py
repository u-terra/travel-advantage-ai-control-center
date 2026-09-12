"""ORCHESTRAVEL: split TRAVEL_ASSISTANT into two on_free_text modes.

Before this change, every plain-typed TRAVEL_ASSISTANT question - regardless
of who it was actually about - went through
MaterialOrchestrationService.build_client_reply_generation_spec(): heading
"💬 Черновик ответа клиенту — для ручной проверки", and an OBJECTIVE
("Сформировать короткий личный ответ клиенту в Telegram по его вопросу")
that told the model to write as if replying to a third party. A bare
factual/current travel question the USER asked for themselves - e.g. "Какие
сейчас изменения правил въезда в Индонезию для россиян?" (see
app/routing/keywords.py's own "Live prod bug" comment - that fix made this
query confidently TRAVEL_ASSISTANT, but did not address this framing bug) -
got the exact same "answer your client" framing, which is wrong: there is no
client in this conversation, only the person asking.

The fix (see app/handlers/tasks.py's _INFORMATIONAL_HEADING comment and
app/services/material_orchestration.py's build_informational_generation_spec)
splits on-free-text TRAVEL_ASSISTANT into:
  - INFORMATIONAL: no explicit client-intent phrase in the message (only a
    bare topic word, e.g. "виза"/"въезд"/"тариф") -> plain assistant answer,
    new OBJECTIVE/CONSTRAINTS with no "ответ клиенту" framing anywhere.
  - CLIENT_REPLY: an explicit client-intent phrase from
    ASSISTANT_INTENT_KEYWORDS is present ("клиент спрашивает", "что ответить
    клиенту", "ответить человеку", ...) -> unchanged existing client-reply
    flow, heading, and OBJECTIVE.

The split only ever runs when reply_context is None, i.e. only at the plain
on_free_text entry point this bug was reported against - every button-driven
"💬 Ответить клиенту" flow (_route_and_dispatch, reply_context always set
once primary_module is TRAVEL_ASSISTANT) keeps its previous CLIENT_REPLY-only
behavior unconditionally, verified here and in the untouched
test_client_reply_v2_flow_skips_technical_route_card (tests/test_journal_handlers.py).
"""

from __future__ import annotations

import asyncio

from app.handlers.tasks import _CLIENT_REPLY_HEADING, _INFORMATIONAL_HEADING, on_free_text
from app.routing.modules import Module
from app.routing.router import route_text
from app.services.llm.models import ContentDraft
from app.services.web_search.base import SearchResponse, SearchResult, WebSearchProvider
from app.services.web_search.service import WebSearchService
from tests.llm_fakes import FakeLLMProvider
from tests.test_journal_handlers import Message, business_profile, context, journal, profile_repository

_INDONESIA_QUERY = "Какие сейчас изменения правил въезда в Индонезию для россиян?"
_CLIENT_REPLY_QUERY = "Клиент спрашивает, нужна ли виза на Бали. Что ему ответить?"
_BALI_POST_REQUEST = "Напиши пост о путешествии на Бали"
_VAGUE_FOLLOWUP = "Что с этим делать?"

_SOURCE_URL = "https://example.com/indonesia-entry-rules"


def run(coro):
    return asyncio.run(coro)


class _FakeProvider(WebSearchProvider):
    name = "fake"

    def __init__(self, response: SearchResponse | None = None):
        self._response = response
        self.calls: list[str] = []

    def search(self, query, *, site=None, limit=5, search_type=None):
        self.calls.append(query)
        return self._response


def _sample_response(query: str) -> SearchResponse:
    return SearchResponse(
        query=query,
        results=[
            SearchResult(
                title="Правила въезда в Индонезию для россиян",
                url=_SOURCE_URL,
                snippet="С 2026 года визовые правила при въезде изменились.",
                domain="example.com", published_at=None, provider="fake", rank=1,
            ),
        ],
        provider="fake",
        elapsed_ms=5,
    )


def run_free_text(text: str, *, web_search_service: WebSearchService | None):
    message = Message(text)
    provider = FakeLLMProvider(draft=ContentDraft("Черновик", ()))
    profiles = profile_repository(business_profile())
    run(on_free_text(
        message, journal(), provider, context(), profiles,
        web_search_service=web_search_service,
    ))
    return message, provider


# A. Exact Indonesia query: INFORMATIONAL end-to-end.
def test_a_indonesia_query_is_travel_assistant_and_informational():
    decision = route_text(_INDONESIA_QUERY)
    assert decision.is_uncertain is False
    assert decision.primary_module is Module.TRAVEL_ASSISTANT


def test_a_indonesia_query_triggers_web_search():
    # _sample_response() has no official domain, and _INDONESIA_QUERY
    # matches the changeable-rules gate ("въезд") - so this also triggers
    # exactly one official-source fallback search (stage two of
    # official-source priority; see test_web_search_service.py's dedicated
    # fallback tests). This test only cares the ORIGINAL query was searched.
    fake_provider = _FakeProvider(_sample_response(_INDONESIA_QUERY))
    service = WebSearchService(fake_provider, enabled=True)
    run_free_text(_INDONESIA_QUERY, web_search_service=service)
    assert fake_provider.calls[0] == _INDONESIA_QUERY
    assert len(fake_provider.calls) == 2


def test_a_indonesia_query_uses_informational_heading_not_client_heading():
    fake_provider = _FakeProvider(_sample_response(_INDONESIA_QUERY))
    service = WebSearchService(fake_provider, enabled=True)
    message, _ = run_free_text(_INDONESIA_QUERY, web_search_service=service)
    text = message.answers[-1][0]
    assert _INFORMATIONAL_HEADING in text
    assert "Черновик ответа клиенту" not in text


def test_a_indonesia_query_prompt_has_no_client_reply_framing():
    fake_provider = _FakeProvider(_sample_response(_INDONESIA_QUERY))
    service = WebSearchService(fake_provider, enabled=True)
    _, provider = run_free_text(_INDONESIA_QUERY, web_search_service=service)
    source_text = provider.generate_draft.call_args.kwargs["source_text"]
    assert "ответ клиенту" not in source_text.lower()
    assert "личный ответ клиенту" not in source_text.lower()


def test_a_indonesia_query_search_context_reaches_generation():
    fake_provider = _FakeProvider(_sample_response(_INDONESIA_QUERY))
    service = WebSearchService(fake_provider, enabled=True)
    _, provider = run_free_text(_INDONESIA_QUERY, web_search_service=service)
    source_text = provider.generate_draft.call_args.kwargs["source_text"]
    assert "АКТУАЛЬНЫЙ ПОИСК В ИНТЕРНЕТЕ" in source_text
    assert _SOURCE_URL in source_text


def test_a_indonesia_query_shows_sources_to_user():
    fake_provider = _FakeProvider(_sample_response(_INDONESIA_QUERY))
    service = WebSearchService(fake_provider, enabled=True)
    message, _ = run_free_text(_INDONESIA_QUERY, web_search_service=service)
    text = message.answers[-1][0]
    assert "Источники:" in text
    assert _SOURCE_URL in text


# B. Exact client-reply query: CLIENT_REPLY flow preserved.
def test_b_client_reply_query_is_travel_assistant():
    decision = route_text(_CLIENT_REPLY_QUERY)
    assert decision.is_uncertain is False
    assert decision.primary_module is Module.TRAVEL_ASSISTANT


def test_b_client_reply_query_keeps_client_heading():
    message, _ = run_free_text(_CLIENT_REPLY_QUERY, web_search_service=None)
    text = message.answers[-1][0]
    assert _CLIENT_REPLY_HEADING in text


def test_b_client_reply_query_keeps_client_reply_objective_in_prompt():
    _, provider = run_free_text(_CLIENT_REPLY_QUERY, web_search_service=None)
    source_text = provider.generate_draft.call_args.kwargs["source_text"]
    assert "Сформировать короткий личный ответ клиенту" in source_text


# C. Content Factory regression: no informational/client-reply split applies.
def test_c_bali_post_request_stays_content_factory_without_search():
    fake_provider = _FakeProvider(_sample_response(_BALI_POST_REQUEST))
    service = WebSearchService(fake_provider, enabled=True)
    decision = route_text(_BALI_POST_REQUEST)
    assert decision.is_uncertain is False
    assert decision.primary_module is Module.CONTENT_FACTORY
    message, _ = run_free_text(_BALI_POST_REQUEST, web_search_service=service)
    assert fake_provider.calls == []
    text = message.answers[-1][0]
    assert _INFORMATIONAL_HEADING not in text
    assert _CLIENT_REPLY_HEADING not in text


# D. Vague follow-up: still uncertain.
def test_d_vague_followup_remains_uncertain():
    decision = route_text(_VAGUE_FOLLOWUP)
    assert decision.is_uncertain is True
    assert decision.primary_module is Module.ORCHESTRATOR


# E. Regression: the button-driven "Ответить клиенту" flow must keep the
# CLIENT_REPLY heading even for a message with only a bare topic word (no
# ASSISTANT_INTENT_KEYWORDS phrase) - reply_context is what decides this
# flow, not message wording. Same scenario as
# test_client_reply_v2_flow_skips_technical_route_card in
# tests/test_journal_handlers.py, asserted again here to document the
# reply_context is None gate explicitly for this task.
def test_e_button_driven_client_reply_ignores_wording_heuristic():
    from app.handlers.tasks import _route_and_dispatch
    from app.routing.modules import Module as RoutingModule
    from tests.test_journal_handlers import State

    message = Message("Можно ли оплатить бронирование из России?")
    provider = FakeLLMProvider(draft=ContentDraft("Черновик ответа", ()))
    profiles = profile_repository(business_profile())
    state = State({})
    run(_route_and_dispatch(
        message, state, journal(), provider, context(), profiles,
        "Можно ли оплатить бронирование из России?",
        forced_module=RoutingModule.TRAVEL_ASSISTANT,
        skip_route_card=True,
        reply_subject_data=(None, None, None),
    ))
    text = message.answers[-1][0]
    assert _CLIENT_REPLY_HEADING in text
    assert _INFORMATIONAL_HEADING not in text
