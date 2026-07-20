from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)


BTN_CREATE_CONTENT = "📝 Создать контент"
BTN_CLIENT_QUESTION = "💬 Вопрос клиента"
BTN_FIND_SIGNALS = "📡 Найти сигналы интереса"
BTN_CHECK_TEXT = "🛡 Проверить текст"
BTN_PACKAGE_MATERIALS = "📦 Упаковать материалы"
BTN_UNSURE = "🧭 Не знаю, куда идти"
BTN_LAST_TASK = "📋 Последняя задача"
BTN_HOW_IT_WORKS = "ℹ️ Как это работает"
BTN_WEB_RESOURCES = "🌐 Веб-ресурсы"
BTN_BACK = "◀️ Назад"


WEB_RESOURCES_BACK = "web_resources_back"

WEB_RESOURCE_LINKS: tuple[tuple[str, str], ...] = (
    ("🏭 Travel Content Factory", "https://factory.vassian-ai.ru"),
    (
        "🤖 AI Travel Assistant",
        "https://assistant.vassian-ai.ru/#features",
    ),
)


CATEGORY_BUTTONS = frozenset({
    BTN_CREATE_CONTENT,
    BTN_CLIENT_QUESTION,
    BTN_CHECK_TEXT,
    BTN_PACKAGE_MATERIALS,
})


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_CREATE_CONTENT), KeyboardButton(text=BTN_CLIENT_QUESTION)],
            [KeyboardButton(text=BTN_FIND_SIGNALS), KeyboardButton(text=BTN_CHECK_TEXT)],
            [KeyboardButton(text=BTN_PACKAGE_MATERIALS), KeyboardButton(text=BTN_UNSURE)],
            [KeyboardButton(text=BTN_LAST_TASK), KeyboardButton(text=BTN_HOW_IT_WORKS)],
            [KeyboardButton(text=BTN_WEB_RESOURCES)],
        ],
        resize_keyboard=True,
    )


def web_resources_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=title, url=url)]
        for title, url in WEB_RESOURCE_LINKS
    ]
    rows.append(
        [InlineKeyboardButton(text=BTN_BACK, callback_data=WEB_RESOURCES_BACK)]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
