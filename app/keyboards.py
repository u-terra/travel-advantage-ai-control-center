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

BTN_V2_ANALYZE_LINK = "🔗 Разобрать ссылку"
BTN_V2_CREATE_MATERIAL = "✍️ Создать материал"
BTN_V2_CLIENT_REPLY = "💬 Ответить клиенту"
BTN_V2_CONTENT_PLAN = "📅 Контент-план"
BTN_V2_CHECK_TEXT = "🛡 Проверить и улучшить текст"
BTN_V2_MATERIALS = "📚 Мои материалы"
BTN_V2_PROFILE = "⚙️ Профиль"
BTN_V2_HELP = "ℹ️ Помощь"
BTN_V2_MAIN_MENU = "⬅️ Главное меню"


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

V2_CATEGORY_BUTTONS = frozenset({BTN_V2_CLIENT_REPLY, BTN_V2_CHECK_TEXT})

V2_PLACEHOLDER_BUTTONS = frozenset({
    BTN_V2_ANALYZE_LINK,
    BTN_V2_CREATE_MATERIAL,
    BTN_V2_CONTENT_PLAN,
    BTN_V2_MATERIALS,
    BTN_V2_PROFILE,
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


def v2_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_V2_ANALYZE_LINK)],
            [KeyboardButton(text=BTN_V2_CREATE_MATERIAL)],
            [KeyboardButton(text=BTN_V2_CLIENT_REPLY)],
            [KeyboardButton(text=BTN_V2_CONTENT_PLAN)],
            [KeyboardButton(text=BTN_V2_CHECK_TEXT)],
            [KeyboardButton(text=BTN_V2_MATERIALS)],
            [KeyboardButton(text=BTN_V2_PROFILE)],
            [KeyboardButton(text=BTN_V2_HELP)],
        ],
        resize_keyboard=True,
    )


def active_main_menu(v2_menu_enabled: bool) -> ReplyKeyboardMarkup:
    return v2_main_menu() if v2_menu_enabled else main_menu()


def v2_back_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_V2_MAIN_MENU)]],
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
