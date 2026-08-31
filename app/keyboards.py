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

BTN_V2_DAILY_ACTIONS = "☀️ Что делать сегодня"
BTN_V2_ANALYZE_LINK = "📝 Разобрать публикацию"
BTN_V2_ANALYZE_MORE = "📝 Разобрать ещё текст"
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

# Stage 3A: pre-subscription lobby — доступны без workspace и без подписки.
BTN_LOBBY_BROWSE = "🧭 Осмотреться"
BTN_LOBBY_TRY_14_DAYS = "🚀 Попробовать 14 дней"
BTN_LOBBY_SUBSCRIBE_MONTH = "💳 Подключить на месяц"
BTN_LOBBY_WHATS_INCLUDED = "ℹ️ Что входит в подписку"
BTN_LOBBY_EXTEND_ACCESS = "🔄 Продлить доступ"
BTN_LOBBY_BACK = "⬅️ Назад в лобби"

BROWSE_SECTION_PREFIX = "browse:section:"
BROWSE_BACK = "browse:back"

SOURCE_TOGGLE_PREFIX = "source_toggle:"
PILOT_CONSENT_ACCEPT_PREFIX = "pilot_consent:accept:"
SOURCE_REGISTRY_ADD = "source_registry:add"
SOURCE_REGISTRY_REFRESH = "source_registry:refresh"

SOURCE_MATERIAL_PREFIX = "source_material:"
SOURCE_MATERIAL_FORMAT_PREFIX = "source_material_format:"
SOURCE_ACTION_ANALYZE_MORE = "source_action:analyze_more"
SOURCE_ACTION_MAIN_MENU = "source_action:main_menu"

# Uncertain-route fallback (review fix): подтверждение перед разбором
# длинного текста как публикации — см. on_confirm_publication_analysis в
# app/handlers/tasks.py. "Выбрать другую задачу" переиспользует
# SOURCE_ACTION_MAIN_MENU/source_main_menu — тот же хендлер уже делает
# state.clear() и показывает главное меню.
TASK_CONFIRM_PUBLICATION_ANALYSIS = "task_action:confirm_publication_analysis"
BTN_V2_CONFIRM_PUBLICATION_ANALYSIS = "🔎 Разобрать публикацию"
BTN_V2_CHOOSE_ANOTHER_TASK = "⬅️ Выбрать другую задачу"
ARTIFACT_CHECK_PREFIX = "artifact_check:"
ARTIFACT_REVIEW_SAVE_PREFIX = "artifact_review_save:"
ARTIFACT_REVIEW_KEEP = "artifact_review_keep"
TEXT_REVIEW_SAVE = "text_review_save"
ARTIFACT_OPEN_PREFIX = "artifact_open:"
# Content → work memory: фиксация факта публикации/использования Artifact
# (app/services/content_usage.py). Живут в material_result_keyboard, общей
# для всех Content entry points — не отдельная параллельная клавиатура.
ARTIFACT_MARK_USED_PREFIX = "artifact_used:"
ARTIFACT_KEEP_LATER_PREFIX = "artifact_later:"
COMPETITOR_REGISTRY_ADD = "competitor_registry:add"
# Stage 3.2: minimal rename action - callback_data carries the competitor id
# so the handler can look it up scoped to the caller's own workspace.
COMPETITOR_REGISTRY_RENAME_PREFIX = "competitor_registry:rename:"
COMPETITOR_OPEN_PREFIX = "competitor:open:"
COMPETITOR_ANALYZE_PREFIX = "competitor:analyze:"
COMPETITOR_NEWS_PREFIX = "competitor:news:"
COMPETITOR_IDEAS_PREFIX = "competitor:ideas:"
COMPETITOR_CREATE_PREFIX = "competitor:create:"
COMPETITOR_REFRESH_PREFIX = "competitor:refresh:"
MATERIAL_ENTRY_ANALYZE = "material_entry:analyze"
MATERIAL_ENTRY_FIND_SIGNALS = "material_entry:find_signals"

# «☀️ Что делать сегодня»: карточка НЕ знает про WorkItem — вызывающий код
# (app/handlers/daily_actions.py) уже классифицировал строку в один из трёх
# бакетов (active_dialog / due_follow_up / waiting_not_due) и просто передаёт
# id + бакет. daily_action_keyboard ниже решает, какой набор кнопок показать.
DAILY_ACTION_PROMPT_PREFIX = "daily_action:prompt:"
DAILY_ACTION_REPLIED_PREFIX = "daily_action:replied:"
DAILY_ACTION_SNOOZE_PREFIX = "daily_action:snooze:"
DAILY_ACTION_DONE_PREFIX = "daily_action:done:"
DAILY_ACTION_DISMISS_PREFIX = "daily_action:dismiss:"

# Кнопки под свежесгенерированным черновиком Reply flow. Каждая несёт
# work_item_id И revision (optimistic-версия из updated_at, см.
# app.domain.work.work_item_revision) — новый черновик для того же item
# обновляет updated_at, поэтому кнопки под предыдущим, уже неактуальным
# черновиком отличаются revision'ом и отклоняются как устаревшие (см.
# app/handlers/daily_actions.py:_parse_reply_confirm_callback). Свой
# REPLY_CONFIRM_DISMISS_PREFIX, а не DAILY_ACTION_DISMISS_PREFIX — тот не
# несёт revision и не годится для этого guard'а.
REPLY_CONFIRM_SENT_PREFIX = "reply_confirm:sent:"
REPLY_CONFIRM_LATER_PREFIX = "reply_confirm:later:"
REPLY_CONFIRM_DISMISS_PREFIX = "reply_confirm:dismiss:"

# Business Onboarding: выбор business_type только для independent-сценария
# (TA workspace его не проходит — ta_affiliated/business_type там уже заданы
# провижининга). Значение в callback_data — одно из тех же BUSINESS_TYPES,
# что уже принимает BusinessProfileService, никакой новой номенклатуры.
ONBOARDING_BUSINESS_TYPE_PREFIX = "onboarding:business_type:"

# Stage 3B1: self-service «⚙️ Профиль и персонализация» — то же самое поле
# business_type редактируется тем же ONBOARDING_BUSINESS_TYPE_PREFIX/
# onboarding_business_type_keyboard(), новой номенклатуры не заводим.
PROFILE_MENU_PREFIX = "profile_menu:"
PROFILE_FIELD_PREFIX = "profile_field:"
PROFILE_EXAMPLES_PREFIX = "profile_examples:"


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
            [KeyboardButton(text=BTN_V2_DAILY_ACTIONS)],
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


def lobby_new_visitor_keyboard() -> ReplyKeyboardMarkup:
    """Новый посетитель без workspace: знакомство + два неактивных пока
    платных варианта + пояснение — без активного рабочего доступа."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_LOBBY_BROWSE)],
            [KeyboardButton(text=BTN_LOBBY_TRY_14_DAYS)],
            [KeyboardButton(text=BTN_LOBBY_SUBSCRIBE_MONTH)],
            [KeyboardButton(text=BTN_LOBBY_WHATS_INCLUDED)],
        ],
        resize_keyboard=True,
    )


def lobby_expired_keyboard() -> ReplyKeyboardMarkup:
    """У workspace был рабочий доступ, но он закончился."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_LOBBY_BROWSE)],
            [KeyboardButton(text=BTN_LOBBY_EXTEND_ACCESS)],
            [KeyboardButton(text=BTN_LOBBY_WHATS_INCLUDED)],
        ],
        resize_keyboard=True,
    )


def browse_sections_keyboard() -> InlineKeyboardMarkup:
    """🧭 Осмотреться — публичный read-only раздел, доступен без workspace."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🚀 С чего начать", callback_data=f"{BROWSE_SECTION_PREFIX}start",
        )],
        [InlineKeyboardButton(
            text="📡 Сигналы и идеи", callback_data=f"{BROWSE_SECTION_PREFIX}signals",
        )],
        [InlineKeyboardButton(
            text="✍️ Создание материалов", callback_data=f"{BROWSE_SECTION_PREFIX}materials",
        )],
        [InlineKeyboardButton(
            text="💬 Ответы клиентам", callback_data=f"{BROWSE_SECTION_PREFIX}replies",
        )],
        [InlineKeyboardButton(
            text="📚 Источники", callback_data=f"{BROWSE_SECTION_PREFIX}sources",
        )],
        [InlineKeyboardButton(
            text="👀 Конкуренты", callback_data=f"{BROWSE_SECTION_PREFIX}competitors",
        )],
        [InlineKeyboardButton(
            text="⚙️ Профиль и персонализация", callback_data=f"{BROWSE_SECTION_PREFIX}profile",
        )],
        [InlineKeyboardButton(
            text="💳 Как устроен доступ", callback_data=f"{BROWSE_SECTION_PREFIX}access",
        )],
        [InlineKeyboardButton(
            text="🖥 Свой сервер или подписка", callback_data=f"{BROWSE_SECTION_PREFIX}hosting",
        )],
        [InlineKeyboardButton(
            text="❓ Частые вопросы", callback_data=f"{BROWSE_SECTION_PREFIX}faq",
        )],
    ])


def browse_section_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=BTN_BACK, callback_data=BROWSE_BACK)],
    ])


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


def uncertain_route_publication_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=BTN_V2_CONFIRM_PUBLICATION_ANALYSIS,
            callback_data=TASK_CONFIRM_PUBLICATION_ANALYSIS,
        )],
        [InlineKeyboardButton(
            text=BTN_V2_CHOOSE_ANOTHER_TASK, callback_data=SOURCE_ACTION_MAIN_MENU
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
            text="✅ Опубликовал",
            callback_data=f"{ARTIFACT_MARK_USED_PREFIX}{artifact_id}",
        )],
        [InlineKeyboardButton(
            text="📌 Оставить на потом",
            callback_data=f"{ARTIFACT_KEEP_LATER_PREFIX}{artifact_id}",
        )],
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


def material_entry_keyboard() -> InlineKeyboardMarkup:
    """Выбор способа создания материала — inline, а не reply-кнопки.

    Callback нельзя ввести свободным текстом, поэтому выбор здесь не может
    случайно провалиться в общий текстовый роутинг (и, как следствие,
    в техническую карточку маршрута): нажатие однозначно ведёт в тот же
    handler, что и прямая кнопка главного меню.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=BTN_V2_ANALYZE_LINK, callback_data=MATERIAL_ENTRY_ANALYZE,
        )],
        [InlineKeyboardButton(
            text=BTN_V2_FIND_SIGNALS, callback_data=MATERIAL_ENTRY_FIND_SIGNALS,
        )],
    ])


def competitors_list_keyboard(
    competitors: tuple[tuple[int, str], ...] = (),
) -> InlineKeyboardMarkup:
    """Workspace competitor list; each saved entity opens its working card."""
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            text=f"🎯 {label}",
            callback_data=f"{COMPETITOR_OPEN_PREFIX}{competitor_id}",
        )]
        for competitor_id, label in competitors
    ]
    rows.append([InlineKeyboardButton(
        text="➕ Добавить конкурента", callback_data=COMPETITOR_REGISTRY_ADD
    )])
    rows.append([InlineKeyboardButton(
        text=BTN_V2_MAIN_MENU, callback_data=SOURCE_ACTION_MAIN_MENU
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def competitor_card_keyboard(competitor_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 Анализ конкурента", callback_data=f"{COMPETITOR_ANALYZE_PREFIX}{competitor_id}")],
        [InlineKeyboardButton(text="📰 Что нового", callback_data=f"{COMPETITOR_NEWS_PREFIX}{competitor_id}")],
        [InlineKeyboardButton(text="💡 Идеи для постов", callback_data=f"{COMPETITOR_IDEAS_PREFIX}{competitor_id}")],
        [InlineKeyboardButton(text=BTN_V2_CREATE_MATERIAL, callback_data=f"{COMPETITOR_CREATE_PREFIX}{competitor_id}")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"{COMPETITOR_REFRESH_PREFIX}{competitor_id}")],
        [InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"{COMPETITOR_REGISTRY_RENAME_PREFIX}{competitor_id}")],
    ])


def competitor_opportunities_keyboard(
    competitor_id: int, opportunities: tuple[tuple[str, str], ...],
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"✍️ {label[:45]}",
            callback_data=f"{COMPETITOR_CREATE_PREFIX}{competitor_id}:{opportunity_id}",
        )]
        for opportunity_id, label in opportunities
    ])


def daily_action_keyboard(work_item_id: int, bucket: str) -> InlineKeyboardMarkup:
    """Кнопки под одной карточкой work_item в «Что делать сегодня».

    ``bucket`` — уже принятое вызывающим кодом решение о том, к какому из
    трёх состояний относится строка (active_dialog / due_follow_up /
    waiting_not_due), а не сырое поле WorkItem — набор кнопок для каждого
    состояния разный и зафиксирован продуктовым решением, не выводится
    здесь заново из lifecycle/loop_state.
    """
    if bucket == "active_dialog":
        rows = [
            ("💬 Продолжить разговор", DAILY_ACTION_PROMPT_PREFIX),
            ("✅ Завершить", DAILY_ACTION_DONE_PREFIX),
            ("🚫 Не актуально", DAILY_ACTION_DISMISS_PREFIX),
        ]
    elif bucket == "due_follow_up":
        rows = [
            ("💬 Написать снова", DAILY_ACTION_PROMPT_PREFIX),
            ("⏭ Перенести", DAILY_ACTION_SNOOZE_PREFIX),
            ("✅ Завершить", DAILY_ACTION_DONE_PREFIX),
        ]
    elif bucket == "waiting_not_due":
        rows = [
            ("💬 Ответил(а)", DAILY_ACTION_REPLIED_PREFIX),
            ("✅ Завершить", DAILY_ACTION_DONE_PREFIX),
            ("🚫 Не актуально", DAILY_ACTION_DISMISS_PREFIX),
        ]
    else:
        raise ValueError(f"Неизвестный bucket для daily_action_keyboard: {bucket!r}")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=f"{prefix}{work_item_id}")]
        for text, prefix in rows
    ])


def onboarding_business_type_keyboard() -> InlineKeyboardMarkup:
    """Выбор типа бизнеса только для independent-сценария Business Onboarding.
    Никакого TA/Carbon/Travel Advantage текста — нейтральные формулировки."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="👤 Независимый турагент",
            callback_data=f"{ONBOARDING_BUSINESS_TYPE_PREFIX}independent_agent",
        )],
        [InlineKeyboardButton(
            text="🏢 Турагентство",
            callback_data=f"{ONBOARDING_BUSINESS_TYPE_PREFIX}agency",
        )],
        [InlineKeyboardButton(
            text="🏢 Турфирма",
            callback_data=f"{ONBOARDING_BUSINESS_TYPE_PREFIX}travel_company",
        )],
    ])


# ── Stage 3B1: ⚙️ Профиль и персонализация (self-service) ───────────────────


def profile_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🏢 Профиль компании", callback_data=f"{PROFILE_MENU_PREFIX}company",
        )],
        [InlineKeyboardButton(
            text="✍️ Мой стиль общения", callback_data=f"{PROFILE_MENU_PREFIX}style",
        )],
        [InlineKeyboardButton(
            text="📝 Примеры моих текстов", callback_data=f"{PROFILE_MENU_PREFIX}examples",
        )],
        [InlineKeyboardButton(
            text="🚫 Чего не использовать", callback_data=f"{PROFILE_MENU_PREFIX}avoid",
        )],
        [InlineKeyboardButton(
            text="👁 Посмотреть профиль", callback_data=f"{PROFILE_MENU_PREFIX}view",
        )],
        [InlineKeyboardButton(text=BTN_V2_MAIN_MENU, callback_data=SOURCE_ACTION_MAIN_MENU)],
    ])


def profile_company_fields_keyboard(*, show_business_type: bool) -> InlineKeyboardMarkup:
    """show_business_type=False для ta_affiliated workspace — тип бизнеса

    там задан системно и пользователю недоступен для редактирования."""
    rows = [
        [InlineKeyboardButton(
            text="Название бизнеса", callback_data=f"{PROFILE_FIELD_PREFIX}business_name",
        )],
    ]
    if show_business_type:
        rows.append([InlineKeyboardButton(
            text="Тип бизнеса", callback_data=f"{PROFILE_FIELD_PREFIX}business_type",
        )])
    rows.extend([
        [InlineKeyboardButton(
            text="Короткое описание", callback_data=f"{PROFILE_FIELD_PREFIX}short_description",
        )],
        [InlineKeyboardButton(
            text="Специализации", callback_data=f"{PROFILE_FIELD_PREFIX}specializations",
        )],
        [InlineKeyboardButton(
            text="Направления", callback_data=f"{PROFILE_FIELD_PREFIX}destinations",
        )],
        [InlineKeyboardButton(
            text="Регион работы", callback_data=f"{PROFILE_FIELD_PREFIX}region",
        )],
        [InlineKeyboardButton(
            text="Целевая аудитория", callback_data=f"{PROFILE_FIELD_PREFIX}audiences",
        )],
        [InlineKeyboardButton(
            text="Стиль общения компании", callback_data=f"{PROFILE_FIELD_PREFIX}tone",
        )],
        [InlineKeyboardButton(
            text="⬅️ Назад", callback_data=f"{PROFILE_MENU_PREFIX}root",
        )],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def profile_examples_keyboard(count: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if count < 5:
        rows.append([InlineKeyboardButton(
            text="➕ Добавить пример", callback_data=f"{PROFILE_EXAMPLES_PREFIX}add",
        )])
    if count:
        rows.append([InlineKeyboardButton(
            text="👁 Посмотреть примеры", callback_data=f"{PROFILE_EXAMPLES_PREFIX}view",
        )])
        rows.append([InlineKeyboardButton(
            text="🗑 Очистить примеры", callback_data=f"{PROFILE_EXAMPLES_PREFIX}clear",
        )])
    rows.append([InlineKeyboardButton(
        text="⬅️ Назад", callback_data=f"{PROFILE_MENU_PREFIX}root",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def profile_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"{PROFILE_MENU_PREFIX}root")],
    ])


def reply_confirm_keyboard(work_item_id: int, revision: str) -> InlineKeyboardMarkup:
    """Кнопки под черновиком, подготовленным Reply flow для конкретного
    work_item. Кнопки сами ничего не генерируют — только меняют состояние
    уже существующего work_item.

    ``revision`` — версия work_item на момент показа ЭТОГО черновика (см.
    app.domain.work.work_item_revision). Обязательна: без неё нечем отличить
    кнопки под текущим черновиком от кнопок под более старым, если для того
    же work_item успели подготовить новый.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Отправил",
            callback_data=f"{REPLY_CONFIRM_SENT_PREFIX}{work_item_id}:{revision}",
        )],
        [InlineKeyboardButton(
            text="🕒 Позже",
            callback_data=f"{REPLY_CONFIRM_LATER_PREFIX}{work_item_id}:{revision}",
        )],
        [InlineKeyboardButton(
            text="🚫 Не актуально",
            callback_data=f"{REPLY_CONFIRM_DISMISS_PREFIX}{work_item_id}:{revision}",
        )],
    ])
