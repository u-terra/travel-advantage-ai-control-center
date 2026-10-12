"""Outreach draft quality (2026-10-12): a shared-contact (team) addressee gets
a team greeting, no empty "в связи с «X»" phrase, only stored facts/hook, no
invented acquaintance or benefit, and a short closing question - in BOTH the
deterministic fallback and the LLM path (whose output is validated)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.services.partner_outreach import (
    build_outreach_draft_prompt,
    fallback_outreach_draft,
    generate_outreach_draft,
)

run = asyncio.run


def _team_contact():
    return {
        "contact_type": "person", "addressee_confirmed": False, "display_name": "Onvisor",
        "role": "", "project_name": "Onvisor", "hook": "Набор группы на Алтай.",
        "evidence_text": "Авторские туры; Групповые поездки", "evidence_url": "https://onvisor.ru/",
    }


@dataclass
class _Cfg:
    api_key: str = "k"


@dataclass
class _Res:
    text: str


class _LLM:
    def __init__(self, text=None, error=None):
        self.config, self._text, self._error, self.prompts = _Cfg(), text, error, []

    def generate(self, *, message):
        self.prompts.append(message)
        if self._error:
            raise self._error
        return _Res(self._text)


_GOOD = (
    "Здравствуйте, команда Onvisor!\n\nУ вас авторские туры и набор группы на Алтай. "
    "Хочу познакомить вас с Travel Advantage - международным туристическим клубом и платформой для путешествий.\n\n"
    "Было бы вам интересно коротко узнать подробнее?"
)


def _assert_clean(text: str) -> None:
    lowered = text.lower()
    assert "в связи с" not in lowered
    for banned in ("(а)", "увидел", "изуч", "выгод", "скидк", "доход", "заработ", "гарант"):
        assert banned not in lowered
    assert text.rstrip().endswith("?")


def test_fallback_for_shared_contact_greets_team_and_uses_stored_facts() -> None:
    text = fallback_outreach_draft(_team_contact(), True)
    assert text.startswith("Здравствуйте, команда Onvisor!")
    assert "Авторские туры" in text and "Набор группы на Алтай" in text
    assert "Обращаюсь к вам" not in text
    _assert_clean(text)


def test_fallback_without_facts_does_not_emit_empty_phrase() -> None:
    contact = {**_team_contact(), "hook": "", "evidence_text": ""}
    text = fallback_outreach_draft(contact, False)
    assert text.startswith("Здравствуйте, команда Onvisor!")
    _assert_clean(text)


def test_fallback_project_only_person_contact_has_no_empty_phrase() -> None:
    contact = {"contact_type": "person", "display_name": "Иванова Мария Петровна",
               "role": "", "project_name": "Клуб Атлас", "evidence_text": ""}
    text = fallback_outreach_draft(contact, True)
    assert text.startswith("Здравствуйте, Мария Петровна!")
    _assert_clean(text)


def test_fallback_person_with_role_keeps_recipient_role_phrase() -> None:
    contact = {"contact_type": "person", "display_name": "Иванова Мария Петровна",
               "role": "гид", "project_name": "Клуб Атлас", "evidence_text": ""}
    assert "Обращаюсь к вам как к гид в «Клуб Атлас»." in fallback_outreach_draft(contact, True)


def test_llm_prompt_carries_team_rule_hook_and_bans() -> None:
    prompt = build_outreach_draft_prompt(_team_contact(), True, None)
    assert 'Начни ровно со строки "Здравствуйте, команда Onvisor!"' in prompt
    assert "Зацепка (из сохранённых данных): Набор группы на Алтай." in prompt
    assert "личное знакомство" in prompt and "обещать выгоду" in prompt
    assert "готовности узнать подробнее" in prompt


def test_prompt_for_named_person_has_no_team_rule() -> None:
    person = {"contact_type": "person", "display_name": "Иванова Мария Петровна", "role": "гид",
              "project_name": "Клуб", "evidence_text": "e"}
    prompt = build_outreach_draft_prompt(person, True, None)
    assert "АДРЕСАТ НЕ ПОДТВЕРЖДЁН" not in prompt


def test_llm_good_draft_is_used() -> None:
    llm = _LLM(_GOOD)
    assert run(generate_outreach_draft(_team_contact(), llm, ta_affiliated=True)) == " ".join(_GOOD.split())
    assert len(llm.prompts) == 1


def test_llm_draft_with_wrong_greeting_falls_back() -> None:
    llm = _LLM("Здравствуйте, Onvisor!\n\nБыло бы вам интересно коротко узнать подробнее?")
    text = run(generate_outreach_draft(_team_contact(), llm, ta_affiliated=True))
    assert text == fallback_outreach_draft(_team_contact(), True)


def test_llm_draft_claiming_acquaintance_or_benefit_falls_back() -> None:
    for bad in (
        "Здравствуйте, команда Onvisor!\n\nЯ изучил ваши материалы. Интересно?",
        "Здравствуйте, команда Onvisor!\n\nУ нас выгодные условия для вас. Интересно?",
    ):
        text = run(generate_outreach_draft(_team_contact(), _LLM(bad), ta_affiliated=True))
        assert text == fallback_outreach_draft(_team_contact(), True)


def test_llm_failure_uses_the_improved_fallback() -> None:
    text = run(generate_outreach_draft(_team_contact(), _LLM(error=TimeoutError("t")), ta_affiliated=True))
    assert text.startswith("Здравствуйте, команда Onvisor!")
    _assert_clean(text)


def test_named_person_llm_draft_is_not_forced_into_team_greeting() -> None:
    person = {"contact_type": "person", "display_name": "Иванова Мария Петровна", "role": "гид",
              "project_name": "Клуб", "evidence_text": "e"}
    text = "Здравствуйте, Мария Петровна!\n\nОбращаюсь к вам как к гиду. Интересно узнать подробнее?"
    assert run(generate_outreach_draft(person, _LLM(text), ta_affiliated=False)) == " ".join(text.split())
