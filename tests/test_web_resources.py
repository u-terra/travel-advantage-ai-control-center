from __future__ import annotations

import asyncio

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
    WEB_RESOURCE_LINKS,
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
    rows = web_resources_keyboard().inline_keyboard
    # Каждый ресурс — отдельная URL-кнопка, открывающая сайт напрямую в браузере.
    for (expected_title, expected_url), row in zip(WEB_RESOURCE_LINKS, rows):
        assert len(row) == 1
        button = row[0]
        assert button.text == expected_title
        assert button.url == expected_url
        assert button.callback_data is None


def test_web_resources_keyboard_has_back_button():
    back_row = web_resources_keyboard().inline_keyboard[-1]
    assert len(back_row) == 1
    back = back_row[0]
    assert back.text == BTN_BACK
    assert back.callback_data == WEB_RESOURCES_BACK
    assert back.url is None


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


def test_on_web_resources_sends_inline_keyboard():
    message = _FakeMessage()
    asyncio.run(on_web_resources(message))

    assert len(message.answers) == 1
    _, markup = message.answers[0]
    assert markup is not None
    assert markup.inline_keyboard[-1][0].callback_data == WEB_RESOURCES_BACK


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
