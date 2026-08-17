from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram import Router

from app.config import load_settings
from app.domain.partners import PartnerWorkspace, WorkspaceContext
from app.handlers.menu import (
    AwaitReplySubject,
    AwaitTask,
    on_find_signals,
    on_material_entry_analyze,
    on_material_entry_find_signals,
    on_v2_category,
    on_v2_help,
    on_v2_main_menu,
    on_v2_create_material,
    on_v2_placeholder,
)
from app.handlers.source_analysis import AnalyzeSource
from app.handlers import competitors as competitors_handlers
from app.handlers import materials as materials_handlers
from app.handlers import menu as menu_handlers
from app.handlers import profile as profile_handlers
from app.handlers import tasks as task_handlers
from app.handlers import text_review as text_review_handlers
from app.handlers.start import cmd_start
from app.main import _build_dispatcher
from app.keyboards import (
    BTN_CHECK_TEXT,
    BTN_CLIENT_QUESTION,
    BTN_CREATE_CONTENT,
    BTN_FIND_SIGNALS,
    BTN_HOW_IT_WORKS,
    BTN_LAST_TASK,
    BTN_PACKAGE_MATERIALS,
    BTN_UNSURE,
    BTN_V2_ANALYZE_LINK,
    BTN_V2_CHECK_TEXT,
    BTN_V2_CLIENT_REPLY,
    BTN_V2_COMPETITORS,
    BTN_V2_CONTENT_PLAN,
    BTN_V2_CREATE_MATERIAL,
    BTN_V2_DAILY_ACTIONS,
    BTN_V2_FIND_SIGNALS,
    BTN_V2_HELP,
    BTN_V2_MAIN_MENU,
    BTN_V2_MATERIALS,
    BTN_V2_PROFILE,
    BTN_V2_SOURCES,
    BTN_WEB_RESOURCES,
    MATERIAL_ENTRY_ANALYZE,
    MATERIAL_ENTRY_FIND_SIGNALS,
    active_main_menu,
    main_menu,
    v2_main_menu,
)
from app.routing.modules import Module


V1_BUTTONS = [
    BTN_CREATE_CONTENT,
    BTN_CLIENT_QUESTION,
    BTN_FIND_SIGNALS,
    BTN_CHECK_TEXT,
    BTN_PACKAGE_MATERIALS,
    BTN_UNSURE,
    BTN_LAST_TASK,
    BTN_HOW_IT_WORKS,
    BTN_WEB_RESOURCES,
]
V2_BUTTONS = [
    BTN_V2_DAILY_ACTIONS,
    BTN_V2_CREATE_MATERIAL,
    BTN_V2_CLIENT_REPLY,
    BTN_V2_FIND_SIGNALS,
    BTN_V2_ANALYZE_LINK,
    BTN_V2_CHECK_TEXT,
    BTN_V2_MATERIALS,
    BTN_V2_SOURCES,
    BTN_V2_COMPETITORS,
    BTN_V2_PROFILE,
    BTN_V2_HELP,
]


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _texts(markup: Any) -> list[str]:
    return [button.text for row in markup.keyboard for button in row]


def _settings(monkeypatch: pytest.MonkeyPatch, flag: str | None):
    monkeypatch.setattr("app.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("BOT_TOKEN", "dummy-token")
    monkeypatch.setenv("ADMIN_TELEGRAM_ID", "586249067")
    if flag is None:
        monkeypatch.delenv("TA_CONTROL_CENTER_V2_MENU_ENABLED", raising=False)
    else:
        monkeypatch.setenv("TA_CONTROL_CENTER_V2_MENU_ENABLED", flag)
    return load_settings()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, False),
        ("", False),
        ("false", False),
        ("FALSE", False),
        ("0", False),
        ("no", False),
        ("true", True),
        ("TRUE", True),
        ("invalid", False),
    ],
)
def test_v2_flag_is_fail_safe(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: bool
) -> None:
    settings = _settings(monkeypatch, raw)
    assert settings.v2_menu_enabled is expected
    assert settings.bot_token == "dummy-token"
    assert settings.admin_telegram_id == 586249067
    assert settings.content_factory_source_analysis_url == ""


def test_v1_menu_is_unchanged_and_remains_default() -> None:
    assert _texts(main_menu()) == V1_BUTTONS
    assert _texts(active_main_menu(False)) == V1_BUTTONS


def test_v2_menu_has_exact_task_buttons_without_legacy_links() -> None:
    texts = _texts(v2_main_menu())
    assert texts == V2_BUTTONS
    assert BTN_V2_CONTENT_PLAN not in texts
    assert BTN_V2_FIND_SIGNALS in texts
    assert BTN_FIND_SIGNALS not in texts
    assert BTN_WEB_RESOURCES not in texts


def test_dispatcher_exposes_boolean_flag_as_workflow_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.main.build_router", lambda: Router())

    dispatcher = _build_dispatcher(
        frozenset(),
        journal=None,
        llm_provider=None,
        lead_radar_config=None,
        v2_menu_enabled=True,
    )

    assert dispatcher["v2_menu_enabled"] is True


class _Message:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.answers: list[tuple[str, Any]] = []
        self.reply_markup_edits: list[Any] = []

    async def answer(self, text: str, reply_markup: Any = None, **kwargs: Any) -> None:
        self.answers.append((text, reply_markup))

    async def edit_reply_markup(self, reply_markup: Any = None) -> None:
        self.reply_markup_edits.append(reply_markup)


class _Callback:
    def __init__(self, message: "_Message") -> None:
        self.message = message
        self.answered = False

    async def answer(self, *args: Any, **kwargs: Any) -> None:
        self.answered = True


class _State:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.state: Any = None
        self.clear_calls = 0

    async def update_data(self, **kwargs: Any) -> None:
        self.data.update(kwargs)

    async def set_state(self, state: Any) -> None:
        self.state = state

    async def clear(self) -> None:
        self.clear_calls += 1
        self.data.clear()
        self.state = None


def _resolved_workspace_context(workspace_id: int = 42) -> WorkspaceContext:
    return WorkspaceContext(100, workspace_id, "member", "active")


def _resolved_partner_repository(workspace_id: int = 42) -> Any:
    workspace = PartnerWorkspace(workspace_id, "Acme Travel Club", "acme", "active", "now", "now")
    return AsyncMock(get_workspace=AsyncMock(return_value=workspace))


def test_start_selects_menu_from_flag() -> None:
    v1_message = _Message()
    v2_message = _Message()
    v1_state = _State()
    v2_state = _State()

    _run(cmd_start(
        v1_message, v1_state, v2_menu_enabled=False,
        workspace_context=_resolved_workspace_context(),
        partner_repository=_resolved_partner_repository(),
    ))
    _run(cmd_start(
        v2_message, v2_state, v2_menu_enabled=True,
        workspace_context=_resolved_workspace_context(),
        partner_repository=_resolved_partner_repository(),
    ))

    assert _texts(v1_message.answers[0][1]) == V1_BUTTONS
    assert _texts(v2_message.answers[0][1]) == V2_BUTTONS
    assert v1_state.clear_calls == 0
    assert v2_state.clear_calls == 1


def test_v2_help_placeholder_and_return_use_v2_navigation() -> None:
    help_message = _Message(BTN_V2_HELP)
    placeholder_message = _Message(BTN_V2_CONTENT_PLAN)
    return_message = _Message(BTN_V2_MAIN_MENU)
    state = _State()

    _run(on_v2_help(help_message, state))
    _run(on_v2_placeholder(placeholder_message, state))
    _run(on_v2_main_menu(return_message, state))

    assert _texts(help_message.answers[0][1]) == [BTN_V2_MAIN_MENU]
    placeholder_text, placeholder_markup = placeholder_message.answers[0]
    assert "будет подключён" in placeholder_text
    assert "репозитор" not in placeholder_text.lower()
    assert _texts(placeholder_markup) == [BTN_V2_MAIN_MENU]
    assert _texts(return_message.answers[0][1]) == V2_BUTTONS
    assert state.clear_calls == 3


def test_v2_create_material_shows_entry_point_choice_screen() -> None:
    """«✍️ Создать материал» — экран выбора между уже существующими рабочими
    путями через inline-кнопки, а не отдельный генератор и не обещание
    материала «по любой теме». Inline-кнопки (в отличие от reply-текста)
    нельзя случайно «напечатать» и провалиться в общий текстовый роутинг."""
    message = _Message(BTN_V2_CREATE_MATERIAL)
    state = _State()
    _run(on_v2_create_material(message, state))

    text, markup = message.answers[0]
    assert "Как хотите создать материал?" in text
    assert "по любой теме" not in text.lower()
    assert "ссылку" not in text.lower()
    # Первое сообщение — обычная reply-клавиатура только с возвратом в меню.
    assert _texts(markup) == [BTN_V2_MAIN_MENU]

    _, choice_markup = message.answers[1]
    buttons = [button for row in choice_markup.inline_keyboard for button in row]
    assert [button.text for button in buttons] == [
        BTN_V2_ANALYZE_LINK, BTN_V2_FIND_SIGNALS,
    ]
    assert [button.callback_data for button in buttons] == [
        MATERIAL_ENTRY_ANALYZE, MATERIAL_ENTRY_FIND_SIGNALS,
    ]


def test_v2_create_material_find_signals_reuses_direct_handler_without_route_card() -> None:
    """«Создать материал» → «Найти сигналы» переиспользует on_find_signals
    напрямую: никакой «📌 Карточка маршрута» / «Основной модуль» / «AI Lead
    Radar» / «Safety Layer» пользователю не показывается — тот же результат,
    что и у прямой кнопки «📡 Найти сигналы и идеи» из главного меню."""
    message = _Message()
    callback = _Callback(message)
    state = _State()

    _run(on_material_entry_find_signals(
        callback, state, lead_radar_config=None,
        workspace_signal_repository=None, workspace_context=None,
    ))

    assert callback.answered is True
    assert message.reply_markup_edits == [None]
    assert len(message.answers) == 1
    text, _ = message.answers[0]
    for forbidden in (
        "📌 Карточка маршрута", "Основной модуль", "AI Lead Radar", "Safety Layer",
    ):
        assert forbidden not in text
    # Без workspace_context on_find_signals отвечает тем же текстом, что и
    # при прямом вызове с главного меню — поведение не продублировано отдельно.
    assert text == "Рабочее пространство недоступно."


def test_v2_create_material_analyze_reuses_direct_handler() -> None:
    """«Создать материал» → «Разобрать публикацию» переиспользует
    start_source_analysis напрямую — тот же сценарий, что и прямая кнопка."""
    message = _Message()
    callback = _Callback(message)
    state = _State()

    _run(on_material_entry_analyze(callback, state))

    assert callback.answered is True
    assert message.reply_markup_edits == [None]
    assert state.state == AnalyzeSource.waiting_for_text
    text, _ = message.answers[0]
    assert "чтение ссылок" not in text


def test_v2_client_reply_asks_subject_before_await_task() -> None:
    """«Ответить клиенту» больше не ведёт в AwaitTask.waiting напрямую:
    сперва AwaitReplySubject.waiting («Кому отвечаем?»), и только после
    ответа на этот вопрос (on_reply_subject_received в tasks.py) — AwaitTask.
    Остальные CATEGORY_BUTTONS этот промежуточный шаг не проходят."""
    message = _Message(BTN_V2_CLIENT_REPLY)
    state = _State()

    _run(on_v2_category(message, state))

    assert state.data == {
        "forced_module": Module.TRAVEL_ASSISTANT.value, "skip_route_card": True,
    }
    assert state.state == AwaitReplySubject.waiting
    text, markup = message.answers[0]
    assert "Кому отвечаем" in text
    assert _texts(markup) == V2_BUTTONS


async def _first_matching_handler(text: str, **workflow_data: Any) -> str | None:
    message = _Message(text)
    data = {"raw_state": None, **workflow_data}
    for router in (
        menu_handlers.router, text_review_handlers.router,
        profile_handlers.router, materials_handlers.router,
        competitors_handlers.router, task_handlers.router,
    ):
        for handler in router.message.handlers:
            matched, _ = await handler.check(message, **data)
            if matched:
                return handler.callback.__name__
    return None


@pytest.mark.parametrize(
    ("button", "handler_name"),
    [
        (BTN_V2_PROFILE, "show_profile"),
        (BTN_V2_MATERIALS, "show_materials"),
        (BTN_V2_COMPETITORS, "show_competitors"),
        (BTN_V2_FIND_SIGNALS, "on_find_signals"),
        (BTN_V2_CREATE_MATERIAL, "on_v2_create_material"),
        (BTN_V2_CONTENT_PLAN, "on_v2_placeholder"),
        (BTN_V2_HELP, "on_v2_help"),
        (BTN_V2_MAIN_MENU, "on_v2_main_menu"),
        (BTN_V2_CHECK_TEXT, "start_free_text_review"),
    ],
)
def test_v2_buttons_reach_menu_router_before_general_task(
    button: str, handler_name: str
) -> None:
    assert _run(
        _first_matching_handler(button, v2_menu_enabled=True)
    ) == handler_name


@pytest.mark.parametrize(
    "button",
    [BTN_V2_PROFILE, BTN_V2_FIND_SIGNALS],
)
@pytest.mark.parametrize("workflow_data", [{"v2_menu_enabled": False}, {}])
def test_v2_text_is_ordinary_text_when_feature_is_disabled(
    button: str, workflow_data: dict[str, Any],
) -> None:
    assert _run(
        _first_matching_handler(button, **workflow_data)
    ) == "on_free_text"


def test_legacy_find_signals_button_is_unaffected_by_v2_flag() -> None:
    """Легаси-кнопка «Найти сигналы интереса» продолжает работать как раньше,
    независимо от v2_menu_enabled — маршрут не завязан на флаг."""
    for workflow_data in ({"v2_menu_enabled": True}, {"v2_menu_enabled": False}, {}):
        assert _run(
            _first_matching_handler(BTN_FIND_SIGNALS, **workflow_data)
        ) == "on_find_signals"
