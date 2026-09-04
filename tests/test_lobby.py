"""Stage 3A: публичное лобби и "Осмотреться" — без workspace, без подписки."""

from __future__ import annotations

import asyncio
from typing import Any

from app.domain.partners import WorkspaceContext
from app.handlers.lobby import (
    BROWSE_INTRO_TEXT,
    PAYMENT_COMING_SOON_TEXT,
    PAYMENT_RENEW_TEXT,
    WEB_BILLING_URL,
    WELCOME_EXPIRED_TEXT,
    WELCOME_NEW_TEXT,
    WHATS_INCLUDED_TEXT,
    WORKSPACE_AMBIGUOUS_TEXT,
    on_access_gate_intercept,
    on_access_gate_intercept_callback,
    on_browse_back,
    on_browse_pressed,
    on_browse_section_selected,
    on_payment_stub_pressed,
    on_whats_included_pressed,
    route_access_gate,
)
from app.keyboards import BROWSE_SECTION_PREFIX


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _Message:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.answers: list[tuple[str, Any]] = []
        self.edits: list[tuple[str, Any]] = []

    async def answer(self, text: str, reply_markup: Any = None, **kwargs: Any) -> None:
        self.answers.append((text, reply_markup))

    async def edit_text(self, text: str, reply_markup: Any = None, **kwargs: Any) -> None:
        self.edits.append((text, reply_markup))


class _Callback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = _Message()
        self.answers: list[tuple[Any, dict]] = []

    async def answer(self, text: Any = None, **kwargs: Any) -> None:
        self.answers.append((text, kwargs))


def _ctx(workspace_id: int = 1) -> WorkspaceContext:
    return WorkspaceContext(100, workspace_id, "owner", "active")


# --- 🧭 Осмотреться: работает без workspace/tenant/LLM/repository -----------


def test_browse_pressed_shows_sections_without_any_workspace_param() -> None:
    """Сигнатура хендлера физически не принимает workspace_context/
    repository/llm_provider — раздел read-only по построению."""
    import inspect
    params = inspect.signature(on_browse_pressed).parameters
    assert set(params) == {"message"}

    message = _Message()
    _run(on_browse_pressed(message))

    text, markup = message.answers[0]
    assert text == BROWSE_INTRO_TEXT
    assert len(markup.inline_keyboard) == 10


def test_browse_section_selected_shows_static_text() -> None:
    callback = _Callback(f"{BROWSE_SECTION_PREFIX}signals")
    _run(on_browse_section_selected(callback))

    text, _ = callback.message.edits[0]
    assert "Сигналы и идеи" in text
    assert "Оркестратор следит за подключёнными источниками" in text


def test_all_ten_browse_sections_are_reachable() -> None:
    for key in (
        "start", "signals", "materials", "replies", "sources",
        "competitors", "profile", "access", "hosting", "faq",
    ):
        callback = _Callback(f"{BROWSE_SECTION_PREFIX}{key}")
        _run(on_browse_section_selected(callback))
        assert callback.message.edits, f"section {key} produced no text"


def test_browse_section_texts_do_not_mention_prices() -> None:
    for key in ("access", "hosting", "faq"):
        callback = _Callback(f"{BROWSE_SECTION_PREFIX}{key}")
        _run(on_browse_section_selected(callback))
        text = callback.message.edits[0][0]
        for forbidden in ("₽", " руб", "руб.", "USD", "$"):
            assert forbidden not in text


def test_browse_back_returns_to_intro() -> None:
    callback = _Callback("browse:back")
    _run(on_browse_back(callback))
    text, _ = callback.message.edits[0]
    assert text == BROWSE_INTRO_TEXT


# --- заглушки оплаты: явные, изолированные, ничего не создают --------------


def test_payment_stub_buttons_do_not_take_repository_param() -> None:
    """No repository/LLM/billing-secret dependency - only workspace_context
    (already resolved upstream by WorkspaceContextMiddleware), used purely
    to decide which of two static texts to show."""
    import inspect
    params = inspect.signature(on_payment_stub_pressed).parameters
    assert set(params) == {"message", "workspace_context"}


def test_new_visitor_without_workspace_sees_the_coming_soon_stub() -> None:
    """No workspace_context (brand-new visitor, никакого workspace ещё
    нет) - ничего не активирует, workspace не создаёт, прежняя заглушка."""
    message = _Message()
    _run(on_payment_stub_pressed(message, workspace_context=None))
    assert message.answers[0][0] == PAYMENT_COMING_SOON_TEXT
    assert "скоро" in message.answers[0][0].lower()


def test_existing_workspace_with_closed_access_sees_the_web_billing_link() -> None:
    """workspace_context present (expired/past_due/suspended existing
    workspace) - a concrete, safe action: a link to web billing, no
    workspace_id/token in the URL, no new auth flow."""
    message = _Message()
    _run(on_payment_stub_pressed(message, workspace_context=_ctx()))
    text = message.answers[0][0]
    assert text == PAYMENT_RENEW_TEXT
    assert WEB_BILLING_URL in text
    assert "workspace" not in WEB_BILLING_URL
    assert str(_ctx().workspace_id) not in WEB_BILLING_URL


def test_whats_included_has_no_price_and_mentions_14_days_then_month() -> None:
    message = _Message()
    _run(on_whats_included_pressed(message))
    text = message.answers[0][0]
    assert "14 дней" in text
    assert "месячная" in text.lower()
    assert "цены" in text.lower() and "не объявлены" in text.lower()
    for forbidden in ("₽", " руб", "руб.", "USD", "$"):
        assert forbidden not in text


# --- route_access_gate: единая точка решения --------------------------------


def test_route_access_gate_new_visitor() -> None:
    message = _Message()
    _run(route_access_gate(
        message, workspace_context=None, workspace_context_ambiguous=False,
        access_state="no_workspace",
    ))
    assert message.answers[0][0] == WELCOME_NEW_TEXT


def test_route_access_gate_ambiguous_takes_priority_over_no_workspace() -> None:
    message = _Message()
    _run(route_access_gate(
        message, workspace_context=None, workspace_context_ambiguous=True,
        access_state="no_workspace",
    ))
    assert message.answers[0][0] == WORKSPACE_AMBIGUOUS_TEXT


def test_route_access_gate_expired() -> None:
    message = _Message()
    _run(route_access_gate(
        message, workspace_context=_ctx(), workspace_context_ambiguous=False,
        access_state="expired",
    ))
    assert message.answers[0][0] == WELCOME_EXPIRED_TEXT


def test_route_access_gate_suspended() -> None:
    message = _Message()
    _run(route_access_gate(
        message, workspace_context=_ctx(), workspace_context_ambiguous=False,
        access_state="suspended",
    ))
    assert "приостановлен" in message.answers[0][0].lower()


# --- centralized gate: перехватывает работающие команды без доступа --------


def test_gate_intercepts_free_text_without_workspace() -> None:
    message = _Message("📡 Найти сигналы и идеи")  # пытается нажать рабочую кнопку
    _run(on_access_gate_intercept(
        message, workspace_context=None, workspace_context_ambiguous=False,
        access_state="no_workspace",
    ))
    assert message.answers[0][0] == WELCOME_NEW_TEXT


def test_gate_intercepts_free_text_for_expired_workspace() -> None:
    message = _Message("✍️ Создать материал")
    _run(on_access_gate_intercept(
        message, workspace_context=_ctx(), workspace_context_ambiguous=False,
        access_state="expired",
    ))
    assert message.answers[0][0] == WELCOME_EXPIRED_TEXT


def test_gate_intercepts_stray_callback_without_mutating_anything() -> None:
    callback = _Callback("daily_action:done:5")
    _run(on_access_gate_intercept_callback(callback))
    assert callback.answers[0][1] == {"show_alert": True}


# --- явные 5 сценариев из security-проверки (через реальный роутинг) -------


def _resolve_via_real_router(data: str, **workflow_data: Any) -> str | None:
    """Гоняет callback_data через реальные lobby.router/menu.router в их
    фактическом порядке регистрации — не прямой вызов функции, а именно
    routing decision (тот же приём, что и в тестах ниже)."""
    from app.handlers import menu as menu_handlers
    from app.handlers import onboarding as onboarding_handlers
    from app.handlers import lobby as lobby_handlers

    async def _run_lookup() -> str | None:
        callback = _Callback(data)
        payload = {"raw_state": None, **workflow_data}
        for sub_router in (lobby_handlers.router, onboarding_handlers.router, menu_handlers.router):
            for handler in sub_router.callback_query.handlers:
                matched, _ = await handler.check(callback, **payload)
                if matched:
                    return handler.callback.__name__
        return None

    return _run(_run_lookup())


def test_1_no_workspace_message_hits_lobby_gate() -> None:
    message = _Message("📡 Найти сигналы и идеи")
    _run(on_access_gate_intercept(
        message, workspace_context=None, workspace_context_ambiguous=False,
        access_state="no_workspace",
    ))
    assert message.answers[0][0] == WELCOME_NEW_TEXT


def test_2_no_workspace_callback_hits_lobby_gate_via_real_routing() -> None:
    handler_name = _resolve_via_real_router(
        "radar_content:7", v2_menu_enabled=True,
        workspace_context=None, workspace_context_ambiguous=False,
        access_state="no_workspace", onboarding_required=False,
    )
    assert handler_name == "on_access_gate_intercept_callback"


def test_3_expired_callback_hits_lobby_gate_via_real_routing() -> None:
    handler_name = _resolve_via_real_router(
        "radar_content:7", v2_menu_enabled=True,
        workspace_context=_ctx(), workspace_context_ambiguous=False,
        access_state="expired", onboarding_required=False,
    )
    assert handler_name == "on_access_gate_intercept_callback"


def test_4_suspended_callback_hits_lobby_gate_via_real_routing() -> None:
    handler_name = _resolve_via_real_router(
        "radar_content:7", v2_menu_enabled=True,
        workspace_context=_ctx(), workspace_context_ambiguous=False,
        access_state="suspended", onboarding_required=False,
    )
    assert handler_name == "on_access_gate_intercept_callback"


def test_5_active_callback_reaches_real_work_handler_via_real_routing() -> None:
    handler_name = _resolve_via_real_router(
        "radar_content:7", v2_menu_enabled=True,
        workspace_context=_ctx(), workspace_context_ambiguous=False,
        access_state="active", onboarding_required=False,
    )
    assert handler_name == "on_radar_content_selected"


# --- реальный порядок роутеров: lobby выигрывает у menu без доступа --------


def test_real_router_order_gate_intercepts_menu_button_before_menu_handler() -> None:
    """Тот же приём, что и в tests/test_onboarding.py
    test_real_router_order_gate_intercepts_v2_button_before_menu: реальный
    список router-объектов, без повторного build_router()."""
    from app.handlers import lobby as lobby_handlers
    from app.handlers import menu as menu_handlers
    from app.handlers import onboarding as onboarding_handlers
    from app.keyboards import BTN_V2_FIND_SIGNALS

    real_order = (lobby_handlers.router, onboarding_handlers.router, menu_handlers.router)

    async def _first_match(text: str, **workflow_data: Any) -> str | None:
        message = _Message(text)
        data = {"raw_state": None, **workflow_data}
        for sub_router in real_order:
            for handler in sub_router.message.handlers:
                matched, _ = await handler.check(message, **data)
                if matched:
                    return handler.callback.__name__
        return None

    # Без workspace: catch-all лобби перехватывает кнопку рабочего меню.
    blocked = _run(_first_match(
        BTN_V2_FIND_SIGNALS, v2_menu_enabled=True,
        workspace_context=None, workspace_context_ambiguous=False,
        access_state="no_workspace", onboarding_required=False,
    ))
    assert blocked == "on_access_gate_intercept"

    # С активным доступом та же кнопка доходит до on_find_signals как раньше.
    granted = _run(_first_match(
        BTN_V2_FIND_SIGNALS, v2_menu_enabled=True,
        workspace_context=_ctx(), workspace_context_ambiguous=False,
        access_state="active", onboarding_required=False,
    ))
    assert granted == "on_find_signals"


def test_real_router_order_gate_intercepts_all_work_callbacks_before_their_handlers() -> None:
    """Security-проверка (не новый общий аудит): CallbackQuery-версия
    test_real_router_order_gate_intercepts_menu_button_before_menu_handler.

    Проверяет реальный порядок роутеров из app/handlers/__init__.py для
    представителя каждого рабочего модуля с callback_query-хендлерами:
    Radar (menu.py), создание материала (material_generation.py), проверка/
    ответ текста (text_review.py), источники (sources.py), конкуренты
    (competitors.py), материалы (materials.py), использование материала
    (content_usage.py), daily actions/ответ клиенту (daily_actions.py).

    on_access_gate_intercept_callback не фильтрует по callback_data вообще
    (только по access_state) — поэтому один этот тест архитектурно
    подтверждает перехват для ЛЮБОГО callback_data этих модулей, включая
    старые inline-кнопки из предыдущих сообщений: callback_data одинаково
    "стар" или "нов" с точки зрения роутинга, различия нет.
    """
    from app.handlers import competitors as competitors_handlers
    from app.handlers import content_usage as content_usage_handlers
    from app.handlers import daily_actions as daily_actions_handlers
    from app.handlers import lobby as lobby_handlers
    from app.handlers import material_generation as material_generation_handlers
    from app.handlers import materials as materials_handlers
    from app.handlers import menu as menu_handlers
    from app.handlers import onboarding as onboarding_handlers
    from app.handlers import sources as sources_handlers
    from app.handlers import text_review as text_review_handlers
    from app.keyboards import (
        ARTIFACT_CHECK_PREFIX,
        ARTIFACT_MARK_USED_PREFIX,
        ARTIFACT_OPEN_PREFIX,
        COMPETITOR_REGISTRY_ADD,
        DAILY_ACTION_PROMPT_PREFIX,
        SOURCE_MATERIAL_PREFIX,
        SOURCE_TOGGLE_PREFIX,
    )

    real_order = (
        lobby_handlers.router,
        onboarding_handlers.router,
        menu_handlers.router,
        material_generation_handlers.router,
        text_review_handlers.router,
        sources_handlers.router,
        competitors_handlers.router,
        materials_handlers.router,
        content_usage_handlers.router,
        daily_actions_handlers.router,
    )

    work_callbacks = {
        "radar": f"{menu_handlers._RADAR_CONTENT_PREFIX}7",
        "create_material": f"{SOURCE_MATERIAL_PREFIX}5",
        "check_or_reply_text": f"{ARTIFACT_CHECK_PREFIX}5",
        "sources": f"{SOURCE_TOGGLE_PREFIX}vk_example",
        "competitors": COMPETITOR_REGISTRY_ADD,
        "materials": f"{ARTIFACT_OPEN_PREFIX}5",
        "content_usage": f"{ARTIFACT_MARK_USED_PREFIX}5",
        "daily_actions_reply": f"{DAILY_ACTION_PROMPT_PREFIX}5",
    }

    async def _first_match(data: str, **workflow_data: Any) -> str | None:
        callback = _Callback(data)
        payload = {"raw_state": None, **workflow_data}
        for sub_router in real_order:
            for handler in sub_router.callback_query.handlers:
                matched, _ = await handler.check(callback, **payload)
                if matched:
                    return handler.callback.__name__
        return None

    for label, data in work_callbacks.items():
        blocked = _run(_first_match(
            data, v2_menu_enabled=True,
            workspace_context=None, workspace_context_ambiguous=False,
            access_state="no_workspace", onboarding_required=False,
        ))
        assert blocked == "on_access_gate_intercept_callback", (
            f"{label}: no_workspace не перехвачен gate'ом, дошло до {blocked}"
        )

        blocked_expired = _run(_first_match(
            data, v2_menu_enabled=True,
            workspace_context=_ctx(), workspace_context_ambiguous=False,
            access_state="expired", onboarding_required=False,
        ))
        assert blocked_expired == "on_access_gate_intercept_callback", (
            f"{label}: expired не перехвачен gate'ом, дошло до {blocked_expired}"
        )

        blocked_suspended = _run(_first_match(
            data, v2_menu_enabled=True,
            workspace_context=_ctx(), workspace_context_ambiguous=False,
            access_state="suspended", onboarding_required=False,
        ))
        assert blocked_suspended == "on_access_gate_intercept_callback", (
            f"{label}: suspended не перехвачен gate'ом, дошло до {blocked_suspended}"
        )

        # Контрольная проверка: при активном доступе callback реально доходит
        # до СВОЕГО обработчика (не до gate'а) — perimeter не пере-перекрыт.
        granted = _run(_first_match(
            data, v2_menu_enabled=True,
            workspace_context=_ctx(), workspace_context_ambiguous=False,
            access_state="active", onboarding_required=False,
        ))
        assert granted != "on_access_gate_intercept_callback", (
            f"{label}: active всё ещё перехвачен gate'ом"
        )
        assert granted is not None, f"{label}: active не находит вообще никакого хендлера"


def test_real_router_order_lobby_buttons_are_not_swallowed_by_its_own_gate() -> None:
    """Специфичные хендлеры лобби (BTN_LOBBY_BROWSE и т.д.) зарегистрированы
    раньше catch-all в том же роутере — тот же принцип, что в
    app/handlers/onboarding.py между FSM-хендлером и catch-all."""
    from app.handlers import lobby as lobby_handlers

    handlers_in_order = [h.callback.__name__ for h in lobby_handlers.router.message.handlers]
    catch_all_index = handlers_in_order.index("on_access_gate_intercept")
    for specific in (
        "on_browse_pressed", "on_payment_stub_pressed", "on_whats_included_pressed",
    ):
        assert handlers_in_order.index(specific) < catch_all_index
