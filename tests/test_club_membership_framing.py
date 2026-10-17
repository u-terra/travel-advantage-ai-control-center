"""Regression tests for a confirmed live-dialogue bug: asked "что такое
Travel Advantage и как рассказать об этом клиенту?", ORCHESTRAVEL answered
as if Travel Advantage were an open booking platform (search/book like any
OTA) and only gave the correct club/membership framing after a user's own
follow-up pushed it there.

Root cause (see app.services.knowledge_generation_context): a prompt-
priority/grounding-order issue, not a missing fact. ta.platform's own core-
definition fact ("OTA / Online Travel Agency") carries no club nuance on
its own, and ta.platform.use.search_booking already says booking is done
"участниками" (BY MEMBERS) - but nothing told the model what that implies.
advisor-product-structure-brief.md / partner-training-official-delta-2026-
08.md already confirm the underlying model (an additional traveler "получает
собственный доступ", "основной держатель membership остаётся владельцем
аккаунта") - _CLUB_MEMBERSHIP_CONSTRAINT only makes the model draw the
conclusion that was already implicit in confirmed facts; no new fact,
price, refund rule, or Ambassador capability was invented.

Fixed in exactly ONE shared layer - app.services.knowledge_generation_
context.advisory_judgment_constraints() (reused from inside
build_knowledge_generation_context() itself, same pattern as the existing
_COMPENSATION_CONSTRAINT) - so there is a single source of truth for this
rule, not two independent copies:

- Telegram's informational TRAVEL_ASSISTANT persona already builds its
  prompt via MaterialOrchestrationService.build_client_reply_generation_spec
  -> build_knowledge_generation_context(), so it gets this constraint for
  free, no Telegram-specific change needed.
- Web's POST /api/chat plain-chat path previously rendered the retrieved
  KnowledgeBundle through its OWN bespoke app.web_api._knowledge_context()
  text formatter, which did not reuse build_knowledge_generation_context()
  at all (and did not even apply the pre-existing _COMPENSATION_CONSTRAINT).
  _knowledge_context() now calls the newly-exported advisory_judgment_
  constraints() helper directly and appends its output to the same
  "Обязательные ограничения" text block it already renders - one rule,
  read by both channels, no duplicate judgment logic.

Also covers: app.services.knowledge_service._retrieval_policy() now
requires ta.platform for a plain booking-capability question ("клиент может
просто зайти и забронировать отель?") even without an explicit membership_
intent keyword - scoped to NOT fire when membership_intent is already True,
so it doesn't reopen the already-tight SOURCE FACTS budget fight covered by
tests/test_knowledge_generation_context_membership_value.py.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.handlers.tasks import on_task_after_button
from app.routing.modules import Module
from app.services.knowledge_generation_context import _CLUB_MEMBERSHIP_CONSTRAINT
from app.services.llm.models import ContentDraft
from app.services.reference_resolver import ReferenceResolver
from tests.llm_fakes import FakeLLMProvider
from tests.test_journal_handlers import (
    Message, State, business_profile, context, journal, profile_repository,
)
from tests.test_knowledge_retrieval import service as _build_knowledge_service
from tests.test_reference_resolver_integration import CountingKnowledgeService

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("markdown")

from fastapi.testclient import TestClient  # noqa: E402

from tests._web_auth_test_helpers import login_as  # noqa: E402

OWNER_ID = 586249067

QUESTION_WHAT_IS_IT = "Объясни просто: что такое Travel Advantage и как рассказать об этом клиенту?"
QUESTION_PARTNER_BENEFIT = "В чём выгода Travel Advantage менеджеру или экскурсоводу?"
QUESTION_CAN_CLIENT_BOOK = "Клиент может просто зайти и забронировать отель?"


def run(value):
    return asyncio.run(value)


# ── Telegram side: on_task_after_button, same harness as
# tests/test_grounded_knowledge_generation.py ───────────────────────────────

def _telegram_source_text(tmp_path: Path, question: str) -> str:
    knowledge = CountingKnowledgeService(_build_knowledge_service(tmp_path))
    resolver = ReferenceResolver(knowledge)
    provider = FakeLLMProvider(draft=ContentDraft("grounded reply", ()))
    message = Message(question)
    state = State({
        "forced_module": Module.TRAVEL_ASSISTANT.value,
        "skip_route_card": True,
    })
    run(on_task_after_button(
        message, state, journal(), provider, context(),
        profile_repository(business_profile()), reference_resolver=resolver,
    ))
    provider.generate_draft.assert_called_once()
    return provider.generate_draft.call_args.kwargs["source_text"]


# ── Web side: POST /api/chat, same harness as tests/test_web_api_client_reply.py /
# tests/test_web_api_web_search.py ──────────────────────────────────────────

@pytest.fixture
def web_api_client(tmp_path, monkeypatch):
    db_path = tmp_path / "journal.sqlite3"
    monkeypatch.setenv("JOURNAL_DB_PATH", str(db_path))
    monkeypatch.setenv("PLANNER_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "false")

    import sys
    sys.modules.pop("app.web_api", None)
    import app.web_api as web_api

    # Real, production knowledge dataset - same fixture builder the
    # KnowledgeService-level tests use - swapped in for the module-level
    # knowledge_service web_api.py builds from the (here, empty) journal DB,
    # so /api/chat's retrieval sees the same facts Telegram's tests do.
    web_api.knowledge_service = _build_knowledge_service(tmp_path / "knowledge")

    with TestClient(web_api.app, base_url="https://testserver") as client:
        ws, _ = run(web_api.partner_repository.ensure_owner_workspace(OWNER_ID))
        login_as(client, web_api, ws.id, OWNER_ID)
        yield client, web_api

    sys.modules.pop("app.web_api", None)


def _web_knowledge_context(web_api_client_tuple, question: str) -> str:
    client, web_api = web_api_client_tuple
    captured: dict = {}

    def _fake_generate(**kwargs):
        captured.update(kwargs)
        return _ChatResult("ok")

    conversation_id = client.post("/api/conversations").json()["conversation"]["id"]
    web_api.chat_provider.generate = _fake_generate
    response = client.post("/api/chat", json={
        "message": question, "conversation_id": conversation_id,
    })
    assert response.status_code == 200
    return captured.get("knowledge_context", "")


class _ChatResult:
    def __init__(self, text: str) -> None:
        self.text = text
        self.usage = None


# ── A/B: "что такое Travel Advantage и как рассказать об этом клиенту?" ────

def test_A_web_what_is_it_gets_club_membership_framing(web_api_client) -> None:
    knowledge_context = _web_knowledge_context(web_api_client, QUESTION_WHAT_IS_IT)
    assert _CLUB_MEMBERSHIP_CONSTRAINT in knowledge_context


def test_B_telegram_what_is_it_gets_club_membership_framing(tmp_path) -> None:
    source_text = _telegram_source_text(tmp_path, QUESTION_WHAT_IS_IT)
    assert _CLUB_MEMBERSHIP_CONSTRAINT in source_text
    # The club/membership rule is a CONSTRAINT, structurally placed ahead of
    # the untrusted source content (the client's own question) in every
    # provider request this codebase builds - i.e. ahead of where the model
    # would otherwise jump straight to listing platform features.
    constraint_pos = source_text.index(_CLUB_MEMBERSHIP_CONSTRAINT)
    untrusted_pos = source_text.index("[UNTRUSTED SOURCE CONTENT")
    assert constraint_pos < untrusted_pos


# ── C/D: "В чём выгода Travel Advantage менеджеру или экскурсоводу?" ────────

def test_C_web_partner_benefit_question_gets_club_membership_framing(web_api_client) -> None:
    knowledge_context = _web_knowledge_context(web_api_client, QUESTION_PARTNER_BENEFIT)
    assert _CLUB_MEMBERSHIP_CONSTRAINT in knowledge_context


def test_D_telegram_partner_benefit_question_gets_club_membership_framing(tmp_path) -> None:
    source_text = _telegram_source_text(tmp_path, QUESTION_PARTNER_BENEFIT)
    assert _CLUB_MEMBERSHIP_CONSTRAINT in source_text


# ── E: "Клиент может просто зайти и забронировать отель?" ───────────────────

def test_E_web_can_client_just_book_gets_club_membership_framing(web_api_client) -> None:
    knowledge_context = _web_knowledge_context(web_api_client, QUESTION_CAN_CLIENT_BOOK)
    assert _CLUB_MEMBERSHIP_CONSTRAINT in knowledge_context


def test_E_telegram_can_client_just_book_gets_club_membership_framing(tmp_path) -> None:
    source_text = _telegram_source_text(tmp_path, QUESTION_CAN_CLIENT_BOOK)
    assert _CLUB_MEMBERSHIP_CONSTRAINT in source_text


# ── F: Member vs Ambassador are never conflated by this rule ───────────────

def test_F_member_and_ambassador_are_not_conflated_in_the_constraint_text() -> None:
    assert "Member" in _CLUB_MEMBERSHIP_CONSTRAINT or "партнёрская роль" in _CLUB_MEMBERSHIP_CONSTRAINT
    assert "разные статусы" in _CLUB_MEMBERSHIP_CONSTRAINT


def test_F_plain_what_is_it_question_does_not_pull_in_ambassador_role_item(tmp_path) -> None:
    """A plain "what is Travel Advantage" question has no ambassador/partner
    -role intent of its own - mwr.member_vs_ambassador must not be required
    just because the club/membership constraint fires (see
    _AMBASSADOR_ROLE_INTENT_STEMS in app.services.knowledge_service - this
    guards against the two concerns being accidentally merged)."""
    bundle = run(_build_knowledge_service(tmp_path).retrieve(QUESTION_WHAT_IS_IT))
    all_keys = {item.stable_key for item in (*bundle.primary_items, *bundle.related_items)}
    assert "mwr.member_vs_ambassador" not in all_keys


# ── G: no "always cheaper"/guaranteed-savings promise in the rule itself ───

def test_G_constraint_never_promises_guaranteed_savings() -> None:
    lowered = _CLUB_MEMBERSHIP_CONSTRAINT.lower()
    for forbidden in ("всегда дешевле", "гарантированн", "обязательно выгод"):
        assert forbidden not in lowered


# ── H: no "just like Booking.com, no membership needed" framing ────────────

def test_H_constraint_explicitly_rejects_open_booking_site_framing() -> None:
    lowered = _CLUB_MEMBERSHIP_CONSTRAINT.lower()
    assert "открыт" in lowered
    assert "membership" in lowered
    assert "аккаунт" in lowered


# ── I: Web and Telegram read the exact same rule - no drift ────────────────

def test_I_web_and_telegram_both_carry_the_identical_constraint_text(
    web_api_client, tmp_path,
) -> None:
    """Single source of truth check: both channels' rendered prompt text
    contains the literal same app.services.knowledge_generation_context
    constant - never two independently-worded copies of the same rule."""
    web_context = _web_knowledge_context(web_api_client, QUESTION_WHAT_IS_IT)
    telegram_source_text = _telegram_source_text(tmp_path, QUESTION_WHAT_IS_IT)

    assert _CLUB_MEMBERSHIP_CONSTRAINT in web_context
    assert _CLUB_MEMBERSHIP_CONSTRAINT in telegram_source_text
