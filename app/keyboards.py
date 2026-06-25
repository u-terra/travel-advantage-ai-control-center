from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


BTN_CREATE_CONTENT = "📝 Создать контент"
BTN_CLIENT_QUESTION = "💬 Вопрос клиента"
BTN_FIND_SIGNALS = "📡 Найти сигналы интереса"
BTN_CHECK_TEXT = "🛡 Проверить текст"
BTN_PACKAGE_MATERIALS = "📦 Упаковать материалы"
BTN_UNSURE = "🧭 Не знаю, куда идти"
BTN_LAST_TASK = "📋 Последняя задача"
BTN_HOW_IT_WORKS = "ℹ️ Как это работает"


CATEGORY_BUTTONS = frozenset({
    BTN_CREATE_CONTENT,
    BTN_CLIENT_QUESTION,
    BTN_FIND_SIGNALS,
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
        ],
        resize_keyboard=True,
    )
