from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.handlers.tasks import _send_partner_package
from app.routing.modules import Module
from app.routing.router import RouteDecision
from app.routing.safety import SafetyLevel


def run(value):
    return asyncio.run(value)


class Message:
    def __init__(self):
        self.answers: list[tuple[str, dict]] = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


def decision(task_text="Подготовь материалы для партнёра"):
    return RouteDecision(
        task_text=task_text,
        primary_module=Module.PARTNER_PACKAGING,
        secondary_modules=(),
        safety_level=SafetyLevel.NOT_REQUIRED,
        is_mixed=False,
        is_uncertain=False,
        matched_modules=(Module.PARTNER_PACKAGING,),
        notes=(),
    )


def profile(ta_affiliated, business_name="Тестовое турагентство"):
    return SimpleNamespace(business_name=business_name, ta_affiliated=ta_affiliated)


def repo_with(profile_value):
    return SimpleNamespace(
        get_business_profile=AsyncMock(return_value=profile_value)
    )


def test_ta_affiliated_workspace_keeps_existing_ta_wording():
    message = Message()
    repo = repo_with(profile(True, business_name="Travel Advantage AI Ecosystem"))

    run(_send_partner_package(message, decision(), workspace_id=1, partner_repository=repo))

    repo.get_business_profile.assert_awaited_once_with(1)
    text = message.answers[-1][0]
    assert "Короткое объяснение Travel Advantage" in text
    assert "Travel Advantage" in text


def test_non_ta_workspace_has_no_travel_advantage_mention_and_uses_business_name():
    message = Message()
    repo = repo_with(profile(False, business_name="Тестовое турагентство"))

    run(_send_partner_package(message, decision(), workspace_id=2, partner_repository=repo))

    text = message.answers[-1][0]
    assert "Travel Advantage" not in text
    assert "Travel Content Factory" not in text
    assert "Тестовое турагентство" in text
    assert "Короткое представление вашего бизнеса" in text


def test_missing_profile_fails_closed_to_neutral_template():
    message = Message()
    repo = repo_with(None)

    run(_send_partner_package(message, decision(), workspace_id=3, partner_repository=repo))

    text = message.answers[-1][0]
    assert "Travel Advantage" not in text
    assert "Travel Content Factory" not in text
    assert "Короткое представление вашего бизнеса" in text


def test_variable_terms_warning_still_appended_for_non_ta_workspace():
    message = Message()
    repo = repo_with(profile(False, business_name="Тестовое турагентство"))

    run(_send_partner_package(
        message, decision("Подготовь материалы про цены и оплату"),
        workspace_id=2, partner_repository=repo,
    ))

    text = message.answers[-1][0]
    assert "Обязательный FAQ по переменным условиям" in text
    assert "Travel Advantage" not in text
