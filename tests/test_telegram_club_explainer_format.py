"""Regression tests for a Telegram-only UX/format fix.

Confirmed in a live Telegram dialogue: asked "Что такое Travel Advantage и
как рассказать об этом клиенту?", ORCHESTRAVEL's Telegram answer was
already CORRECT in substance (club/membership framing - see
tests/test_club_membership_framing.py for that separate grounding fix) but
too terse, ending with an offer to shorten it further ("могу помочь
сформулировать ещё короче?") instead of giving one immediately usable
answer. Web's /api/chat and /api/client-reply were already fine and are
untouched by this fix.

Root cause here is pure FORMAT, in the Telegram-only generation layer:
app.handlers.tasks._maybe_send_draft already resolves this question to
either build_informational_generation_spec or build_client_reply_
generation_spec (app.services.material_orchestration) depending on exact
phrasing - this fix appends two extra, narrowly-scoped constraints
(_TA_CLUB_EXPLAINER_FORMAT_CONSTRAINTS) on top of whichever spec was
already built, requiring a self-contained 3-part answer (framing / ready
client phrasing / "Важно" caveats) and banning the "let me shorten it"
tail. Trigger is app.handlers.tasks._is_travel_advantage_client_explainer_
intent() - topic (Travel Advantage/membership/членство) + an explicit
"клиент" mention or a "в чём смысл" framing - deliberately NOT a bare
"объясни"/"что такое" alone, which would also catch:
- the plain grounding-structure question ("Что такое Travel Advantage?",
  tests/test_grounded_knowledge_generation.py's test_A_*), and
- an existing member's own benefit question ("Объясни... зачем мне
  сохранять членство и что я получаю?", tests/test_knowledge_generation_
  context_membership_value.py) - a different, already-tightly-budgeted
  answer shape this fix must not reopen.

None of app/services/knowledge_service.py, app/services/knowledge_
generation_context.py, or app/web_api.py are touched by this fix -
app.handlers.tasks.py is Telegram-only (Web's /api/chat and /api/client-
reply build their GenerationSpec directly in app/web_api.py and never call
this module's _maybe_send_draft), so Web's answers are provably unaffected.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.handlers.tasks import (
    _is_travel_advantage_client_explainer_intent,
    on_task_after_button,
)
from app.routing.modules import Module
from app.services.llm.models import ContentDraft
from app.services.material_orchestration import _TA_CLUB_EXPLAINER_FORMAT_CONSTRAINTS
from app.services.reference_resolver import ReferenceResolver
from tests.llm_fakes import FakeLLMProvider
from tests.test_journal_handlers import (
    Message, State, business_profile, context, journal, profile_repository,
)
from tests.test_knowledge_retrieval import service as _build_knowledge_service
from tests.test_reference_resolver_integration import CountingKnowledgeService

QUESTION = "Объясни просто: что такое Travel Advantage и как рассказать об этом клиенту?"
QUESTION_EXPLAIN_TO_CLIENT = "Как объяснить клиенту Travel Advantage?"
QUESTION_MEMBERSHIP_MEANING = "В чём смысл membership?"
QUESTION_WHAT_TO_TELL_CLIENT = "Что сказать клиенту про Travel Advantage?"

# Must NOT trigger the new format - covered by their own, separately
# budget-tuned tests (see module docstring above).
QUESTION_BARE_DEFINITION = "Что такое Travel Advantage?"
QUESTION_OWN_MEMBERSHIP_BENEFIT = (
    "Я уже плачу за Travel Advantage каждый месяц. Объясни простыми словами, "
    "зачем мне сохранять членство и что конкретно я от него получаю?"
)


def run(value):
    return asyncio.run(value)


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


# ── intent detector unit coverage ───────────────────────────────────────────

def test_detector_fires_on_all_four_target_phrasings() -> None:
    for question in (
        QUESTION, QUESTION_EXPLAIN_TO_CLIENT,
        QUESTION_MEMBERSHIP_MEANING, QUESTION_WHAT_TO_TELL_CLIENT,
    ):
        assert _is_travel_advantage_client_explainer_intent(question), question


def test_detector_does_not_fire_on_bare_definition_or_own_benefit_question() -> None:
    assert not _is_travel_advantage_client_explainer_intent(QUESTION_BARE_DEFINITION)
    assert not _is_travel_advantage_client_explainer_intent(QUESTION_OWN_MEMBERSHIP_BENEFIT)


def test_detector_ignores_unrelated_questions() -> None:
    assert not _is_travel_advantage_client_explainer_intent(
        "Какие сейчас изменения правил въезда в Индонезию для россиян?",
    )


# ── a) club/membership framing still present (format fix doesn't regress content) ──

def test_a_response_still_carries_club_membership_framing(tmp_path: Path) -> None:
    from app.services.knowledge_generation_context import _CLUB_MEMBERSHIP_CONSTRAINT
    source_text = _telegram_source_text(tmp_path, QUESTION)
    assert _CLUB_MEMBERSHIP_CONSTRAINT in source_text


# ── b) the prompt requires a ready-to-send client formulation ──────────────

def test_b_prompt_requires_a_ready_client_formulation(tmp_path: Path) -> None:
    source_text = _telegram_source_text(tmp_path, QUESTION)
    assert _TA_CLUB_EXPLAINER_FORMAT_CONSTRAINTS[0] in source_text
    assert "готовая формулировка для клиента" in source_text


# ── c) the prompt requires the "не обещай гарантированную выгоду" caveat ───

def test_c_prompt_requires_the_no_guaranteed_savings_caveat(tmp_path: Path) -> None:
    source_text = _telegram_source_text(tmp_path, QUESTION)
    assert "гарантированную экономию" in source_text
    assert "всегда дешевле" in source_text


# ── d) the prompt bans the "let me shorten it further" tail ────────────────

def test_d_prompt_bans_the_offer_to_shorten_tail(tmp_path: Path) -> None:
    source_text = _telegram_source_text(tmp_path, QUESTION)
    assert _TA_CLUB_EXPLAINER_FORMAT_CONSTRAINTS[1] in source_text
    assert "сформулировать короче" in source_text
    assert "целиком уже в этом сообщении" in source_text


# ── all four target phrasings actually carry the full format fix end-to-end ─

def test_all_four_target_phrasings_get_the_format_constraints(tmp_path: Path) -> None:
    for question in (
        QUESTION, QUESTION_EXPLAIN_TO_CLIENT,
        QUESTION_MEMBERSHIP_MEANING, QUESTION_WHAT_TO_TELL_CLIENT,
    ):
        source_text = _telegram_source_text(tmp_path, question)
        for constraint in _TA_CLUB_EXPLAINER_FORMAT_CONSTRAINTS:
            assert constraint in source_text, question
        assert len(source_text) <= 6000, question


# ── guard: the two excluded question shapes keep their own prior behavior ──

def test_bare_definition_question_does_not_get_the_new_format(tmp_path: Path) -> None:
    source_text = _telegram_source_text(tmp_path, QUESTION_BARE_DEFINITION)
    for constraint in _TA_CLUB_EXPLAINER_FORMAT_CONSTRAINTS:
        assert constraint not in source_text
    # Grounding (ta.platform's structured facts) must still reach the model
    # whole - this is exactly what tests/test_grounded_knowledge_generation.py
    # ::test_A_* already assert in detail; this is just a smoke check that
    # this fix did not reopen that budget fight.
    assert "ta.platform" in source_text


def test_own_membership_benefit_question_does_not_get_the_new_format(tmp_path: Path) -> None:
    source_text = _telegram_source_text(tmp_path, QUESTION_OWN_MEMBERSHIP_BENEFIT)
    for constraint in _TA_CLUB_EXPLAINER_FORMAT_CONSTRAINTS:
        assert constraint not in source_text
    assert "[SOURCE FACTS - DATA]" in source_text
