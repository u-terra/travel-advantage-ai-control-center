from __future__ import annotations

import asyncio
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock

from app.domain.business_profiles import BusinessContext, BusinessProfile
from app.domain.partners import WorkspaceContext
from app.handlers.menu import on_web_resources, on_web_resources_back
from app.keyboards import (
    BTN_BACK,
    BTN_CHECK_TEXT,
    BTN_CLIENT_QUESTION,
    BTN_CREATE_CONTENT,
    BTN_FIND_SIGNALS,
    BTN_HOW_IT_WORKS,
    BTN_LAST_TASK,
    BTN_PACKAGE_MATERIALS,
    BTN_UNSURE,
    BTN_WEB_RESOURCES,
    TA_WEB_RESOURCE_LINKS,
    WEB_RESOURCES_BACK,
    main_menu,
    web_resources_keyboard,
)


def _reply_button_texts(markup) -> list[str]:
    return [button.text for row in markup.keyboard for button in row]


def test_main_menu_keeps_all_existing_buttons():
    texts = _reply_button_texts(main_menu())
    for expected in (
        BTN_CREATE_CONTENT,
        BTN_CLIENT_QUESTION,
        BTN_FIND_SIGNALS,
        BTN_CHECK_TEXT,
        BTN_PACKAGE_MATERIALS,
        BTN_UNSURE,
        BTN_LAST_TASK,
        BTN_HOW_IT_WORKS,
    ):
        assert expected in texts


def test_main_menu_has_web_resources_button():
    assert BTN_WEB_RESOURCES in _reply_button_texts(main_menu())


def test_web_resources_keyboard_has_url_buttons():
    rows = web_resources_keyboard(TA_WEB_RESOURCE_LINKS).inline_keyboard
    # Каждый ресурс — отдельная URL-кнопка, открывающая сайт напрямую в браузере.
    for (expected_title, expected_url), row in zip(TA_WEB_RESOURCE_LINKS, rows):
        assert len(row) == 1
        button = row[0]
        assert button.text == expected_title
        assert button.url == expected_url
        assert button.callback_data is None


def test_web_resources_keyboard_has_back_button():
    back_row = web_resources_keyboard(TA_WEB_RESOURCE_LINKS).inline_keyboard[-1]
    assert len(back_row) == 1
    back = back_row[0]
    assert back.text == BTN_BACK
    assert back.callback_data == WEB_RESOURCES_BACK
    assert back.url is None


def test_web_resources_keyboard_with_no_links_still_has_back_button():
    """Пустой список ссылок — валидный ввод: клавиатура не должна падать или
    подставлять какие-то ссылки по умолчанию."""
    markup = web_resources_keyboard(())
    assert len(markup.inline_keyboard) == 1
    assert markup.inline_keyboard[0][0].callback_data == WEB_RESOURCES_BACK


class _FakeMessage:
    def __init__(self) -> None:
        self.answers: list[tuple[str, object]] = []
        self.reply_markup_edits: list[object] = []

    async def answer(self, text, reply_markup=None, **kwargs):
        self.answers.append((text, reply_markup))

    async def edit_reply_markup(self, reply_markup=None):
        self.reply_markup_edits.append(reply_markup)


class _FakeCallback:
    def __init__(self, message) -> None:
        self.message = message
        self.answered = False

    async def answer(self, *args, **kwargs):
        self.answered = True


def _profile(
    business_type: str, *,
    ta_affiliated: bool = False,
    public_contacts: dict[str, str] | None = None,
) -> BusinessProfile:
    return BusinessProfile(
        1, 42, "Test Business", business_type, "Описание", "usable", 1, 1,
        BusinessContext(
            specializations=(), destinations=(), audiences=(), markets=(),
            positioning=MappingProxyType({}),
            communication=MappingProxyType({}),
            goals=(),
            content_preferences=MappingProxyType({}),
            public_contacts=MappingProxyType(public_contacts or {}),
            claims=(),
        ),
        "now", "now",
        ta_affiliated=ta_affiliated,
    )


def _partner_repository(profile: BusinessProfile | None):
    return SimpleNamespace(get_business_profile=AsyncMock(return_value=profile))


def _workspace_context(workspace_id: int) -> WorkspaceContext:
    return WorkspaceContext(100, workspace_id, "owner", "active")


def _urls(markup) -> list[str]:
    return [
        button.url
        for row in markup.inline_keyboard[:-1]
        for button in row
    ]


def test_on_web_resources_sends_ta_links_to_ta_affiliated_workspace():
    """Изоляция, случай 1: workspace с явным признаком ta_affiliated=True
    (реальный Travel Advantage workspace) видит свои разрешённые TA-ссылки."""
    message = _FakeMessage()
    repository = _partner_repository(_profile("club_partner", ta_affiliated=True))

    asyncio.run(on_web_resources(message, _workspace_context(1), repository))

    assert len(message.answers) == 1
    _, markup = message.answers[0]
    assert _urls(markup) == [url for _, url in TA_WEB_RESOURCE_LINKS]
    assert markup.inline_keyboard[-1][0].callback_data == WEB_RESOURCES_BACK


def test_on_web_resources_hides_ta_links_from_non_affiliated_club_partner():
    """business_type == "club_partner" сам по себе не означает аффилиацию с
    Travel Advantage: сторонний клуб/партнёр с таким business_type, но без
    ta_affiliated, не должен получать TA-ссылки."""
    message = _FakeMessage()
    repository = _partner_repository(_profile("club_partner", ta_affiliated=False))

    asyncio.run(on_web_resources(message, _workspace_context(4), repository))

    assert len(message.answers) == 1
    text, markup = message.answers[0]
    assert _urls(markup) == []
    for _, ta_url in TA_WEB_RESOURCE_LINKS:
        assert ta_url not in _urls(markup)
        assert ta_url not in text


def test_on_web_resources_hides_ta_links_from_independent_workspace():
    """Изоляция, случай 2: сторонний (независимый) workspace не должен видеть
    ссылки Travel Advantage — только собственные ссылки своего профиля, без
    какого-либо глобального fallback на TA."""
    message = _FakeMessage()
    repository = _partner_repository(
        _profile(
            "independent_agent",
            public_contacts={"website": "https://my-agency.example.com"},
        )
    )

    asyncio.run(on_web_resources(message, _workspace_context(2), repository))

    assert len(message.answers) == 1
    text, markup = message.answers[0]
    assert _urls(markup) == ["https://my-agency.example.com"]
    for _, ta_url in TA_WEB_RESOURCE_LINKS:
        assert ta_url not in _urls(markup)
    assert "vassian-ai.ru" not in text


def test_on_web_resources_empty_profile_gets_own_empty_state_not_ta_fallback():
    """Пустой профиль стороннего workspace получает понятное сообщение, а не
    ссылки чужого (Travel Advantage) workspace."""
    message = _FakeMessage()
    repository = _partner_repository(_profile("independent_agent"))

    asyncio.run(on_web_resources(message, _workspace_context(3), repository))

    assert len(message.answers) == 1
    text, markup = message.answers[0]
    assert "не настроены" in text.lower()
    for _, ta_url in TA_WEB_RESOURCE_LINKS:
        assert ta_url not in text
    assert markup.inline_keyboard[-1][0].callback_data == WEB_RESOURCES_BACK


def test_on_web_resources_without_workspace_context_shows_empty_state():
    message = _FakeMessage()
    repository = _partner_repository(None)

    asyncio.run(on_web_resources(message, None, repository))

    text, _ = message.answers[0]
    assert "не настроены" in text.lower()


def test_back_returns_to_main_menu():
    message = _FakeMessage()
    callback = _FakeCallback(message)
    asyncio.run(on_web_resources_back(callback))

    assert callback.answered is True
    # Инлайн-клавиатура убирается, а пользователь возвращается в главное меню.
    assert message.reply_markup_edits == [None]
    assert len(message.answers) == 1
    _, markup = message.answers[0]
    assert BTN_WEB_RESOURCES in _reply_button_texts(markup)
