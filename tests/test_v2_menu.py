from __future__ import annotations

import asyncio
from typing import Any

import pytest
from aiogram import Router

from app.config import load_settings
from app.handlers.menu import (
    AwaitTask,
    on_v2_category,
    on_v2_help,
    on_v2_main_menu,
    on_v2_create_material,
    on_v2_placeholder,
)
from app.handlers import menu as menu_handlers
from app.handlers import tasks as task_handlers
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
    BTN_V2_CONTENT_PLAN,
    BTN_V2_CREATE_MATERIAL,
    BTN_V2_HELP,
    BTN_V2_MAIN_MENU,
    BTN_V2_MATERIALS,
    BTN_V2_PROFILE,
    BTN_WEB_RESOURCES,
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
    BTN_V2_ANALYZE_LINK,
    BTN_V2_CREATE_MATERIAL,
    BTN_V2_CLIENT_REPLY,
    BTN_V2_CONTENT_PLAN,
    BTN_V2_CHECK_TEXT,
    BTN_V2_MATERIALS,
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
    assert BTN_FIND_SIGNALS not in texts
    assert BTN_WEB_RESOURCES not in texts


def test_dispatcher_exposes_boolean_flag_as_workflow_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.main.build_router", lambda: Router())

    dispatcher = _build_dispatcher(
        frozenset(),
        journal=None,
        content_factory_config=None,
        lead_radar_config=None,
        v2_menu_enabled=True,
    )

    assert dispatcher["v2_menu_enabled"] is True


class _Message:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.answers: list[tuple[str, Any]] = []

    async def answer(self, text: str, reply_markup: Any = None, **kwargs: Any) -> None:
        self.answers.append((text, reply_markup))


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


def test_start_selects_menu_from_flag() -> None:
    v1_message = _Message()
    v2_message = _Message()
    v1_state = _State()
    v2_state = _State()

    _run(cmd_start(v1_message, v1_state, v2_menu_enabled=False))
    _run(cmd_start(v2_message, v2_state, v2_menu_enabled=True))

    assert _texts(v1_message.answers[0][1]) == V1_BUTTONS
    assert _texts(v2_message.answers[0][1]) == V2_BUTTONS
    assert v1_state.clear_calls == 0
    assert v2_state.clear_calls == 1


def test_v2_help_placeholder_and_return_use_v2_navigation() -> None:
    help_message = _Message(BTN_V2_HELP)
    placeholder_message = _Message(BTN_V2_PROFILE)
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


def test_v2_create_material_explains_source_first_and_offers_analysis() -> None:
    message = _Message(BTN_V2_CREATE_MATERIAL)
    state = _State()
    _run(on_v2_create_material(message, state))
    text, markup = message.answers[0]
    assert "Сначала разберите источник" in text
    assert _texts(markup) == [BTN_V2_ANALYZE_LINK, BTN_V2_MAIN_MENU]


@pytest.mark.parametrize(
    ("button", "module"),
    [
        (BTN_V2_CHECK_TEXT, Module.SAFETY_LAYER),
        (BTN_V2_CLIENT_REPLY, Module.TRAVEL_ASSISTANT),
    ],
)
def test_v2_existing_scenarios_reuse_await_task(button: str, module: Module) -> None:
    message = _Message(button)
    state = _State()

    _run(on_v2_category(message, state))

    assert state.data == {"forced_module": module.value}
    assert state.state == AwaitTask.waiting
    assert _texts(message.answers[0][1]) == V2_BUTTONS


async def _first_matching_handler(text: str, **workflow_data: Any) -> str | None:
    message = _Message(text)
    data = {"raw_state": None, **workflow_data}
    for router in (menu_handlers.router, task_handlers.router):
        for handler in router.message.handlers:
            matched, _ = await handler.check(message, **data)
            if matched:
                return handler.callback.__name__
    return None


@pytest.mark.parametrize(
    ("button", "handler_name"),
    [
        (BTN_V2_PROFILE, "on_v2_placeholder"),
        (BTN_V2_HELP, "on_v2_help"),
        (BTN_V2_MAIN_MENU, "on_v2_main_menu"),
        (BTN_V2_CHECK_TEXT, "on_v2_category"),
    ],
)
def test_v2_buttons_reach_menu_router_before_general_task(
    button: str, handler_name: str
) -> None:
    assert _run(
        _first_matching_handler(button, v2_menu_enabled=True)
    ) == handler_name


@pytest.mark.parametrize("workflow_data", [{"v2_menu_enabled": False}, {}])
def test_v2_text_is_ordinary_text_when_feature_is_disabled(
    workflow_data: dict[str, Any],
) -> None:
    assert _run(
        _first_matching_handler(BTN_V2_PROFILE, **workflow_data)
    ) == "on_free_text"
