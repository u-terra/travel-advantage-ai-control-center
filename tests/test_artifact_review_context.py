"""«🛡 Проверка текста» и «✅ Сохранена версия» называют материал; указатель
«текущий материал» не откатывается на более старый черновик.

Прод 16.10: черновик A (Стамбул, №67) создан раньше, черновик B (Казанский
собор, №68) позже. Под B нажали «✅ Опубликовал» - клавиатура сообщения была
снята целиком, вместе с «🛡 Проверить текст». Следом «🛡 Проверить текст» из
сообщения про A проверила и сохранила версию №2 именно в A, при этом в карточке
не было ни слова о том, какой материал проверяется, а сохранение вернуло
«текущий материал» на A. Здесь тот же сценарий идёт через НАСТОЯЩИЙ
aiogram Dispatcher (feed_update) с настоящими репозиториями.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram import Bot
from aiogram.methods import EditMessageReplyMarkup
from aiogram.types import CallbackQuery, Chat, Message, Update

from app.keyboards import (
    ARTIFACT_CHECK_PREFIX,
    ARTIFACT_KEEP_LATER_PREFIX,
    ARTIFACT_MARK_USED_PREFIX,
    ARTIFACT_REVIEW_SAVE_PREFIX,
    SOURCE_ACTION_MAIN_MENU,
)
from app.main import _build_dispatcher
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.conversation_state_repository import ConversationStateRepository
from app.repositories.partner_repository import PartnerRepository, empty_business_context
from app.repositories.work_repository import WorkRepository
from app.services.conversation_state_service import ConversationStateService
from app.services.llm.models import TextCheckResult
from tests.llm_fakes import FakeLLMProvider
from tests.test_signal_post_real_dispatcher import (
    CHAT_ID,
    USER_ID,
    RecordingSession,
    _detach_module_routers,
    _user,
)

A_TITLE = "Telegram: Ура, подешевело: прямые рейсы из Москвы в Стамбул за 4800 рублей"
A_TEXT = "Прямые рейсы из Москвы в Стамбул звучат заманчиво."
A_IMPROVED = "Прямые рейсы из Москвы в Стамбул звучат заманчиво, но смотри условия тарифа."
B_TITLE = "Telegram: Санкт-Петербург: другой взгляд на привычные места"
B_TEXT = "Казанский собор открыл смотровую площадку на колоннаде."
B_IMPROVED = "Казанский собор открыл смотровую площадку на восточном крыле колоннады."


class _World:
    """Real repositories + the real Dispatcher; only Telegram and the LLM are fakes."""

    def __init__(self) -> None:
        self.llm = None


async def _build(tmp_path: Path, *, rewritten: dict[str, str | None]):
    db = tmp_path / "ws.sqlite3"
    partners = PartnerRepository(db)
    await partners.init()
    artifacts = ArtifactRepository(db)
    await artifacts.init()
    work = WorkRepository(db)
    await work.init()
    conversation = ConversationStateRepository(db)
    await conversation.init()

    context = empty_business_context()
    context["specializations"] = ["круизы"]
    provisioned = await partners.provision_partner(
        USER_ID, "Тревел Клуб", "uxfix-ws",
        business_name="Тревел Клуб", business_type="independent_agent",
        short_description="Организуем туры.", context=context, ta_affiliated=False,
    )
    workspace_id = provisioned.workspace.id

    def check_text(*, source_text):
        rewritten_text = rewritten.get(source_text)
        return TextCheckResult(
            warnings=(), rewritten_text=rewritten_text, rewrite_warnings=(),
            generation_mode=None, ai_note=None,
        )

    llm = FakeLLMProvider()
    llm.check_text.side_effect = check_text

    _detach_module_routers()
    dispatcher = _build_dispatcher(
        frozenset({USER_ID}), journal=SimpleNamespace(add=AsyncMock(), last=AsyncMock()),
        llm_provider=llm, lead_radar_config=SimpleNamespace(), v2_menu_enabled=True,
        partner_repository=partners, artifact_repository=artifacts, work_repository=work,
        conversation_state_repository=conversation,
        subscription_repository=SimpleNamespace(
            resolve_access_state=AsyncMock(return_value="active")
        ),
        source_catalog_repository=SimpleNamespace(),
    )
    session = RecordingSession()
    bot = Bot(token="123456:TEST", session=session)

    async def create(title: str, text: str):
        artifact, _ = await artifacts.create_artifact_with_initial_version(
            workspace_id, artifact_type="post", title=title, content=text,
        )
        # exactly what the Radar/web generation does after saving a draft
        await ConversationStateService(conversation).record_artifact(
            workspace_id, USER_ID, artifact.id, active_module="lead_radar",
            current_task="radar_content_draft", last_action="radar_content_draft_created",
        )
        return artifact

    return SimpleNamespace(
        dispatcher=dispatcher, bot=bot, session=session, artifacts=artifacts,
        conversation=conversation, workspace_id=workspace_id, llm=llm, create=create,
    )


_update_counter = iter(range(100, 10_000))


async def _press(world, callback_data: str, message_id: int) -> None:
    update = Update(
        update_id=next(_update_counter),
        callback_query=CallbackQuery(
            id=f"cb{next(_update_counter)}", from_user=_user(), chat_instance="ci",
            data=callback_data,
            message=Message(
                message_id=message_id, date=datetime.now(timezone.utc),
                chat=Chat(id=CHAT_ID, type="private"), text="черновик",
            ),
        ),
    )
    await world.dispatcher.feed_update(world.bot, update)


def _texts(world) -> list[str]:
    return world.session.sent_texts()


async def _current_artifact_id(world):
    state = await world.conversation.get_state(world.workspace_id, USER_ID)
    return state.current_artifact_id if state else None


async def _contents(world, artifact_id: int) -> list[str]:
    versions = await world.artifacts.list_artifact_versions(world.workspace_id, artifact_id)
    return [v.content for v in sorted(versions, key=lambda v: v.version_number)]


def test_checking_older_a_after_publishing_newer_b_names_a_and_keeps_b_current(tmp_path) -> None:
    async def scenario():
        world = await _build(tmp_path, rewritten={A_TEXT: A_IMPROVED, B_TEXT: B_IMPROVED})
        a = await world.create(A_TITLE, A_TEXT)
        b = await world.create(B_TITLE, B_TEXT)
        assert await _current_artifact_id(world) == b.id

        # B is marked as published: its status buttons go, "🛡 Проверить текст" stays.
        await _press(world, f"{ARTIFACT_MARK_USED_PREFIX}{b.id}", message_id=2)
        edits = [m for m in world.session.requests if isinstance(m, EditMessageReplyMarkup)]
        assert len(edits) == 1
        remaining = [
            btn.callback_data for row in edits[0].reply_markup.inline_keyboard for btn in row
        ]
        assert remaining == [f"{ARTIFACT_CHECK_PREFIX}{b.id}", SOURCE_ACTION_MAIN_MENU]
        assert (await world.artifacts.get_artifact(world.workspace_id, b.id)).status == "used"

        # The user checks A (button under A's older message).
        world.session.requests.clear()
        await _press(world, f"{ARTIFACT_CHECK_PREFIX}{a.id}", message_id=1)
        card = _texts(world)[-1]
        assert f"Материал №{a.id}" in card
        assert "Ура, подешевело" in card
        assert "версия 1" in card
        assert "Казанский" not in card
        world.llm.check_text.assert_called_with(source_text=A_TEXT)

        # Saving the improved version: goes to A, names A, B stays current.
        world.session.requests.clear()
        await _press(world, f"{ARTIFACT_REVIEW_SAVE_PREFIX}{a.id}", message_id=3)
        confirmation = _texts(world)[-1]
        assert f"версия №2 материала №{a.id}" in confirmation
        assert "Ура, подешевело" in confirmation

        assert await _contents(world, a.id) == [A_TEXT, A_IMPROVED]
        assert await _contents(world, b.id) == [B_TEXT]  # B untouched
        assert await _current_artifact_id(world) == b.id  # pointer not rolled back to A
        assert (await world.artifacts.get_artifact(world.workspace_id, b.id)).status == "used"

    asyncio.run(scenario())


def test_checking_and_saving_b_is_all_about_b(tmp_path) -> None:
    async def scenario():
        world = await _build(tmp_path, rewritten={A_TEXT: A_IMPROVED, B_TEXT: B_IMPROVED})
        a = await world.create(A_TITLE, A_TEXT)
        b = await world.create(B_TITLE, B_TEXT)

        await _press(world, f"{ARTIFACT_CHECK_PREFIX}{b.id}", message_id=2)
        card = _texts(world)[-1]
        assert f"Материал №{b.id}" in card and "Санкт-Петербург" in card
        assert "Стамбул" not in card.split("Предлагаемая версия:")[0]
        world.llm.check_text.assert_called_with(source_text=B_TEXT)

        await _press(world, f"{ARTIFACT_REVIEW_SAVE_PREFIX}{b.id}", message_id=3)
        assert f"версия №2 материала №{b.id}" in _texts(world)[-1]
        assert await _contents(world, b.id) == [B_TEXT, B_IMPROVED]
        assert await _contents(world, a.id) == [A_TEXT]
        assert await _current_artifact_id(world) == b.id

    asyncio.run(scenario())


def test_card_without_suggested_rewrite_still_names_the_material(tmp_path) -> None:
    async def scenario():
        world = await _build(tmp_path, rewritten={A_TEXT: None})
        a = await world.create(A_TITLE, A_TEXT)

        await _press(world, f"{ARTIFACT_CHECK_PREFIX}{a.id}", message_id=1)

        card = _texts(world)[-1]
        assert f"Материал №{a.id}" in card and "версия 1" in card
        assert "Текущую версию можно оставить" in card

    asyncio.run(scenario())


def test_saving_the_only_artifact_still_records_it_as_current(tmp_path) -> None:
    """Behaviour kept: with no newer material, the improved one stays current."""
    async def scenario():
        world = await _build(tmp_path, rewritten={A_TEXT: A_IMPROVED})
        a = await world.create(A_TITLE, A_TEXT)
        await world.conversation.clear_state(world.workspace_id, USER_ID)
        assert await _current_artifact_id(world) is None

        await _press(world, f"{ARTIFACT_CHECK_PREFIX}{a.id}", message_id=1)
        await _press(world, f"{ARTIFACT_REVIEW_SAVE_PREFIX}{a.id}", message_id=2)

        assert await _current_artifact_id(world) == a.id

    asyncio.run(scenario())


def test_archived_newer_artifact_does_not_pin_the_pointer(tmp_path) -> None:
    """Only a still-ACTIVE newer material is protected; an archived one is not."""
    async def scenario():
        world = await _build(tmp_path, rewritten={A_TEXT: A_IMPROVED})
        a = await world.create(A_TITLE, A_TEXT)
        b = await world.create(B_TITLE, B_TEXT)
        await world.artifacts.update_artifact_status(world.workspace_id, b.id, "archived")

        await _press(world, f"{ARTIFACT_CHECK_PREFIX}{a.id}", message_id=1)
        await _press(world, f"{ARTIFACT_REVIEW_SAVE_PREFIX}{a.id}", message_id=2)

        assert await _current_artifact_id(world) == a.id

    asyncio.run(scenario())


def test_keep_later_button_does_not_touch_the_keyboard(tmp_path) -> None:
    """Unchanged: "📌 Оставить на потом" never edited the message."""
    async def scenario():
        world = await _build(tmp_path, rewritten={})
        a = await world.create(A_TITLE, A_TEXT)
        await _press(world, f"{ARTIFACT_KEEP_LATER_PREFIX}{a.id}", message_id=1)
        assert not [m for m in world.session.requests if isinstance(m, EditMessageReplyMarkup)]

    asyncio.run(scenario())


def test_saving_an_older_artifact_version_does_not_roll_current_material_back(tmp_path) -> None:
    """The pointer alone (isolated from the card/keyboard asserts above): after
    B was created, improving A must leave B as the user's current material."""
    async def scenario():
        world = await _build(tmp_path, rewritten={A_TEXT: A_IMPROVED})
        a = await world.create(A_TITLE, A_TEXT)
        b = await world.create(B_TITLE, B_TEXT)

        await _press(world, f"{ARTIFACT_CHECK_PREFIX}{a.id}", message_id=1)
        await _press(world, f"{ARTIFACT_REVIEW_SAVE_PREFIX}{a.id}", message_id=2)

        assert await _contents(world, a.id) == [A_TEXT, A_IMPROVED]
        assert await _current_artifact_id(world) == b.id

    asyncio.run(scenario())
