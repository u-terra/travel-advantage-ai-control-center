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
BTN_V2_ANALYZE_MORE = "🔗 Разобрать ещё текст"
BTN_V2_CREATE_MATERIAL = "✍️ Создать материал"
BTN_V2_FIND_SIGNALS = "📡 Найти сигналы и идеи"
BTN_V2_CLIENT_REPLY = "💬 Ответить клиенту"
BTN_V2_CONTENT_PLAN = "📅 Контент-план"
BTN_V2_CHECK_TEXT = "🛡 Проверить и улучшить текст"
BTN_V2_MATERIALS = "📚 Мои материалы"
BTN_V2_SOURCES = "📚 Источники"
BTN_V2_COMPETITORS = "🎯 Мои конкуренты"
BTN_V2_PROFILE = "⚙️ Профиль"
BTN_V2_HELP = "ℹ️ Помощь"
BTN_V2_MAIN_MENU = "⬅️ Главное меню"

SOURCE_TOGGLE_PREFIX = "source_toggle:"
PILOT_CONSENT_ACCEPT_PREFIX = "pilot_consent:accept:"
SOURCE_REGISTRY_ADD = "source_registry:add"
SOURCE_REGISTRY_REFRESH = "source_registry:refresh"

SOURCE_MATERIAL_PREFIX = "source_material:"
SOURCE_MATERIAL_FORMAT_PREFIX = "source_material_format:"
SOURCE_ACTION_ANALYZE_MORE = "source_action:analyze_more"
SOURCE_ACTION_MAIN_MENU = "source_action:main_menu"
ARTIFACT_CHECK_PREFIX = "artifact_check:"
ARTIFACT_REVIEW_SAVE_PREFIX = "artifact_review_save:"
ARTIFACT_REVIEW_KEEP = "artifact_review_keep"
TEXT_REVIEW_SAVE = "text_review_save"
ARTIFACT_OPEN_PREFIX = "artifact_open:"
COMPETITOR_REGISTRY_ADD = "competitor_registry:add"


WEB_RESOURCES_BACK = "web_resources_back"

# Ресурсы Travel Advantage: показываются только workspace с явным признаком
# BusinessProfile.ta_affiliated (не по business_type, названию или описанию —
# см. app/domain/business_profiles.py и app/handlers/menu.py:
# _resolve_web_resource_links). Сторонние независимые workspace эти ссылки
# не должны видеть ни при каких обстоятельствах.
TA_WEB_RESOURCE_LINKS: tuple[tuple[str, str], ...] = (
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

V2_CATEGORY_BUTTONS = frozenset({BTN_V2_CLIENT_REPLY})

V2_PLACEHOLDER_BUTTONS = frozenset({
    BTN_V2_CONTENT_PLAN,
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
            [KeyboardButton(text=BTN_V2_CREATE_MATERIAL)],
            [KeyboardButton(text=BTN_V2_CLIENT_REPLY)],
            [KeyboardButton(text=BTN_V2_FIND_SIGNALS)],
            [KeyboardButton(text=BTN_V2_ANALYZE_LINK)],
            [KeyboardButton(text=BTN_V2_CHECK_TEXT)],
            [KeyboardButton(text=BTN_V2_MATERIALS)],
            [KeyboardButton(text=BTN_V2_SOURCES)],
            [KeyboardButton(text=BTN_V2_COMPETITORS)],
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


def source_analysis_result_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_V2_ANALYZE_MORE)],
            [KeyboardButton(text=BTN_V2_MAIN_MENU)],
        ],
        resize_keyboard=True,
    )


def pilot_consent_keyboard(consent_version: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Я согласен на обработку персональных данных",
            callback_data=f"{PILOT_CONSENT_ACCEPT_PREFIX}{consent_version}",
        )]
    ])


def analyzed_source_keyboard(source_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=BTN_V2_CREATE_MATERIAL,
            callback_data=f"{SOURCE_MATERIAL_PREFIX}{source_id}",
        )],
        [InlineKeyboardButton(
            text=BTN_V2_ANALYZE_MORE, callback_data=SOURCE_ACTION_ANALYZE_MORE
        )],
        [InlineKeyboardButton(
            text=BTN_V2_MAIN_MENU, callback_data=SOURCE_ACTION_MAIN_MENU
        )],
    ])


def source_material_formats_keyboard(source_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Telegram",
                callback_data=f"{SOURCE_MATERIAL_FORMAT_PREFIX}{source_id}:telegram",
            ),
            InlineKeyboardButton(
                text="VK",
                callback_data=f"{SOURCE_MATERIAL_FORMAT_PREFIX}{source_id}:vk",
            ),
        ],
        [InlineKeyboardButton(
            text="Отмена", callback_data=SOURCE_ACTION_MAIN_MENU
        )],
    ])


def material_result_keyboard(artifact_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🛡 Проверить текст",
            callback_data=f"{ARTIFACT_CHECK_PREFIX}{artifact_id}",
        )],
        [InlineKeyboardButton(
            text=BTN_V2_MAIN_MENU, callback_data=SOURCE_ACTION_MAIN_MENU
        )]
    ])


def artifact_review_keyboard(artifact_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Сохранить улучшенную версию",
            callback_data=f"{ARTIFACT_REVIEW_SAVE_PREFIX}{artifact_id}",
        )],
        [InlineKeyboardButton(
            text="❌ Оставить текущую", callback_data=ARTIFACT_REVIEW_KEEP
        )],
    ])


def artifact_review_complete_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="❌ Оставить текущую", callback_data=ARTIFACT_REVIEW_KEEP
        )],
        [InlineKeyboardButton(
            text=BTN_V2_MAIN_MENU, callback_data=SOURCE_ACTION_MAIN_MENU
        )],
    ])


def free_text_review_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="💾 Сохранить как материал", callback_data=TEXT_REVIEW_SAVE
        )],
        [InlineKeyboardButton(
            text=BTN_V2_MAIN_MENU, callback_data=SOURCE_ACTION_MAIN_MENU
        )],
    ])


def sources_registry_keyboard(
    sources: tuple[tuple[str, str, bool], ...],
) -> InlineKeyboardMarkup:
    """Список источников: кнопка на источник переключает enabled.

    Принимает готовые тройки (id, подпись, включён) — клавиатура не знает ни
    про реестр, ни про конкретные каналы.
    """
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"{'🟢' if enabled else '⚪️'} {title}",
                callback_data=f"{SOURCE_TOGGLE_PREFIX}{source_id}",
            )
        ]
        for source_id, title, enabled in sources
    ]
    rows.append(
        [InlineKeyboardButton(
            text="➕ Предложить источник", callback_data=SOURCE_REGISTRY_ADD
        )]
    )
    rows.append(
        [InlineKeyboardButton(
            text=BTN_V2_MAIN_MENU, callback_data=SOURCE_ACTION_MAIN_MENU
        )]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def materials_list_keyboard(
    items: tuple[tuple[int, str], ...],
) -> InlineKeyboardMarkup:
    """Список материалов: кнопка на материал открывает его текущую версию.

    Принимает готовые пары (id, подпись) — клавиатура не знает про Artifact
    и не решает, как формировать подпись.
    """
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=label,
                callback_data=f"{ARTIFACT_OPEN_PREFIX}{artifact_id}",
            )
        ]
        for artifact_id, label in items
    ]
    rows.append(
        [InlineKeyboardButton(
            text=BTN_V2_MAIN_MENU, callback_data=SOURCE_ACTION_MAIN_MENU
        )]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def web_resources_keyboard(
    links: tuple[tuple[str, str], ...],
) -> InlineKeyboardMarkup:
    """Ресурсы, разрешённые для конкретного workspace.

    Клавиатура не решает, какие ссылки допустимы — это делает вызывающий
    код (см. app/handlers/menu.py), исходя из business_type workspace.
    """
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=title, url=url)]
        for title, url in links
    ]
    rows.append(
        [InlineKeyboardButton(text=BTN_BACK, callback_data=WEB_RESOURCES_BACK)]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def competitors_list_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="➕ Добавить конкурента", callback_data=COMPETITOR_REGISTRY_ADD
        )],
        [InlineKeyboardButton(
            text=BTN_V2_MAIN_MENU, callback_data=SOURCE_ACTION_MAIN_MENU
        )],
    ])
