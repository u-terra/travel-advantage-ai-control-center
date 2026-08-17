from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.domain.business_profiles import BusinessContext, BusinessProfile
from app.domain.partners import WorkspaceContext
from app.domain.work import WorkItem, WorkItemView
from app.handlers import competitors as competitors_handlers
from app.handlers import daily_actions as daily_actions_handlers
from app.handlers import material_generation as material_generation_handlers
from app.handlers import materials as materials_handlers
from app.handlers import menu as menu_handlers
from app.handlers import profile as profile_handlers
from app.handlers import source_analysis as source_analysis_handlers
from app.handlers import sources as sources_handlers
from app.handlers import tasks as task_handlers
from app.handlers import text_review as text_review_handlers
from app.handlers.daily_actions import (
    _WORK_ITEM_NOT_FOUND,
    on_daily_action_dismiss,
    on_daily_action_done,
    on_daily_action_prompt,
    on_daily_action_replied,
    on_daily_action_snooze,
    show_daily_actions,
)
from app.handlers.menu import AwaitTask
from app.keyboards import (
    BTN_V2_DAILY_ACTIONS,
    DAILY_ACTION_DISMISS_PREFIX,
    DAILY_ACTION_DONE_PREFIX,
    DAILY_ACTION_PROMPT_PREFIX,
    DAILY_ACTION_REPLIED_PREFIX,
    DAILY_ACTION_SNOOZE_PREFIX,
)
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.partner_repository import PartnerRepository, empty_business_context
from app.repositories.work_repository import WorkRepository
from app.routing.modules import Module


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _iso(offset: timedelta) -> str:
    return (datetime.now(timezone.utc) + offset).isoformat()


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
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = _Message()
        self.answers: list[tuple[Any, dict]] = []

    async def answer(self, text: Any = None, **kwargs: Any) -> None:
        self.answers.append((text, kwargs))


class _State:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.state: Any = None

    async def update_data(self, **kwargs: Any) -> None:
        self.data.update(kwargs)

    async def set_state(self, state: Any) -> None:
        self.state = state

    async def clear(self) -> None:
        self.data.clear()
        self.state = None


def _context(workspace_id: int = 42) -> WorkspaceContext:
    return WorkspaceContext(100, workspace_id, "member", "active")


def _empty_business_profile(*, ta_affiliated: bool = False, incomplete: bool = False) -> BusinessProfile:
    context = BusinessContext(
        specializations=(), destinations=(), audiences=(), markets=(),
        positioning={"statement": "", "value_proposition": "", "differentiators": ()},
        communication={
            "tone": "", "formality": "", "emoji_preference": "",
            "preferred_terms": (), "banned_formulations": (), "cta_preference": "",
        },
        goals=(),
        content_preferences={"formats": (), "channels": (), "preferred_topics": (), "prohibited_topics": ()},
        public_contacts={}, claims=(),
    )
    return BusinessProfile(
        id=1, workspace_id=42, business_name="Acme", business_type="independent_agent",
        short_description="desc", profile_status="incomplete" if incomplete else "usable",
        schema_version=1, revision=1, context=context,
        created_at="now", updated_at="now", ta_affiliated=ta_affiliated,
    )


def _view(
    *, item_id: int, kind: str = "dialog", loop_state: str | None,
    subject_name: str | None, next_step: str = "", due_at: str | None = None,
    workspace_id: int = 42, lifecycle: str = "open",
) -> WorkItemView:
    now = _iso(timedelta(0))
    return WorkItemView(
        item=WorkItem(
            id=item_id, workspace_id=workspace_id, subject_id=item_id, kind=kind,
            lifecycle=lifecycle, loop_state=loop_state, next_step=next_step,
            due_at=due_at, ref_type=None, ref_id=None, created_at=now, updated_at=now,
            resolved_at=None if lifecycle == "open" else now,
        ),
        subject_name=subject_name,
    )


def _repository(
    *, open_items: list[WorkItemView] | None = None,
    waiting_items: list[WorkItemView] | None = None,
) -> Any:
    return AsyncMock(
        list_open_actionable=AsyncMock(return_value=open_items or []),
        list_waiting_not_due=AsyncMock(return_value=waiting_items or []),
        get_work_item=AsyncMock(return_value=None),
        mark_reply_received=AsyncMock(return_value=None),
        snooze=AsyncMock(return_value=None),
        resolve_done=AsyncMock(return_value=None),
        resolve_dismissed=AsyncMock(return_value=None),
    )


def _partner_repository(profile: BusinessProfile | None) -> Any:
    return AsyncMock(get_business_profile=AsyncMock(return_value=profile))


def _artifact_repository() -> Any:
    return AsyncMock(list_artifacts=AsyncMock(return_value=[]))


# ── show_daily_actions: rendering (AsyncMock repositories) ────────────────


def test_daily_actions_unavailable_without_workspace_context() -> None:
    message = _Message()
    work_repo = _repository()

    _run(show_daily_actions(
        message, None, work_repo, _partner_repository(None), _artifact_repository(),
    ))

    assert message.answers[0][0] == "Рабочее пространство недоступно."
    work_repo.list_open_actionable.assert_not_awaited()


def test_daily_actions_shows_active_dialog_with_continue_buttons() -> None:
    message = _Message()
    ivan = _view(item_id=1, loop_state="active_dialog", subject_name="Иван", next_step="Продолжить с Иваном")
    work_repo = _repository(open_items=[ivan])

    _run(show_daily_actions(
        message, _context(42), work_repo,
        _partner_repository(_empty_business_profile()), _artifact_repository(),
    ))

    work_repo.list_open_actionable.assert_awaited_once()
    assert work_repo.list_open_actionable.await_args.args[0] == 42
    texts = [text for text, _ in message.answers]
    assert any("Иван" in text for text in texts)
    action_message = next(m for m in message.answers if "Иван" in m[0])
    buttons = [b for row in action_message[1].inline_keyboard for b in row]
    assert [b.text for b in buttons] == ["💬 Продолжить разговор", "✅ Завершить", "🚫 Не актуально"]
    assert all(b.callback_data.endswith(":1") for b in buttons)


def test_daily_actions_shows_due_follow_up_with_write_again_and_snooze() -> None:
    message = _Message()
    olga = _view(item_id=2, loop_state="waiting_reply", subject_name="Ольга", next_step="Написать снова")
    work_repo = _repository(open_items=[olga])

    _run(show_daily_actions(
        message, _context(42), work_repo,
        _partner_repository(_empty_business_profile()), _artifact_repository(),
    ))

    action_message = next(m for m in message.answers if "Ольга" in m[0])
    buttons = [b for row in action_message[1].inline_keyboard for b in row]
    assert [b.text for b in buttons] == ["💬 Написать снова", "⏭ Перенести", "✅ Завершить"]


def test_daily_actions_waiting_block_is_separate_from_actions() -> None:
    """Future-due waiting_reply не входит в 1-3 actions, но показан отдельным
    блоком «Ждём ответ» со своим набором кнопок."""
    message = _Message()
    olga_waiting = _view(
        item_id=3, loop_state="waiting_reply", subject_name="Ольга",
        next_step="Ждём Ольгу", due_at=_iso(timedelta(days=2)),
    )
    work_repo = _repository(waiting_items=[olga_waiting])

    _run(show_daily_actions(
        message, _context(42), work_repo,
        _partner_repository(_empty_business_profile()), _artifact_repository(),
    ))

    texts = [text for text, _ in message.answers]
    assert not any(text.startswith("1.") and "Ольга" in text for text in texts)
    waiting_message = next(m for m in message.answers if "Ждём ответ: Ольга" in m[0])
    buttons = [b for row in waiting_message[1].inline_keyboard for b in row]
    assert [b.text for b in buttons] == ["💬 Ответил(а)", "✅ Завершить", "🚫 Не актуально"]


def test_daily_actions_non_ta_workspace_has_no_ta_wording() -> None:
    message = _Message()
    work_repo = _repository()

    _run(show_daily_actions(
        message, _context(42), work_repo,
        _partner_repository(_empty_business_profile(ta_affiliated=False)), _artifact_repository(),
    ))

    all_text = "\n".join(text for text, _ in message.answers)
    assert "Travel Advantage" not in all_text
    assert "TA" not in all_text


def test_daily_actions_ta_workspace_gets_ta_fallback_wording() -> None:
    message = _Message()
    work_repo = _repository()

    _run(show_daily_actions(
        message, _context(42), work_repo,
        _partner_repository(_empty_business_profile(ta_affiliated=True)), _artifact_repository(),
    ))

    all_text = "\n".join(text for text, _ in message.answers)
    assert "Travel Advantage" in all_text


# ── callback handlers: guard behaviour + repository wiring (AsyncMock) ────


def _active_dialog_item(item_id: int = 5, subject_id: int | None = 1) -> WorkItem:
    return WorkItem(
        id=item_id, workspace_id=42, subject_id=subject_id, kind="dialog",
        lifecycle="open", loop_state="active_dialog", next_step="Продолжить с Иваном",
        due_at=None, ref_type=None, ref_id=None, created_at="now", updated_at="now",
        resolved_at=None,
    )


def _due_follow_up_item(item_id: int = 6, subject_id: int | None = 2) -> WorkItem:
    return WorkItem(
        id=item_id, workspace_id=42, subject_id=subject_id, kind="dialog",
        lifecycle="open", loop_state="waiting_reply", next_step="Написать Ольге снова",
        due_at="2026-01-01T00:00:00+00:00", ref_type=None, ref_id=None,
        created_at="now", updated_at="now", resolved_at=None,
    )


def test_on_daily_action_prompt_bridges_active_dialog_into_travel_assistant_flow() -> None:
    """«Продолжить разговор» — не тупик: переводит FSM в тот же вход, что и
    кнопка «Ответить клиенту» (forced_module=TRAVEL_ASSISTANT + AwaitTask.waiting),
    сохраняя work_item_id/subject_name, и не мутирует lifecycle/loop_state."""
    work_repo = _repository()
    work_repo.get_work_item = AsyncMock(return_value=_active_dialog_item(subject_id=1))
    subject = type("S", (), {"name": "Иван"})()
    work_repo.get_subject = AsyncMock(return_value=subject)
    callback = _Callback(f"{DAILY_ACTION_PROMPT_PREFIX}5")
    state = _State()

    _run(on_daily_action_prompt(callback, state, _context(42), work_repo))

    work_repo.get_work_item.assert_awaited_once_with(42, 5)
    work_repo.get_subject.assert_awaited_once_with(42, 1)
    assert state.data["forced_module"] == Module.TRAVEL_ASSISTANT.value
    assert state.data["skip_route_card"] is True
    assert state.data["daily_action_work_item_id"] == 5
    assert state.data["daily_action_subject_name"] == "Иван"
    assert state.state == AwaitTask.waiting
    work_repo.resolve_done.assert_not_awaited()
    work_repo.mark_reply_received.assert_not_awaited()
    work_repo.snooze.assert_not_awaited()
    work_repo.resolve_dismissed.assert_not_awaited()
    prompt_text = callback.message.answers[0][0]
    assert prompt_text == (
        "Контакт: Иван. Пришлите его последнее сообщение, "
        "и я помогу подготовить ответ."
    )


def test_on_daily_action_prompt_bridges_due_follow_up_with_different_wording() -> None:
    work_repo = _repository()
    work_repo.get_work_item = AsyncMock(return_value=_due_follow_up_item(subject_id=2))
    subject = type("S", (), {"name": "Ольга"})()
    work_repo.get_subject = AsyncMock(return_value=subject)
    callback = _Callback(f"{DAILY_ACTION_PROMPT_PREFIX}6")
    state = _State()

    _run(on_daily_action_prompt(callback, state, _context(42), work_repo))

    prompt_text = callback.message.answers[0][0]
    assert prompt_text == "Контакт: Ольга. Опишите, по какому поводу хотите написать снова."
    assert "последнее сообщение" not in prompt_text
    assert state.data["daily_action_subject_name"] == "Ольга"


def test_on_daily_action_prompt_without_subject_uses_generic_wording() -> None:
    work_repo = _repository()
    work_repo.get_work_item = AsyncMock(return_value=_active_dialog_item(subject_id=None))
    callback = _Callback(f"{DAILY_ACTION_PROMPT_PREFIX}5")
    state = _State()

    _run(on_daily_action_prompt(callback, state, _context(42), work_repo))

    work_repo.get_subject.assert_not_awaited()
    assert state.data["daily_action_subject_name"] is None
    assert callback.message.answers[0][0] == (
        "Пришлите последнее сообщение собеседника, и я помогу подготовить ответ."
    )
    assert "Контакт:" not in callback.message.answers[0][0]


@pytest.mark.parametrize(
    ("loop_state", "subject_name", "expected"),
    [
        (
            "active_dialog", "Иван",
            "Контакт: Иван. Пришлите его последнее сообщение, и я помогу подготовить ответ.",
        ),
        (
            "active_dialog", "Мария",
            "Контакт: Мария. Пришлите его последнее сообщение, и я помогу подготовить ответ.",
        ),
        (
            "waiting_reply", "Ольга",
            "Контакт: Ольга. Опишите, по какому поводу хотите написать снова.",
        ),
    ],
)
def test_bridge_prompt_text_never_declines_subject_name(
    loop_state: str, subject_name: str, expected: str,
) -> None:
    """Имя вставляется как есть (именительный падеж) вне зависимости от
    падежа/окончания — никакой попытки программного склонения "Мария" в
    "Марии" и т.п."""
    from app.handlers.daily_actions import _bridge_prompt_text

    assert _bridge_prompt_text(loop_state, subject_name) == expected
    assert subject_name in expected


def test_on_daily_action_prompt_reports_stale_item_and_does_not_touch_state() -> None:
    work_repo = _repository()
    callback = _Callback(f"{DAILY_ACTION_PROMPT_PREFIX}5")
    state = _State()

    _run(on_daily_action_prompt(callback, state, _context(42), work_repo))

    assert callback.answers[0] == (_WORK_ITEM_NOT_FOUND, {"show_alert": True})
    assert state.data == {}
    assert state.state is None


def test_on_daily_action_prompt_rejects_done_item_via_stale_callback() -> None:
    """Старая callback-кнопка на уже завершённый (в другом чате/сессии)
    work_item не должна вести в Reply flow: FSM не трогается."""
    work_repo = _repository()
    done_item = WorkItem(
        id=5, workspace_id=42, subject_id=1, kind="dialog", lifecycle="done",
        loop_state="active_dialog", next_step="Продолжить с Иваном", due_at=None,
        ref_type=None, ref_id=None, created_at="now", updated_at="now", resolved_at="now",
    )
    work_repo.get_work_item = AsyncMock(return_value=done_item)
    callback = _Callback(f"{DAILY_ACTION_PROMPT_PREFIX}5")
    state = _State()

    _run(on_daily_action_prompt(callback, state, _context(42), work_repo))

    assert callback.answers[0] == (_WORK_ITEM_NOT_FOUND, {"show_alert": True})
    assert state.data == {}
    assert state.state is None
    work_repo.get_subject.assert_not_awaited()


def test_on_daily_action_prompt_rejects_dismissed_item_via_stale_callback() -> None:
    work_repo = _repository()
    dismissed_item = WorkItem(
        id=5, workspace_id=42, subject_id=None, kind="dialog", lifecycle="dismissed",
        loop_state="waiting_reply", next_step="", due_at=None, ref_type=None, ref_id=None,
        created_at="now", updated_at="now", resolved_at="now",
    )
    work_repo.get_work_item = AsyncMock(return_value=dismissed_item)
    callback = _Callback(f"{DAILY_ACTION_PROMPT_PREFIX}5")
    state = _State()

    _run(on_daily_action_prompt(callback, state, _context(42), work_repo))

    assert callback.answers[0] == (_WORK_ITEM_NOT_FOUND, {"show_alert": True})
    assert state.data == {}


def test_on_daily_action_prompt_rejects_snoozed_waiting_item_with_future_due_at() -> None:
    """Старая кнопка «Написать снова» на waiting_reply, который тем временем
    перенесли (due_at в будущем) — ещё не due follow-up, в Reply flow не пускаем."""
    work_repo = _repository()
    snoozed_item = WorkItem(
        id=6, workspace_id=42, subject_id=2, kind="dialog", lifecycle="open",
        loop_state="waiting_reply", next_step="Написать Ольге снова",
        due_at=_iso(timedelta(days=2)), ref_type=None, ref_id=None,
        created_at="now", updated_at="now", resolved_at=None,
    )
    work_repo.get_work_item = AsyncMock(return_value=snoozed_item)
    callback = _Callback(f"{DAILY_ACTION_PROMPT_PREFIX}6")
    state = _State()

    _run(on_daily_action_prompt(callback, state, _context(42), work_repo))

    assert callback.answers[0] == (_WORK_ITEM_NOT_FOUND, {"show_alert": True})
    assert state.data == {}
    assert state.state is None
    work_repo.get_subject.assert_not_awaited()


def test_on_daily_action_prompt_fails_closed_without_workspace_context() -> None:
    work_repo = _repository()
    callback = _Callback(f"{DAILY_ACTION_PROMPT_PREFIX}5")
    state = _State()

    _run(on_daily_action_prompt(callback, state, None, work_repo))

    assert callback.answers[0] == (_WORK_ITEM_NOT_FOUND, {"show_alert": True})
    work_repo.get_work_item.assert_not_awaited()
    assert state.data == {}


def test_on_daily_action_prompt_foreign_workspace_item_is_rejected() -> None:
    """workspace_context.workspace_id=99, но work_item реально принадлежит 42:
    get_work_item сам workspace-scoped (WHERE workspace_id=?), поэтому мок
    здесь эмулирует именно это — реальную проверку см. в WorkRepository."""
    work_repo = _repository()
    work_repo.get_work_item = AsyncMock(return_value=None)
    callback = _Callback(f"{DAILY_ACTION_PROMPT_PREFIX}5")
    state = _State()

    _run(on_daily_action_prompt(callback, state, _context(99), work_repo))

    work_repo.get_work_item.assert_awaited_once_with(99, 5)
    assert callback.answers[0] == (_WORK_ITEM_NOT_FOUND, {"show_alert": True})
    assert state.data == {}


def test_on_daily_action_replied_transitions_waiting_to_active_dialog() -> None:
    work_repo = _repository()
    updated = WorkItem(
        id=5, workspace_id=42, subject_id=1, kind="dialog", lifecycle="open",
        loop_state="active_dialog", next_step="", due_at=None, ref_type=None,
        ref_id=None, created_at="now", updated_at="now", resolved_at=None,
    )
    work_repo.mark_reply_received = AsyncMock(return_value=updated)
    callback = _Callback(f"{DAILY_ACTION_REPLIED_PREFIX}5")

    _run(on_daily_action_replied(callback, _context(42), work_repo))

    work_repo.mark_reply_received.assert_awaited_once_with(42, 5)
    assert callback.message.reply_markup_edits == [None]


def test_on_daily_action_snooze_moves_due_at_two_days_forward() -> None:
    work_repo = _repository()
    work_repo.snooze = AsyncMock(return_value=object())
    callback = _Callback(f"{DAILY_ACTION_SNOOZE_PREFIX}7")

    before = datetime.now(timezone.utc)
    _run(on_daily_action_snooze(callback, _context(42), work_repo))
    after = datetime.now(timezone.utc)

    work_repo.snooze.assert_awaited_once()
    args, kwargs = work_repo.snooze.await_args
    assert args == (42, 7)
    due_at = datetime.fromisoformat(kwargs["due_at"])
    assert before + timedelta(days=2) <= due_at <= after + timedelta(days=2)


def test_on_daily_action_done_resolves_item() -> None:
    work_repo = _repository()
    work_repo.resolve_done = AsyncMock(return_value=object())
    callback = _Callback(f"{DAILY_ACTION_DONE_PREFIX}9")

    _run(on_daily_action_done(callback, _context(42), work_repo))

    work_repo.resolve_done.assert_awaited_once_with(42, 9)


def test_on_daily_action_dismiss_resolves_item() -> None:
    work_repo = _repository()
    work_repo.resolve_dismissed = AsyncMock(return_value=object())
    callback = _Callback(f"{DAILY_ACTION_DISMISS_PREFIX}9")

    _run(on_daily_action_dismiss(callback, _context(42), work_repo))

    work_repo.resolve_dismissed.assert_awaited_once_with(42, 9)


def test_callback_handlers_fail_closed_without_workspace_context() -> None:
    work_repo = _repository()
    for handler, prefix in (
        (on_daily_action_replied, DAILY_ACTION_REPLIED_PREFIX),
        (on_daily_action_snooze, DAILY_ACTION_SNOOZE_PREFIX),
        (on_daily_action_done, DAILY_ACTION_DONE_PREFIX),
        (on_daily_action_dismiss, DAILY_ACTION_DISMISS_PREFIX),
    ):
        callback = _Callback(f"{prefix}1")
        _run(handler(callback, None, work_repo))
        assert callback.answers[0] == (_WORK_ITEM_NOT_FOUND, {"show_alert": True})
    work_repo.mark_reply_received.assert_not_awaited()
    work_repo.snooze.assert_not_awaited()
    work_repo.resolve_done.assert_not_awaited()
    work_repo.resolve_dismissed.assert_not_awaited()


# ── end-to-end with real WorkRepository/PartnerRepository/ArtifactRepository ─


def _provisioned_workspace(tmp_path: Path, *, ta_affiliated: bool = False) -> tuple[Path, int]:
    db_path = tmp_path / "workspace.sqlite3"
    partners = PartnerRepository(db_path)
    _run(partners.init())
    context = empty_business_context()
    context["specializations"] = ["cruises"]
    provisioned = _run(partners.provision_partner(
        111222333, "Independent Agency", "independent-agency",
        business_name="Independent Agency", business_type="independent_agent",
        short_description="Сторонний тревел-агент.", context=context,
        ta_affiliated=ta_affiliated,
    ))
    return db_path, provisioned.workspace.id


def _real_stack(tmp_path: Path, *, ta_affiliated: bool = False):
    db_path, workspace_id = _provisioned_workspace(tmp_path, ta_affiliated=ta_affiliated)
    work_repo = WorkRepository(db_path)
    _run(work_repo.init())
    partner_repo = PartnerRepository(db_path)
    _run(partner_repo.init())
    artifact_repo = ArtifactRepository(db_path)
    _run(artifact_repo.init())
    return work_repo, partner_repo, artifact_repo, workspace_id


def test_end_to_end_done_removes_active_dialog_from_screen(tmp_path: Path) -> None:
    work_repo, partner_repo, artifact_repo, workspace_id = _real_stack(tmp_path)
    ivan = _run(work_repo.get_or_create_subject(workspace_id, "Иван"))
    item = _run(work_repo.create_work_item(
        workspace_id, kind="dialog", subject_id=ivan.id, loop_state="active_dialog",
        next_step="Продолжить с Иваном",
    ))

    before = _Message()
    _run(show_daily_actions(before, _context(workspace_id), work_repo, partner_repo, artifact_repo))
    assert any("Иван" in text for text, _ in before.answers)

    callback = _Callback(f"{DAILY_ACTION_DONE_PREFIX}{item.id}")
    _run(on_daily_action_done(callback, _context(workspace_id), work_repo))

    after = _Message()
    _run(show_daily_actions(after, _context(workspace_id), work_repo, partner_repo, artifact_repo))
    assert not any("Иван" in text for text, _ in after.answers)


def test_end_to_end_dismiss_removes_waiting_item_from_screen(tmp_path: Path) -> None:
    work_repo, partner_repo, artifact_repo, workspace_id = _real_stack(tmp_path)
    olga = _run(work_repo.get_or_create_subject(workspace_id, "Ольга"))
    item = _run(work_repo.create_work_item(
        workspace_id, kind="dialog", subject_id=olga.id, loop_state="waiting_reply",
        next_step="Ждём Ольгу", due_at=_iso(timedelta(days=2)),
    ))

    before = _Message()
    _run(show_daily_actions(before, _context(workspace_id), work_repo, partner_repo, artifact_repo))
    assert any("Ольга" in text for text, _ in before.answers)

    callback = _Callback(f"{DAILY_ACTION_DISMISS_PREFIX}{item.id}")
    _run(on_daily_action_dismiss(callback, _context(workspace_id), work_repo))

    after = _Message()
    _run(show_daily_actions(after, _context(workspace_id), work_repo, partner_repo, artifact_repo))
    assert not any("Ольга" in text for text, _ in after.answers)


def test_end_to_end_replied_moves_waiting_subject_into_actions(tmp_path: Path) -> None:
    work_repo, partner_repo, artifact_repo, workspace_id = _real_stack(tmp_path)
    ivan = _run(work_repo.get_or_create_subject(workspace_id, "Иван"))
    item = _run(work_repo.create_work_item(
        workspace_id, kind="dialog", subject_id=ivan.id, loop_state="waiting_reply",
        next_step="Ждём Ивана", due_at=_iso(timedelta(days=2)),
    ))

    waiting_screen = _Message()
    _run(show_daily_actions(waiting_screen, _context(workspace_id), work_repo, partner_repo, artifact_repo))
    assert any("Ждём ответ: Иван" in text for text, _ in waiting_screen.answers)
    assert not any(text.startswith("1.") and "Иван" in text for text, _ in waiting_screen.answers)

    callback = _Callback(f"{DAILY_ACTION_REPLIED_PREFIX}{item.id}")
    _run(on_daily_action_replied(callback, _context(workspace_id), work_repo))

    active_screen = _Message()
    _run(show_daily_actions(active_screen, _context(workspace_id), work_repo, partner_repo, artifact_repo))
    assert any(text.startswith("1.") and "Иван" in text for text, _ in active_screen.answers)
    assert not any("Ждём ответ: Иван" in text for text, _ in active_screen.answers)


def test_end_to_end_snooze_moves_item_from_actions_to_waiting(tmp_path: Path) -> None:
    work_repo, partner_repo, artifact_repo, workspace_id = _real_stack(tmp_path)
    item = _run(work_repo.create_work_item(
        workspace_id, kind="dialog", loop_state="waiting_reply",
        next_step="Написать снова", due_at=_iso(-timedelta(minutes=1)),
    ))

    due_screen = _Message()
    _run(show_daily_actions(due_screen, _context(workspace_id), work_repo, partner_repo, artifact_repo))
    assert any(text.startswith("1.") for text, _ in due_screen.answers)

    callback = _Callback(f"{DAILY_ACTION_SNOOZE_PREFIX}{item.id}")
    _run(on_daily_action_snooze(callback, _context(workspace_id), work_repo))

    snoozed = _run(work_repo.get_work_item(workspace_id, item.id))
    assert snoozed is not None
    assert snoozed.due_at is not None
    assert datetime.fromisoformat(snoozed.due_at) > datetime.now(timezone.utc)

    after_screen = _Message()
    _run(show_daily_actions(after_screen, _context(workspace_id), work_repo, partner_repo, artifact_repo))
    # Слот действий не пуст — его теперь занимает playbook-fallback (это верно
    # по дизайну: fallback всегда кандидат, просто самого низкого приоритета).
    # Важно, что именно перенесённый due-follow-up там больше не значится —
    # он легитимно всё ещё виден в блоке «Ждём ответ» с тем же next_step,
    # поэтому проверяем конкретно action-карточки, а не весь экран целиком.
    action_texts = [text for text, _ in after_screen.answers if text[:2] in {"1.", "2.", "3."}]
    assert not any("Написать снова" in text for text in action_texts)
    assert any("Ждём ответ" in text for text, _ in after_screen.answers)


def test_end_to_end_tenant_isolation(tmp_path: Path) -> None:
    db_path, workspace_a = _provisioned_workspace(tmp_path)
    partner_repo = PartnerRepository(db_path)
    _run(partner_repo.init())
    context_b = empty_business_context()
    context_b["specializations"] = ["ski"]
    provisioned_b = _run(partner_repo.provision_partner(
        444555666, "Other Agency", "other-agency",
        business_name="Other Agency", business_type="independent_agent",
        short_description="Другое агентство.", context=context_b,
    ))
    workspace_b = provisioned_b.workspace.id

    work_repo = WorkRepository(db_path)
    _run(work_repo.init())
    artifact_repo = ArtifactRepository(db_path)
    _run(artifact_repo.init())

    ivan = _run(work_repo.get_or_create_subject(workspace_a, "Иван"))
    _run(work_repo.create_work_item(
        workspace_a, kind="dialog", subject_id=ivan.id, loop_state="active_dialog",
        next_step="Продолжить с Иваном",
    ))

    screen_b = _Message()
    _run(show_daily_actions(screen_b, _context(workspace_b), work_repo, partner_repo, artifact_repo))
    assert not any("Иван" in text for text, _ in screen_b.answers)


# ── bridge into Reply flow: real WorkRepository, lifecycle/loop_state untouched ─


def test_end_to_end_bridge_preserves_lifecycle_and_loop_state(tmp_path: Path) -> None:
    """Переход в Reply flow сам по себе не меняет work_item: до и после
    вызова on_daily_action_prompt lifecycle/loop_state/due_at идентичны —
    их меняют только явные кнопки Ответил(а)/Завершить/Перенести/Не актуально."""
    work_repo, partner_repo, artifact_repo, workspace_id = _real_stack(tmp_path)
    ivan = _run(work_repo.get_or_create_subject(workspace_id, "Иван"))
    item = _run(work_repo.create_work_item(
        workspace_id, kind="dialog", subject_id=ivan.id, loop_state="active_dialog",
        next_step="Продолжить с Иваном",
    ))

    callback = _Callback(f"{DAILY_ACTION_PROMPT_PREFIX}{item.id}")
    state = _State()
    _run(on_daily_action_prompt(callback, state, _context(workspace_id), work_repo))

    after = _run(work_repo.get_work_item(workspace_id, item.id))
    assert after is not None
    assert after.lifecycle == item.lifecycle
    assert after.loop_state == item.loop_state
    assert after.due_at == item.due_at
    assert after.updated_at == item.updated_at

    assert state.state == AwaitTask.waiting
    assert state.data["daily_action_work_item_id"] == item.id
    assert state.data["daily_action_subject_name"] == "Иван"
    assert state.data["forced_module"] == Module.TRAVEL_ASSISTANT.value


def test_end_to_end_bridge_rejects_work_item_from_other_workspace(tmp_path: Path) -> None:
    db_path, workspace_a = _provisioned_workspace(tmp_path)
    partner_repo = PartnerRepository(db_path)
    _run(partner_repo.init())
    context_b = empty_business_context()
    context_b["specializations"] = ["ski"]
    provisioned_b = _run(partner_repo.provision_partner(
        444555666, "Other Agency", "other-agency",
        business_name="Other Agency", business_type="independent_agent",
        short_description="Другое агентство.", context=context_b,
    ))
    workspace_b = provisioned_b.workspace.id

    work_repo = WorkRepository(db_path)
    _run(work_repo.init())
    ivan = _run(work_repo.get_or_create_subject(workspace_a, "Иван"))
    item = _run(work_repo.create_work_item(
        workspace_a, kind="dialog", subject_id=ivan.id, loop_state="active_dialog",
        next_step="Продолжить с Иваном",
    ))

    callback = _Callback(f"{DAILY_ACTION_PROMPT_PREFIX}{item.id}")
    state = _State()
    _run(on_daily_action_prompt(callback, state, _context(workspace_b), work_repo))

    assert callback.answers[0] == (_WORK_ITEM_NOT_FOUND, {"show_alert": True})
    assert state.data == {}
    assert state.state is None
    # work_item в своём workspace не пострадал.
    untouched = _run(work_repo.get_work_item(workspace_a, item.id))
    assert untouched is not None and untouched.lifecycle == "open"


# ── роутинг «☀️ Что делать сегодня» в реальном порядке регистрации ────────


# app.handlers.build_router() присоединяет module-level singleton-роутеры
# (router.parent_router можно установить только один раз — см.
# aiogram.dispatcher.router.Router.parent_router) — вызывать build_router()
# повторно внутри тестов нельзя, поэтому (как и test_v2_menu.py рядом)
# здесь напрямую перечислены те же router-объекты, что build_router()
# регистрирует в app/handlers/__init__.py, СТРОГО в том же порядке
# (start/consent пропущены: CommandStart-фильтр требует kwarg `bot`,
# которого этот тестовый гарнесс не поставляет, а для проверки перехвата
# текстовой кнопки они не участвуют — оба реагируют только на команды/callback).
_REAL_ROUTER_ORDER = (
    menu_handlers.router,
    daily_actions_handlers.router,
    sources_handlers.router,
    source_analysis_handlers.router,
    material_generation_handlers.router,
    text_review_handlers.router,
    profile_handlers.router,
    materials_handlers.router,
    competitors_handlers.router,
    task_handlers.router,
)


async def _first_matching_handler_in_real_router_order(
    text: str, **workflow_data: Any,
) -> str | None:
    message = _Message(text)
    data = {"raw_state": None, **workflow_data}
    for sub_router in _REAL_ROUTER_ORDER:
        for handler in sub_router.message.handlers:
            matched, _ = await handler.check(message, **data)
            if matched:
                return handler.callback.__name__
    return None


def test_daily_actions_button_reaches_handler_in_real_registration_order() -> None:
    handler_name = _run(_first_matching_handler_in_real_router_order(
        BTN_V2_DAILY_ACTIONS, v2_menu_enabled=True,
    ))
    assert handler_name == "show_daily_actions"


def test_daily_actions_button_is_ordinary_text_when_v2_disabled() -> None:
    """Без флага кнопка не должна попасть в daily_actions — она должна дойти
    туда же, куда и любой другой нераспознанный текст (on_free_text)."""
    for workflow_data in ({"v2_menu_enabled": False}, {}):
        handler_name = _run(_first_matching_handler_in_real_router_order(
            BTN_V2_DAILY_ACTIONS, **workflow_data,
        ))
        assert handler_name == "on_free_text"
