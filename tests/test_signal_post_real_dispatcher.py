"""«📡 Найти сигналы и идеи» → «📝 Создать пост» через НАСТОЯЩИЙ Dispatcher.

Прямые вызовы обработчиков не ловят ошибки маршрутизации (порядок роутеров,
фильтры, outer-middleware, FSM). Здесь апдейты идут через ``feed_update``
реального ``app.main._build_dispatcher`` с боевым ``build_router()``.

Заменены заглушками (и только они): репозитории (partner/signal/artifact/
subscription), LLM-провайдер, рекомендатель внешнего Lead Radar и сетевая
сессия бота (запросы Telegram записываются, а не отправляются).
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app.domain.partners import PartnerWorkspace, WorkspaceContext
from app.keyboards import BTN_V2_FIND_SIGNALS
from app.main import _build_dispatcher
from app.services.llm.models import ContentDraft
from tests.llm_fakes import FakeLLMProvider
from tests.test_journal_handlers import (
    RADAR_SUMMARY,
    analysis_payload,
    business_profile,
    radar_record,
)

USER_ID = 100
CHAT_ID = 555


class RecordingSession(BaseSession):
    """Сетевая сессия-заглушка: записывает вызовы Bot API."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list = []
        self._message_id = 5000

    async def close(self) -> None:  # pragma: no cover - nothing to close
        return None

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, SendMessage):
            self._message_id += 1
            return Message(
                message_id=self._message_id, date=datetime.now(timezone.utc),
                chat=Chat(id=CHAT_ID, type="private"), text=method.text,
            )
        return True

    def sent_texts(self) -> list[str]:
        return [m.text for m in self.requests if isinstance(m, SendMessage)]


def _user() -> User:
    return User(id=USER_ID, is_bot=False, first_name="Owner")


def _detach_module_routers() -> None:
    """Модульные роутеры aiogram можно подключить к родителю только один раз;
    приложение собирает диспетчер один раз, тесты - по разу на сценарий."""
    import pkgutil

    import app.handlers as handlers_package

    for info in pkgutil.iter_modules(handlers_package.__path__):
        module = __import__(f"app.handlers.{info.name}", fromlist=["router"])
        module_router = getattr(module, "router", None)
        if module_router is not None:
            module_router._parent_router = None


def _fixture(web_records=()):
    """(dispatcher, bot, session, artifact_repo, llm) c реальным router-деревом."""
    workspace = PartnerWorkspace(42, "Acme Travel Club", "acme", "active", "now", "now")
    partner_repository = SimpleNamespace(
        resolve_workspace_context=AsyncMock(
            return_value=WorkspaceContext(USER_ID, 42, "owner", "active")
        ),
        get_workspace=AsyncMock(return_value=workspace),
        get_business_profile=AsyncMock(return_value=business_profile()),
        get_user_preferences=AsyncMock(return_value=None),
    )
    record = radar_record(title="Тропа Петяярви", summary=RADAR_SUMMARY)
    signal_repository = SimpleNamespace(
        sync_eligible=AsyncMock(),
        list_for_workspace=AsyncMock(return_value=[record]),
        get_for_workspace=AsyncMock(return_value=record),
    )
    artifact_repository = SimpleNamespace(
        create_artifact_with_initial_version=AsyncMock(
            return_value=(SimpleNamespace(id=501), object())
        )
    )
    subscription_repository = SimpleNamespace(
        resolve_access_state=AsyncMock(return_value="active")
    )
    llm = FakeLLMProvider(
        draft=ContentDraft("Готовый пост про тропу Петяярви", ()),
        analysis=analysis_payload(),
    )
    journal = SimpleNamespace(add=AsyncMock(return_value=1), last=AsyncMock())
    _detach_module_routers()
    dispatcher = _build_dispatcher(
        frozenset({USER_ID}), journal=journal, llm_provider=llm,
        lead_radar_config=SimpleNamespace(), v2_menu_enabled=True,
        partner_repository=partner_repository,
        artifact_repository=artifact_repository,
        workspace_signal_repository=signal_repository,
        subscription_repository=subscription_repository,
        source_catalog_repository=SimpleNamespace(),
        web_signal_repository=SimpleNamespace(
            list_for_workspace=AsyncMock(return_value=list(web_records)),
            get_for_workspace=AsyncMock(return_value=web_records[0] if web_records else None),
        ),
    )
    session = RecordingSession()
    bot = Bot(token="123456:TEST", session=session)
    return dispatcher, bot, session, artifact_repository, llm, record


def _recommender():
    return SimpleNamespace(
        recommend_action=lambda row: {
            "recommended_action": "content", "action_reason": "Подходит",
        },
        action_label=lambda action: "Создать контент",
    )


async def _press_find_signals(dispatcher, bot) -> None:
    update = Update(
        update_id=1,
        message=Message(
            message_id=1, date=datetime.now(timezone.utc),
            chat=Chat(id=CHAT_ID, type="private"), from_user=_user(),
            text=BTN_V2_FIND_SIGNALS,
        ),
    )
    await dispatcher.feed_update(bot, update)


def _create_post_buttons(session: RecordingSession) -> list:
    buttons = []
    for method in session.requests:
        markup = getattr(method, "reply_markup", None)
        for row in getattr(markup, "inline_keyboard", []) or []:
            buttons.extend(row)
    return buttons


async def _press(dispatcher, bot, button, message_id: int = 9000) -> None:
    update = Update(
        update_id=2,
        callback_query=CallbackQuery(
            id="cb1", from_user=_user(), chat_instance="ci",
            data=button.callback_data,
            message=Message(
                message_id=message_id, date=datetime.now(timezone.utc),
                chat=Chat(id=CHAT_ID, type="private"), text="💡 Выберите материал",
            ),
        ),
    )
    await dispatcher.feed_update(bot, update)


def test_find_signals_offers_create_post_button_with_real_callback_data() -> None:
    async def scenario():
        dispatcher, bot, session, _artifacts, _llm, record = _fixture()
        with patch("app.services.lead_radar._load_recommender", return_value=_recommender()):
            await _press_find_signals(dispatcher, bot)
        return session, record

    session, record = asyncio.run(scenario())
    buttons = _create_post_buttons(session)
    post_buttons = [b for b in buttons if b.text.startswith("📝 Создать пост")]
    assert [b.callback_data for b in post_buttons] == [f"radar_content:{record.interpretation_id}"]


def test_pressing_create_post_through_real_dispatcher_returns_draft_not_list() -> None:
    async def scenario():
        dispatcher, bot, session, artifacts, llm, _record = _fixture()
        with patch("app.services.lead_radar._load_recommender", return_value=_recommender()):
            await _press_find_signals(dispatcher, bot)
            listing_texts = list(session.sent_texts())
            button = next(
                b for b in _create_post_buttons(session)
                if b.text.startswith("📝 Создать пост")
            )
            session.requests.clear()
            await _press(dispatcher, bot, button)
        return session, artifacts, llm, listing_texts

    session, artifacts, llm, listing_texts = asyncio.run(scenario())

    # 1. Нажатие подтверждено сразу: «Готовлю черновик…».
    answers = [m for m in session.requests if isinstance(m, AnswerCallbackQuery)]
    assert [a.text for a in answers] == ["Готовлю черновик…"]
    # 2. Генерация реально вызвана, материал сохранён в текущем workspace.
    llm.generate_draft.assert_called_once()
    artifacts.create_artifact_with_initial_version.assert_awaited_once()
    assert artifacts.create_artifact_with_initial_version.await_args.args[0] == 42
    # 3. В ответ пришёл черновик, а не повторный список сигналов.
    texts = session.sent_texts()
    assert any("Готовый пост про тропу Петяярви" in t for t in texts)
    assert not any("Выберите материал" in t or "Найденные сигналы" in t for t in texts)
    assert all(t not in listing_texts for t in texts)
    # 4. Клавиатура выбора после нажатия снята.
    assert any(isinstance(m, EditMessageReplyMarkup) for m in session.requests)


def test_web_post_button_goes_through_real_dispatcher() -> None:
    """``webpost:<id>`` тоже доходит до своего обработчика, а не до общего меню."""
    async def scenario():
        dispatcher, bot, session, artifacts, llm, _record = _fixture()
        web_record = SimpleNamespace(
            id=9, title="Тропа Петяярви", summary=RADAR_SUMMARY, body="",
            item_url="https://example.org/trail", source_name="Блог",
        )
        dispatcher["web_signal_repository"] = SimpleNamespace(
            get_for_workspace=AsyncMock(return_value=web_record)
        )
        button = SimpleNamespace(callback_data="webpost:9")
        await _press(dispatcher, bot, button)
        return session, artifacts, llm

    session, artifacts, llm = asyncio.run(scenario())
    llm.generate_draft.assert_called_once()
    artifacts.create_artifact_with_initial_version.assert_awaited_once()
    assert any("Готовый пост про тропу Петяярви" in t for t in session.sent_texts())


def _web_record():
    return SimpleNamespace(
        id=9, workspace_id=42, source_id="s", source_name="Блог", source_url="https://example.org/",
        item_url="https://example.org/trail-petyayarvi-guide", title="Тропа Петяярви",
        summary=RADAR_SUMMARY, body="", fetched_at="now",
    )


def _post_button_ids(session: RecordingSession) -> list[str]:
    return [b.callback_data for b in _create_post_buttons(session) if b.text.startswith("📝")]


def test_slow_web_collection_does_not_delay_or_block_radar_post_button() -> None:
    """Web-сбор ещё идёт: Radar-кнопка уже отправлена, нажатие даёт черновик
    (а не ждёт сбор), web-материалы и их кнопка приходят отдельно по готовности."""
    from app.handlers import menu as menu_handlers

    async def scenario():
        dispatcher, bot, session, artifacts, llm, record = _fixture([_web_record()])
        release = asyncio.Event()
        started = asyncio.Event()

        async def slow_collect(*args, **kwargs):
            started.set()
            await release.wait()

        with patch("app.services.lead_radar._load_recommender", return_value=_recommender()),                 patch("app.handlers.menu.collect_web_signals", slow_collect):
            # Обработчик «Найти сигналы» возвращается, не дожидаясь web-сбора.
            await asyncio.wait_for(_press_find_signals(dispatcher, bot), timeout=5)
            await asyncio.wait_for(started.wait(), timeout=5)
            before_ids = _post_button_ids(session)
            before_texts = list(session.sent_texts())
            button = next(
                b for b in _create_post_buttons(session) if b.text.startswith("📝")
            )
            session.requests.clear()
            # Сбор всё ещё заблокирован - создание поста обязано пройти.
            await asyncio.wait_for(_press(dispatcher, bot, button), timeout=5)
            draft_texts = list(session.sent_texts())
            session.requests.clear()
            release.set()
            await asyncio.wait_for(
                asyncio.gather(*list(menu_handlers._BACKGROUND_TASKS)), timeout=5
            )
        return before_ids, before_texts, draft_texts, session, artifacts, record

    before_ids, before_texts, draft_texts, session, artifacts, record = asyncio.run(scenario())
    assert before_ids == [f"radar_content:{record.interpretation_id}"]
    assert not any("Материалы с сайтов" in t or "Источник: Блог" in t for t in before_texts)
    assert any("Готовый пост про тропу Петяярви" in t for t in draft_texts)
    artifacts.create_artifact_with_initial_version.assert_awaited_once()
    # После готовности сбора web-материал пришёл отдельным сообщением со своей кнопкой.
    assert any("Источник: Блог" in t for t in session.sent_texts())
    assert _post_button_ids(session) == ["webpost:9"]


def test_failed_web_collection_does_not_break_radar_post_button() -> None:
    from app.handlers import menu as menu_handlers

    async def scenario():
        dispatcher, bot, session, artifacts, llm, record = _fixture()

        async def broken_collect(*args, **kwargs):
            raise RuntimeError("web sources are down")

        with patch("app.services.lead_radar._load_recommender", return_value=_recommender()),                 patch("app.handlers.menu.collect_web_signals", broken_collect):
            await asyncio.wait_for(_press_find_signals(dispatcher, bot), timeout=5)
            await asyncio.wait_for(
                asyncio.gather(*list(menu_handlers._BACKGROUND_TASKS)), timeout=5
            )
            button = next(
                b for b in _create_post_buttons(session) if b.text.startswith("📝")
            )
            session.requests.clear()
            await _press(dispatcher, bot, button)
        return session, artifacts, record

    session, artifacts, record = asyncio.run(scenario())
    assert any("Готовый пост про тропу Петяярви" in t for t in session.sent_texts())
    artifacts.create_artifact_with_initial_version.assert_awaited_once()
