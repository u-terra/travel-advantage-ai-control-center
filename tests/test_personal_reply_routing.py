"""Regression (2026-10-12): "Знакомый говорит: «Travel Advantage — это сетевой
маркетинг…». Подготовь короткий спокойный ответ от моего лица…" was (1) routed
to the INFORMATIONAL persona and, after that was fixed, (2) still answered with
a product reference because the generic client-reply instructions and the
knowledge-base facts in the final prompt overrode the user's request
("предложи следующий шаг", "затем кратко отличия Travel Advantage", safety
"дай общее объяснение и сверь условия", club "объясняя, что это такое…").
A personal-reply request now gets its own spec; ordinary questions keep
their previous behaviour."""

from __future__ import annotations

import asyncio

from app.handlers.tasks import (
    _CLIENT_REPLY_HEADING,
    _INFORMATIONAL_HEADING,
    _PERSONAL_REPLY_HEADING,
    _is_personal_reply_request,
    on_free_text,
)
from app.services.llm.models import ContentDraft
from app.services.material_orchestration import (
    _PERSONAL_REPLY_CONSTRAINTS,
    _PERSONAL_REPLY_OBJECTIVE,
)
from app.services.reference_resolver import ReferenceResolver
from tests.llm_fakes import FakeLLMProvider
from tests.test_journal_handlers import Message, business_profile, context, journal, profile_repository
from tests.test_knowledge_retrieval import service
from tests.test_reference_resolver_integration import CountingKnowledgeService
from tests.test_travel_assistant_informational_mode import run_free_text

_OBJECTION = (
    "Знакомый говорит: “Travel Advantage — это сетевой маркетинг, мне такое неинтересно”. "
    "Подготовь короткий спокойный ответ от моего лица: без спора, давления, обещаний дохода "
    "и попытки переубедить любой ценой"
)

# Instructions of the generic client-reply / knowledge persona that push the
# model toward a product explanation and must NOT be in a personal reply prompt.
_CONFLICTING = (
    "Сформировать короткий личный ответ клиенту",
    "уместные отличия Travel Advantage",
    "предложи уточнить детали",
    "уместно, но должно звучать",
    "Дай только общее объяснение",
    "сверить вручную",
    "Объясняя клиенту, что это такое",
    "ta.platform",
    "canonical_facts",
)


def _source(provider) -> str:
    return provider.generate_draft.call_args.kwargs["source_text"]


def _with_knowledge(tmp_path, text):
    knowledge = CountingKnowledgeService(service(tmp_path))
    message = Message(text)
    provider = FakeLLMProvider(draft=ContentDraft("Понимаю, спасибо что сказали.", ()))
    asyncio.run(on_free_text(
        message, journal(), provider, context(), profile_repository(business_profile()),
        reference_resolver=ReferenceResolver(knowledge),
    ))
    return message, provider


def test_objection_reply_request_is_recognised() -> None:
    assert _is_personal_reply_request(_OBJECTION) is True


def test_objection_reply_request_gets_reply_heading_not_reference_heading() -> None:
    message, _ = run_free_text(_OBJECTION, web_search_service=None)
    text = message.answers[-1][0]
    assert _PERSONAL_REPLY_HEADING in text
    assert _INFORMATIONAL_HEADING not in text


def test_final_prompt_keeps_user_instructions_and_has_no_conflicting_instructions(tmp_path) -> None:
    _, provider = _with_knowledge(tmp_path, _OBJECTION)
    source = _source(provider)
    for fragment in ("от моего лица", "короткий спокойный ответ", "без спора, давления"):
        assert fragment in source          # the user's person / length / tone, verbatim
    assert _PERSONAL_REPLY_OBJECTIVE in source
    for rule in _PERSONAL_REPLY_CONSTRAINTS[:4]:
        assert rule in source
    for conflicting in _CONFLICTING:
        assert conflicting not in source, conflicting
    # the personal objective is the only objective, and constraints come last
    assert source.count("[OBJECTIVE - CONTROL]") == 1


def test_constraints_cover_refusal_handling_without_a_fixed_answer() -> None:
    joined = " ".join(_PERSONAL_REPLY_CONSTRAINTS)
    assert "2-3 коротких предложения" in joined                 # default length, user may override
    assert "не спорь" in joined and "не предлагай рассмотреть продукт" in joined
    assert "Не отрицай существование партнёрской модели" in joined.replace("не отрицай", "Не отрицай")
    assert "Служебные напоминания" in joined
    assert "Понимаю, уговаривать не буду" not in joined         # no canned reply


def test_service_reminder_is_appended_separately_from_the_draft() -> None:
    message, _ = run_free_text(_OBJECTION, web_search_service=None)
    text = message.answers[-1][0]
    assert text.index("Черновик") < text.index("🛡 Safety Layer")


def test_what_is_travel_advantage_still_gets_knowledge_and_client_persona(tmp_path) -> None:
    message, provider = _with_knowledge(tmp_path, "Что такое Travel Advantage?")
    text, source = message.answers[-1][0], _source(provider)
    assert _is_personal_reply_request("Что такое Travel Advantage?") is False
    assert _CLIENT_REPLY_HEADING in text and _PERSONAL_REPLY_HEADING not in text
    assert "ta.platform" in source                       # facts are still supplied
    assert _PERSONAL_REPLY_OBJECTIVE not in source


def test_plain_informational_question_is_still_informational() -> None:
    message, provider = run_free_text(
        "Какие сейчас изменения правил въезда в Индонезию для россиян?", web_search_service=None)
    assert _INFORMATIONAL_HEADING in message.answers[-1][0]
    assert _PERSONAL_REPLY_OBJECTIVE not in _source(provider)


def test_unrelated_requests_are_not_treated_as_personal_replies() -> None:
    for text in (
        "Объясни, как работает Travel Advantage",
        "Подготовь пост о Travel Advantage",
        "Как устроен membership Travel Advantage?",
        "Напиши ответ клиенту про тарифы",
    ):
        assert _is_personal_reply_request(text) is False
