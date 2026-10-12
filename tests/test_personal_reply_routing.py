"""Regression (2026-10-12): "Знакомый говорит: «Travel Advantage — это сетевой
маркетинг…». Подготовь короткий спокойный ответ от моего лица…" was routed to
the INFORMATIONAL persona (the brand name is a router topic keyword), which
answered with a reference article about the club instead of a ready reply.
A request to prepare a reply in the user's own voice must reach reply-draft
generation, keeping the requested person/length/tone; ordinary questions such
as "Что такое Travel Advantage?" must keep their previous behaviour."""

from __future__ import annotations

from app.handlers.tasks import (
    _CLIENT_REPLY_HEADING,
    _INFORMATIONAL_HEADING,
    _PERSONAL_REPLY_CONSTRAINT,
    _PERSONAL_REPLY_HEADING,
    _is_personal_reply_request,
)
from tests.test_travel_assistant_informational_mode import run_free_text

_OBJECTION = (
    "Знакомый говорит: “Travel Advantage — это сетевой маркетинг, мне такое неинтересно”. "
    "Подготовь короткий спокойный ответ от моего лица: без спора, давления, обещаний дохода "
    "и попытки переубедить любой ценой"
)


def _source(provider) -> str:
    return provider.generate_draft.call_args.kwargs["source_text"]


def test_objection_reply_request_is_recognised() -> None:
    assert _is_personal_reply_request(_OBJECTION) is True


def test_objection_reply_request_gets_reply_heading_not_reference_heading() -> None:
    message, _ = run_free_text(_OBJECTION, web_search_service=None)
    text = message.answers[-1][0]
    assert _PERSONAL_REPLY_HEADING in text
    assert _INFORMATIONAL_HEADING not in text


def test_objection_reply_request_keeps_user_instructions_and_reply_constraint() -> None:
    _, provider = run_free_text(_OBJECTION, web_search_service=None)
    source = _source(provider)
    # the user's own person / length / tone instructions reach the model verbatim
    for fragment in ("от моего лица", "короткий спокойный ответ", "без спора, давления"):
        assert fragment in source
    assert _PERSONAL_REPLY_CONSTRAINT[:60] in source
    # reply persona, not the "answer the user's own question directly" persona
    assert "ответ travel-ассистента самому пользователю" not in source.lower()


def test_personal_reply_constraint_forbids_reference_dump_and_denying_the_model() -> None:
    text = _PERSONAL_REPLY_CONSTRAINT
    assert "ОДИН короткий текст реплики" in text
    assert "не пересказывай справку" in text
    assert "не отрицай существование партнёрской модели" in text
    assert "не придумывай условия" in text
    assert "Служебные напоминания не включай в текст реплики" in text


def test_service_reminder_is_separate_from_the_sendable_text() -> None:
    message, _ = run_free_text(_OBJECTION, web_search_service=None)
    text = message.answers[-1][0]
    body, _, reminder = text.partition("🛡 Safety Layer")
    assert "Черновик" in body  # the draft text sits before the reminder
    assert reminder == "" or reminder.startswith(":")


def test_what_is_travel_advantage_keeps_its_previous_behaviour() -> None:
    message, provider = run_free_text("Что такое Travel Advantage?", web_search_service=None)
    text = message.answers[-1][0]
    assert _is_personal_reply_request("Что такое Travel Advantage?") is False
    assert _CLIENT_REPLY_HEADING in text and _PERSONAL_REPLY_HEADING not in text
    assert _PERSONAL_REPLY_CONSTRAINT[:60] not in _source(provider)


def test_plain_informational_question_is_still_informational() -> None:
    message, provider = run_free_text(
        "Какие сейчас изменения правил въезда в Индонезию для россиян?", web_search_service=None)
    assert _INFORMATIONAL_HEADING in message.answers[-1][0]
    assert _PERSONAL_REPLY_CONSTRAINT[:60] not in _source(provider)


def test_unrelated_requests_are_not_treated_as_personal_replies() -> None:
    for text in (
        "Объясни, как работает Travel Advantage",
        "Подготовь пост о Travel Advantage",
        "Как устроен membership Travel Advantage?",
        "Напиши ответ клиенту про тарифы",
    ):
        assert _is_personal_reply_request(text) is False
