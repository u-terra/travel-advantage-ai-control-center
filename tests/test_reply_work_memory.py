"""Интеграционные тесты: Reply flow (TRAVEL_ASSISTANT) <-> рабочая память.

Только новый интеграционный слой этого этапа — subject-промпт, создание/
обновление work_item вокруг существующего Reply flow, кнопки подтверждения.
Сама генерация черновика (Content Factory/LLM) не переделывается и не
дублируется — везде используется FakeLLMProvider, как и в остальных тестах
tasks.py/menu.py.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.domain.partners import WorkspaceContext
from app.domain.work import work_item_revision
from app.handlers.daily_actions import (
    _WORK_ITEM_NOT_FOUND,
    on_daily_action_dismiss,
    on_daily_action_prompt,
    on_daily_action_replied,
    on_reply_confirm_dismiss,
    on_reply_confirm_later,
    on_reply_confirm_sent,
)
from app.handlers.menu import AwaitReplySubject, AwaitTask, on_v2_category
from app.handlers.tasks import on_reply_subject_received, on_task_after_button
from app.keyboards import (
    BTN_V2_CLIENT_REPLY,
    DAILY_ACTION_DISMISS_PREFIX,
    DAILY_ACTION_REPLIED_PREFIX,
    REPLY_CONFIRM_DISMISS_PREFIX,
    REPLY_CONFIRM_LATER_PREFIX,
    REPLY_CONFIRM_SENT_PREFIX,
)
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.partner_repository import PartnerRepository, empty_business_context
from app.repositories.work_repository import WorkRepository
from app.services.llm.models import ContentDraft
from tests.llm_fakes import FakeLLMProvider


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _iso(offset: timedelta) -> str:
    return (datetime.now(timezone.utc) + offset).isoformat()


class _Message:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.answers: list[tuple[str, dict]] = []
        self.reply_markup_edits: list[Any] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append((text, kwargs))

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
    def __init__(self, data: dict | None = None) -> None:
        self.data: dict[str, Any] = data or {}
        self.state: Any = None

    async def get_data(self) -> dict:
        return self.data

    async def update_data(self, **kwargs: Any) -> None:
        self.data.update(kwargs)

    async def set_state(self, state: Any) -> None:
        self.state = state

    async def clear(self) -> None:
        self.data = {}
        self.state = None


def _journal() -> Any:
    return SimpleNamespace(add=AsyncMockLike())


class AsyncMockLike:
    """Минимальная async-заглушка без unittest.mock, чтобы не тянуть лишнее —
    только запись факта вызова, она здесь не проверяется отдельно."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> int:
        self.calls.append(args)
        return 1


def _ctx(workspace_id: int) -> WorkspaceContext:
    return WorkspaceContext(100, workspace_id, "member", "active")


def _stack(tmp_path: Path, *, ta_affiliated: bool = False) -> tuple[
    PartnerRepository, WorkRepository, ArtifactRepository, int,
]:
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
    workspace_id = provisioned.workspace.id
    work_repo = WorkRepository(db_path)
    _run(work_repo.init())
    artifact_repo = ArtifactRepository(db_path)
    _run(artifact_repo.init())
    return partners, work_repo, artifact_repo, workspace_id


async def _start_reply(state: _State) -> _Message:
    message = _Message(BTN_V2_CLIENT_REPLY)
    await on_v2_category(message, state)
    return message


async def _submit_subject(
    state: _State, work_repo: WorkRepository, workspace_id: int, name: str,
) -> _Message:
    """Короткие имена/метки (все случаи в этом файле) не задействуют journal/
    llm_provider/partner_repository — on_reply_subject_received использует их
    только когда текст похож на уже присланное сообщение клиента (см.
    _looks_like_client_message в app/handlers/tasks.py), поэтому здесь
    достаточно минимальных заглушек."""
    message = _Message(name)
    await on_reply_subject_received(
        message, state, _journal(), FakeLLMProvider(), work_repo,
        _ctx(workspace_id), PartnerRepository(work_repo.db_path),
    )
    return message


async def _submit_client_message(
    state: _State, provider: FakeLLMProvider, workspace_id: int,
    partner_repo: PartnerRepository, work_repo: WorkRepository,
    artifact_repo: ArtifactRepository, text: str,
) -> _Message:
    message = _Message(text)
    await on_task_after_button(
        message, state, _journal(), provider, _ctx(workspace_id), partner_repo,
        work_repository=work_repo, artifact_repository=artifact_repo,
    )
    return message


def _keyboard_callback_data(message: _Message, row: int = 0) -> str:
    markup = message.answers[-1][1]["reply_markup"]
    return markup.inline_keyboard[row][0].callback_data


def _keyboard_work_item_id(message: _Message) -> int:
    # callback_data = "<prefix>:<work_item_id>:<revision>" — id предпоследний.
    return int(_keyboard_callback_data(message).rsplit(":", 2)[-2])


async def _current_reply_callback(
    work_repo: WorkRepository, workspace_id: int, work_item_id: int, prefix: str,
) -> _Callback:
    """callback с revision, совпадающим с ТЕКУЩИМ updated_at item — то, что
    реально показала бы актуальная клавиатура прямо сейчас."""
    item = await work_repo.get_work_item(workspace_id, work_item_id)
    assert item is not None
    return _Callback(f"{prefix}{work_item_id}:{work_item_revision(item)}")


# ── 1. обычный Reply flow спрашивает subject, затем создаёт WorkSubject ────


def test_reply_flow_asks_subject_then_creates_it(tmp_path: Path) -> None:
    _, work_repo, _, workspace_id = _stack(tmp_path)
    state = _State()

    prompt_message = _run(_start_reply(state))
    assert state.state == AwaitReplySubject.waiting
    assert "Кому отвечаем" in prompt_message.answers[0][0]

    subject_message = _run(_submit_subject(state, work_repo, workspace_id, "Иван"))
    assert state.state == AwaitTask.waiting
    assert state.data["daily_action_subject_name"] == "Иван"
    subject = _run(work_repo.get_subject(workspace_id, state.data["daily_action_subject_id"]))
    assert subject is not None and subject.name == "Иван"
    assert "Опишите вопрос клиента" in subject_message.answers[0][0]


# ── 2. «Иван»/«ИВАН»/«иван» переиспользуют одного subject ──────────────────


def test_reply_subject_normalization_reuses_same_subject(tmp_path: Path) -> None:
    _, work_repo, _, workspace_id = _stack(tmp_path)
    subject_ids = set()
    for name in ("Иван", "ИВАН", "  иван  "):
        state = _State()
        _run(_submit_subject(state, work_repo, workspace_id, name))
        subject_ids.add(state.data["daily_action_subject_id"])
    assert len(subject_ids) == 1


# ── 3. bridge из daily_actions не спрашивает subject повторно ──────────────


def test_bridge_from_daily_actions_skips_subject_question(tmp_path: Path) -> None:
    _, work_repo, _, workspace_id = _stack(tmp_path)
    ivan = _run(work_repo.get_or_create_subject(workspace_id, "Иван"))
    item = _run(work_repo.create_work_item(
        workspace_id, kind="dialog", subject_id=ivan.id, loop_state="active_dialog",
        next_step="Продолжить с Иваном",
    ))

    callback = _Callback(f"daily_action:prompt:{item.id}")
    state = _State()
    _run(on_daily_action_prompt(callback, state, _ctx(workspace_id), work_repo))

    assert state.state == AwaitTask.waiting
    assert state.state != AwaitReplySubject.waiting
    assert state.data["daily_action_subject_id"] == ivan.id
    assert state.data["daily_action_work_item_id"] == item.id


# ── 4. новый Reply создаёт ровно один active_dialog work_item ──────────────


def test_new_reply_creates_exactly_one_active_dialog_work_item(tmp_path: Path) -> None:
    partners, work_repo, artifact_repo, workspace_id = _stack(tmp_path)
    state = _State()
    _run(_start_reply(state))
    _run(_submit_subject(state, work_repo, workspace_id, "Иван"))
    provider = FakeLLMProvider(draft=ContentDraft("Черновик ответа Ивану", ()))

    message = _run(_submit_client_message(
        state, provider, workspace_id, partners, work_repo, artifact_repo,
        "Иван написал: интересно ли это?",
    ))

    provider.generate_draft.assert_called_once()
    now = _iso(timedelta(0))
    open_items = _run(work_repo.list_open_actionable(workspace_id, now=now))
    assert len(open_items) == 1
    item = open_items[0].item
    assert item.kind == "dialog"
    assert item.lifecycle == "open"
    assert item.loop_state == "active_dialog"
    assert open_items[0].subject_name == "Иван"
    assert item.ref_type == "artifact"
    assert item.ref_id is not None

    artifacts = _run(artifact_repo.list_artifacts(workspace_id))
    assert len(artifacts) == 1
    assert artifacts[0].artifact_type == "client_message"
    assert artifacts[0].id == item.ref_id

    text, kwargs = message.answers[-1]
    assert "Черновик ответа Ивану" in text
    assert kwargs["reply_markup"] is not None


# ── 5. «Отправил» -> waiting_reply + due_at ~+2 дня ─────────────────────────


def test_confirm_sent_moves_to_waiting_reply_with_two_day_due_at(tmp_path: Path) -> None:
    _, work_repo, _, workspace_id = _stack(tmp_path)
    item = _run(work_repo.create_work_item(
        workspace_id, kind="dialog", loop_state="active_dialog",
        next_step="Отправить подготовленный ответ: Иван",
    ))

    before = datetime.now(timezone.utc)
    callback = _run(_current_reply_callback(
        work_repo, workspace_id, item.id, REPLY_CONFIRM_SENT_PREFIX,
    ))
    _run(on_reply_confirm_sent(callback, _ctx(workspace_id), work_repo))
    after = datetime.now(timezone.utc)

    updated = _run(work_repo.get_work_item(workspace_id, item.id))
    assert updated.lifecycle == "open"
    assert updated.loop_state == "waiting_reply"
    assert updated.due_at is not None
    due_at = datetime.fromisoformat(updated.due_at)
    assert before + timedelta(days=2) <= due_at <= after + timedelta(days=2)


# ── 6. «Позже» оставляет active_dialog без изменений ────────────────────────


def test_confirm_later_leaves_active_dialog_untouched(tmp_path: Path) -> None:
    _, work_repo, _, workspace_id = _stack(tmp_path)
    item = _run(work_repo.create_work_item(
        workspace_id, kind="dialog", loop_state="active_dialog",
        next_step="Отправить подготовленный ответ: Иван",
    ))

    callback = _run(_current_reply_callback(
        work_repo, workspace_id, item.id, REPLY_CONFIRM_LATER_PREFIX,
    ))
    _run(on_reply_confirm_later(callback, _ctx(workspace_id), work_repo))

    unchanged = _run(work_repo.get_work_item(workspace_id, item.id))
    assert unchanged.lifecycle == item.lifecycle
    assert unchanged.loop_state == item.loop_state == "active_dialog"
    assert unchanged.due_at == item.due_at
    assert unchanged.updated_at == item.updated_at

    now = _iso(timedelta(0))
    open_items = _run(work_repo.list_open_actionable(workspace_id, now=now))
    assert len(open_items) == 1


# ── 7. «Не актуально» -> dismissed (свой revision-aware хендлер) ───────────


def test_confirm_dismiss_resolves_item(tmp_path: Path) -> None:
    """Reply-confirm «🚫 Не актуально» — отдельный on_reply_confirm_dismiss,
    не daily_action:dismiss/on_daily_action_dismiss (у того нет revision)."""
    _, work_repo, _, workspace_id = _stack(tmp_path)
    item = _run(work_repo.create_work_item(
        workspace_id, kind="dialog", loop_state="active_dialog",
        next_step="Отправить подготовленный ответ: Иван",
    ))

    callback = _run(_current_reply_callback(
        work_repo, workspace_id, item.id, REPLY_CONFIRM_DISMISS_PREFIX,
    ))
    _run(on_reply_confirm_dismiss(callback, _ctx(workspace_id), work_repo))

    resolved = _run(work_repo.get_work_item(workspace_id, item.id))
    assert resolved.lifecycle == "dismissed"
    assert resolved.resolved_at is not None


def test_confirm_dismiss_on_missing_item_completes_without_error(tmp_path: Path) -> None:
    """«work_item ещё по какой-то причине не был создан» — корректный UX без
    исключения, а не 500-ошибка."""
    _, work_repo, _, workspace_id = _stack(tmp_path)
    callback = _Callback(f"{REPLY_CONFIRM_DISMISS_PREFIX}999:anyrevision")
    _run(on_reply_confirm_dismiss(callback, _ctx(workspace_id), work_repo))
    assert callback.answers[0][1] == {"show_alert": True}


def test_daily_action_dismiss_still_used_only_by_daily_actions_screen(tmp_path: Path) -> None:
    """Убеждаемся, что старый общий daily_action:dismiss (без revision) для
    reply-confirm карточек больше не задействован: он остаётся рабочим сам по
    себе (карточки «Что делать сегодня» его по-прежнему используют), но
    reply_confirm_keyboard его больше не генерирует."""
    _, work_repo, _, workspace_id = _stack(tmp_path)
    item = _run(work_repo.create_work_item(
        workspace_id, kind="dialog", loop_state="active_dialog",
        next_step="Продолжить разговор",
    ))
    callback = _Callback(f"{DAILY_ACTION_DISMISS_PREFIX}{item.id}")
    _run(on_daily_action_dismiss(callback, _ctx(workspace_id), work_repo))
    resolved = _run(work_repo.get_work_item(workspace_id, item.id))
    assert resolved.lifecycle == "dismissed"


# ── 8. Полный сценарий Ивана ────────────────────────────────────────────────


def test_ivan_full_acceptance_scenario(tmp_path: Path) -> None:
    partners, work_repo, artifact_repo, workspace_id = _stack(tmp_path)

    # День 1: «Ответить клиенту» -> Иван -> сообщение -> черновик -> Отправил.
    state = _State()
    _run(_start_reply(state))
    _run(_submit_subject(state, work_repo, workspace_id, "Иван"))
    provider = FakeLLMProvider(draft=ContentDraft("Черновик 1", ()))
    message = _run(_submit_client_message(
        state, provider, workspace_id, partners, work_repo, artifact_repo,
        "Здравствуйте! Расскажите про тур в Турцию.",
    ))
    provider.generate_draft.assert_called_once()
    work_item_id = _keyboard_work_item_id(message)

    sent_callback = _run(_current_reply_callback(
        work_repo, workspace_id, work_item_id, REPLY_CONFIRM_SENT_PREFIX,
    ))
    _run(on_reply_confirm_sent(sent_callback, _ctx(workspace_id), work_repo))
    after_sent = _run(work_repo.get_work_item(workspace_id, work_item_id))
    assert after_sent.loop_state == "waiting_reply"
    assert after_sent.due_at is not None

    # День 2: Иван ответил раньше due_at — виден только в waiting.
    now = _iso(timedelta(0))
    assert _run(work_repo.list_open_actionable(workspace_id, now=now)) == []
    waiting = _run(work_repo.list_waiting_not_due(workspace_id, now=now))
    assert len(waiting) == 1 and waiting[0].item.id == work_item_id

    replied_callback = _Callback(f"{DAILY_ACTION_REPLIED_PREFIX}{work_item_id}")
    _run(on_daily_action_replied(replied_callback, _ctx(workspace_id), work_repo))
    after_replied = _run(work_repo.get_work_item(workspace_id, work_item_id))
    assert after_replied.loop_state == "active_dialog"
    assert after_replied.due_at is None

    # «Продолжить разговор»: имя не спрашивается повторно.
    prompt_callback = _Callback(f"daily_action:prompt:{work_item_id}")
    state2 = _State()
    _run(on_daily_action_prompt(prompt_callback, state2, _ctx(workspace_id), work_repo))
    assert state2.state == AwaitTask.waiting
    assert state2.data["daily_action_work_item_id"] == work_item_id
    assert state2.data["daily_action_subject_name"] == "Иван"

    provider2 = FakeLLMProvider(draft=ContentDraft("Черновик 2", ()))
    message2 = _run(_submit_client_message(
        state2, provider2, workspace_id, partners, work_repo, artifact_repo,
        "Иван: да, интересно, что дальше?",
    ))
    provider2.generate_draft.assert_called_once()
    reused_work_item_id = _keyboard_work_item_id(message2)
    assert reused_work_item_id == work_item_id

    sent_callback2 = _run(_current_reply_callback(
        work_repo, workspace_id, work_item_id, REPLY_CONFIRM_SENT_PREFIX,
    ))
    _run(on_reply_confirm_sent(sent_callback2, _ctx(workspace_id), work_repo))
    final_item = _run(work_repo.get_work_item(workspace_id, work_item_id))
    assert final_item.loop_state == "waiting_reply"

    # Никаких дублей: ровно один субъект «Иван», ровно один work_item.
    same_subject = _run(work_repo.get_or_create_subject(workspace_id, "Иван"))
    assert same_subject.id == final_item.subject_id
    all_related = [
        *_run(work_repo.list_open_actionable(workspace_id, now=_iso(timedelta(days=3)))),
        *_run(work_repo.list_waiting_not_due(workspace_id, now=_iso(timedelta(0)))),
    ]
    ivan_item_ids = {view.item.id for view in all_related if view.subject_name == "Иван"}
    assert ivan_item_ids == {work_item_id}


# ── 9. Ольга: future due -> waiting, не action ──────────────────────────────


def test_olga_stays_in_waiting_until_due_at(tmp_path: Path) -> None:
    partners, work_repo, artifact_repo, workspace_id = _stack(tmp_path)
    state = _State()
    _run(_start_reply(state))
    _run(_submit_subject(state, work_repo, workspace_id, "Ольга"))
    provider = FakeLLMProvider(draft=ContentDraft("Черновик Ольге", ()))
    message = _run(_submit_client_message(
        state, provider, workspace_id, partners, work_repo, artifact_repo,
        "Здравствуйте, уточните условия, пожалуйста.",
    ))
    work_item_id = _keyboard_work_item_id(message)

    sent_callback = _run(_current_reply_callback(
        work_repo, workspace_id, work_item_id, REPLY_CONFIRM_SENT_PREFIX,
    ))
    _run(on_reply_confirm_sent(sent_callback, _ctx(workspace_id), work_repo))

    now = _iso(timedelta(0))
    assert _run(work_repo.list_open_actionable(workspace_id, now=now)) == []
    waiting = _run(work_repo.list_waiting_not_due(workspace_id, now=now))
    assert len(waiting) == 1
    assert waiting[0].subject_name == "Ольга"


# ── 10. due follow-up -> bridge -> Отправил -> тот же item, новый due_at ───


def test_due_follow_up_bridge_sent_updates_same_work_item(tmp_path: Path) -> None:
    _, work_repo, _, workspace_id = _stack(tmp_path)
    olga = _run(work_repo.get_or_create_subject(workspace_id, "Ольга"))
    item = _run(work_repo.create_work_item(
        workspace_id, kind="dialog", subject_id=olga.id, loop_state="waiting_reply",
        next_step="Ждём ответ: Ольга", due_at=_iso(-timedelta(minutes=1)),
    ))
    original_due_at = item.due_at

    # «💬 Написать снова» — bridge (subject уже известен, имя не спрашивается).
    prompt_callback = _Callback(f"daily_action:prompt:{item.id}")
    state = _State()
    _run(on_daily_action_prompt(prompt_callback, state, _ctx(workspace_id), work_repo))
    assert state.state == AwaitTask.waiting
    assert state.data["daily_action_work_item_id"] == item.id

    partner_repo = PartnerRepository(work_repo.db_path)
    _run(partner_repo.init())
    artifact_repo = ArtifactRepository(work_repo.db_path)
    _run(artifact_repo.init())
    provider = FakeLLMProvider(draft=ContentDraft("Ещё раз напоминаю про тур", ()))
    message = _run(_submit_client_message(
        state, provider, workspace_id, partner_repo, work_repo, artifact_repo,
        "Хочу уточнить, актуально ли предложение.",
    ))
    reused_id = _keyboard_work_item_id(message)
    assert reused_id == item.id

    # Черновик реально подготовлен -> item уже active_dialog (теперь ход
    # пользователя), due_at снят, ещё до нажатия «Отправил».
    prepared = _run(work_repo.get_work_item(workspace_id, item.id))
    assert prepared.loop_state == "active_dialog"
    assert prepared.due_at is None

    sent_callback = _run(_current_reply_callback(
        work_repo, workspace_id, item.id, REPLY_CONFIRM_SENT_PREFIX,
    ))
    _run(on_reply_confirm_sent(sent_callback, _ctx(workspace_id), work_repo))

    updated = _run(work_repo.get_work_item(workspace_id, item.id))
    assert updated.loop_state == "waiting_reply"
    assert updated.due_at != original_due_at
    assert datetime.fromisoformat(updated.due_at) > datetime.now(timezone.utc)


# ── 11. повторный «Отправил» не создаёт второй work_item ───────────────────


def test_repeated_confirm_sent_does_not_duplicate_work_item(tmp_path: Path) -> None:
    """Три тапа с ОДНИМ и тем же (изначальным) callback_data: первый —
    реальный клик по актуальной клавиатуре, успевает. Второй и третий —
    это уже повтор того же, теперь устаревшего revision (первый клик успел
    сдвинуть updated_at) — верно отклоняются как stale, а не тихо повторяют
    мутацию. В любом случае — ни один тап не создаёт второй work_item."""
    _, work_repo, _, workspace_id = _stack(tmp_path)
    ivan = _run(work_repo.get_or_create_subject(workspace_id, "Иван"))
    item = _run(work_repo.create_work_item(
        workspace_id, kind="dialog", subject_id=ivan.id, loop_state="active_dialog",
        next_step="Отправить подготовленный ответ: Иван",
    ))
    original_revision = work_item_revision(item)

    results = []
    for _ in range(3):
        callback = _Callback(f"{REPLY_CONFIRM_SENT_PREFIX}{item.id}:{original_revision}")
        _run(on_reply_confirm_sent(callback, _ctx(workspace_id), work_repo))
        results.append(callback.answers[0][0])

    assert results[0] == "Отмечено: ждём ответ."
    assert results[1] == results[2] == "Это действие уже устарело. Откройте «Что делать сегодня»."

    now = _iso(timedelta(0))
    waiting = _run(work_repo.list_waiting_not_due(workspace_id, now=now))
    matching = [view for view in waiting if view.subject_name == "Иван"]
    assert len(matching) == 1
    assert matching[0].item.id == item.id


# ── 12. tenant isolation ────────────────────────────────────────────────────


def test_reply_work_memory_is_tenant_isolated(tmp_path: Path) -> None:
    db_path = tmp_path / "workspace.sqlite3"
    partners = PartnerRepository(db_path)
    _run(partners.init())
    context_a = empty_business_context()
    context_a["specializations"] = ["cruises"]
    workspace_a = _run(partners.provision_partner(
        111222333, "Agency A", "agency-a", business_name="Agency A",
        business_type="independent_agent", short_description="A", context=context_a,
    )).workspace.id
    context_b = empty_business_context()
    context_b["specializations"] = ["ski"]
    workspace_b = _run(partners.provision_partner(
        444555666, "Agency B", "agency-b", business_name="Agency B",
        business_type="independent_agent", short_description="B", context=context_b,
    )).workspace.id

    work_repo = WorkRepository(db_path)
    _run(work_repo.init())
    artifact_repo = ArtifactRepository(db_path)
    _run(artifact_repo.init())

    state_a = _State()
    _run(_start_reply(state_a))
    _run(_submit_subject(state_a, work_repo, workspace_a, "Иван"))
    provider_a = FakeLLMProvider(draft=ContentDraft("Черновик A", ()))
    _run(_submit_client_message(
        state_a, provider_a, workspace_a, partners, work_repo, artifact_repo,
        "Сообщение в workspace A",
    ))

    now = _iso(timedelta(0))
    items_a = _run(work_repo.list_open_actionable(workspace_a, now=now))
    items_b = _run(work_repo.list_open_actionable(workspace_b, now=now))
    assert len(items_a) == 1
    assert items_b == []

    subject_a = _run(work_repo.get_or_create_subject(workspace_a, "Иван"))
    subject_b = _run(work_repo.get_or_create_subject(workspace_b, "Иван"))
    assert subject_a.id != subject_b.id
    assert _run(work_repo.get_subject(workspace_b, subject_a.id)) is None


# ── 13. существующий AI ровно один вызов на цикл, без второго/дублирующего ─


def test_exactly_one_llm_call_per_reply_cycle(tmp_path: Path) -> None:
    partners, work_repo, artifact_repo, workspace_id = _stack(tmp_path)
    state = _State()
    _run(_start_reply(state))
    _run(_submit_subject(state, work_repo, workspace_id, "Иван"))
    provider = FakeLLMProvider(draft=ContentDraft("Единственный черновик", ()))

    _run(_submit_client_message(
        state, provider, workspace_id, partners, work_repo, artifact_repo,
        "Вопрос клиента",
    ))

    assert provider.generate_draft.call_count == 1
    # ArtifactRepository пишет уже готовый текст из того же draft, не второй
    # генератор: содержимое артефакта совпадает с draft.text дословно.
    artifacts = _run(artifact_repo.list_artifacts(workspace_id))
    assert len(artifacts) == 1
    version = _run(artifact_repo.get_current_artifact_version(workspace_id, artifacts[0].id))
    assert version.content == "Единственный черновик"


# ── 14. устаревшие reply_confirm-клавиатуры не мутируют более новый цикл ───


def test_old_sent_callback_after_reply_received_does_not_mutate_item(tmp_path: Path) -> None:
    """Клавиатура «draft A» показана, пока item был waiting_reply. Пока
    пользователь её не трогал, «Иван ответил» (daily_actions) переводит item
    в active_dialog — мутация, не связанная с reply-confirm кнопками, но
    сдвигающая updated_at. Старая кнопка «Отправил» из draft A после этого
    не должна снова переводить item в waiting_reply."""
    _, work_repo, _, workspace_id = _stack(tmp_path)
    ivan = _run(work_repo.get_or_create_subject(workspace_id, "Иван"))
    item = _run(work_repo.create_work_item(
        workspace_id, kind="dialog", subject_id=ivan.id, loop_state="waiting_reply",
        next_step="Ждём ответ: Иван", due_at=_iso(timedelta(days=2)),
    ))
    stale_sent = _run(_current_reply_callback(
        work_repo, workspace_id, item.id, REPLY_CONFIRM_SENT_PREFIX,
    ))

    replied_callback = _Callback(f"{DAILY_ACTION_REPLIED_PREFIX}{item.id}")
    _run(on_daily_action_replied(replied_callback, _ctx(workspace_id), work_repo))
    after_replied = _run(work_repo.get_work_item(workspace_id, item.id))
    assert after_replied.loop_state == "active_dialog"

    _run(on_reply_confirm_sent(stale_sent, _ctx(workspace_id), work_repo))

    unchanged = _run(work_repo.get_work_item(workspace_id, item.id))
    assert unchanged.loop_state == "active_dialog"
    assert unchanged.due_at is None
    assert unchanged.updated_at == after_replied.updated_at
    assert stale_sent.answers[-1] == (
        "Это действие уже устарело. Откройте «Что делать сегодня».", {"show_alert": True},
    )


def test_old_sent_callback_after_new_draft_prepared_does_not_mutate_item(tmp_path: Path) -> None:
    """Ровно сценарий из отчёта: draft A показан (клавиатура захвачена, но
    не нажата) -> «Продолжить разговор» готовит draft B для того же item ->
    старая кнопка draft A не должна сработать, а актуальная (draft B) —
    должна."""
    partners, work_repo, artifact_repo, workspace_id = _stack(tmp_path)
    ivan = _run(work_repo.get_or_create_subject(workspace_id, "Иван"))
    item = _run(work_repo.create_work_item(
        workspace_id, kind="dialog", subject_id=ivan.id, loop_state="active_dialog",
        next_step="Отправить подготовленный ответ: Иван",
    ))
    stale_sent = _run(_current_reply_callback(
        work_repo, workspace_id, item.id, REPLY_CONFIRM_SENT_PREFIX,
    ))  # клавиатура draft A

    prompt_callback = _Callback(f"daily_action:prompt:{item.id}")
    state = _State()
    _run(on_daily_action_prompt(prompt_callback, state, _ctx(workspace_id), work_repo))
    provider = FakeLLMProvider(draft=ContentDraft("Черновик B", ()))
    _run(_submit_client_message(
        state, provider, workspace_id, partners, work_repo, artifact_repo,
        "Ещё один вопрос от Ивана",
    ))
    after_draft_b = _run(work_repo.get_work_item(workspace_id, item.id))
    assert after_draft_b.updated_at != item.updated_at

    _run(on_reply_confirm_sent(stale_sent, _ctx(workspace_id), work_repo))
    still_active = _run(work_repo.get_work_item(workspace_id, item.id))
    assert still_active.loop_state == "active_dialog"
    assert still_active.updated_at == after_draft_b.updated_at
    assert stale_sent.answers[-1][0] == "Это действие уже устарело. Откройте «Что делать сегодня»."

    current_sent = _run(_current_reply_callback(
        work_repo, workspace_id, item.id, REPLY_CONFIRM_SENT_PREFIX,
    ))
    _run(on_reply_confirm_sent(current_sent, _ctx(workspace_id), work_repo))
    final = _run(work_repo.get_work_item(workspace_id, item.id))
    assert final.loop_state == "waiting_reply"


def test_old_dismiss_callback_does_not_close_newer_cycle(tmp_path: Path) -> None:
    """Ключевой кейс из отчёта: draft A → «Отправил» подтверждён (item уже
    waiting_reply) → забытая кнопка «Не актуально» из draft A не должна
    закрыть только что подтверждённый цикл."""
    _, work_repo, _, workspace_id = _stack(tmp_path)
    ivan = _run(work_repo.get_or_create_subject(workspace_id, "Иван"))
    item = _run(work_repo.create_work_item(
        workspace_id, kind="dialog", subject_id=ivan.id, loop_state="active_dialog",
        next_step="Отправить подготовленный ответ: Иван",
    ))
    stale_dismiss = _run(_current_reply_callback(
        work_repo, workspace_id, item.id, REPLY_CONFIRM_DISMISS_PREFIX,
    ))

    sent_callback = _run(_current_reply_callback(
        work_repo, workspace_id, item.id, REPLY_CONFIRM_SENT_PREFIX,
    ))
    _run(on_reply_confirm_sent(sent_callback, _ctx(workspace_id), work_repo))
    after_sent = _run(work_repo.get_work_item(workspace_id, item.id))
    assert after_sent.loop_state == "waiting_reply"

    _run(on_reply_confirm_dismiss(stale_dismiss, _ctx(workspace_id), work_repo))

    still_open = _run(work_repo.get_work_item(workspace_id, item.id))
    assert still_open.lifecycle == "open"
    assert still_open.loop_state == "waiting_reply"
    assert still_open.due_at == after_sent.due_at
    assert stale_dismiss.answers[-1][0] == "Это действие уже устарело. Откройте «Что делать сегодня»."


def test_current_reply_confirm_callbacks_still_work(tmp_path: Path) -> None:
    """Актуальная (не устаревшая) клавиатура продолжает работать как раньше —
    revision-guard не ломает обычный путь."""
    _, work_repo, _, workspace_id = _stack(tmp_path)
    sent_item = _run(work_repo.create_work_item(
        workspace_id, kind="dialog", loop_state="active_dialog",
        next_step="Отправить подготовленный ответ: Иван",
    ))
    sent_callback = _run(_current_reply_callback(
        work_repo, workspace_id, sent_item.id, REPLY_CONFIRM_SENT_PREFIX,
    ))
    _run(on_reply_confirm_sent(sent_callback, _ctx(workspace_id), work_repo))
    assert _run(work_repo.get_work_item(
        workspace_id, sent_item.id,
    )).loop_state == "waiting_reply"

    dismiss_item = _run(work_repo.create_work_item(
        workspace_id, kind="dialog", loop_state="active_dialog",
        next_step="Отправить подготовленный ответ: Ольга",
    ))
    dismiss_callback = _run(_current_reply_callback(
        work_repo, workspace_id, dismiss_item.id, REPLY_CONFIRM_DISMISS_PREFIX,
    ))
    _run(on_reply_confirm_dismiss(dismiss_callback, _ctx(workspace_id), work_repo))
    assert _run(work_repo.get_work_item(
        workspace_id, dismiss_item.id,
    )).lifecycle == "dismissed"


# ── 15. UX polish: сообщение вместо имени сразу обрабатывается ─────────────


def test_message_instead_of_name_is_processed_immediately_without_repeat_prompt(
    tmp_path: Path,
) -> None:
    """Живой тест Stage 3B1: пользователь после «Ответить клиенту» сразу
    вставляет вопрос клиента вместо имени — не должен получить повторный
    вопрос «Опишите вопрос клиента» (см. _looks_like_client_message в
    app/handlers/tasks.py)."""
    partners, work_repo, artifact_repo, workspace_id = _stack(tmp_path)
    state = _State()
    _run(_start_reply(state))

    provider = FakeLLMProvider(draft=ContentDraft("Ответ клиенту", ()))
    message = _Message(
        "А правда, что через Travel Advantage всегда дешевле бронировать отели?"
    )
    _run(on_reply_subject_received(
        message, state, _journal(), provider, work_repo, _ctx(workspace_id),
        partners, artifact_repo,
    ))

    provider.generate_draft.assert_called_once()
    assert state.state is None  # состояние сброшено, повторный вопрос не задан
    texts = [text for text, _ in message.answers]
    assert not any("Опишите вопрос клиента" in (t or "") for t in texts)
    assert any("Ответ клиенту" in (t or "") for t in texts)


def test_message_instead_of_name_makes_name_optional_and_does_not_break_work_memory(
    tmp_path: Path,
) -> None:
    """Имя необязательно: без subject_id ReplyWorkSyncService корректно не
    создаёт work_item (см. app/services/reply_sync.py), но черновик всё
    равно доставляется — Reply flow не падает и не требует имени."""
    partners, work_repo, artifact_repo, workspace_id = _stack(tmp_path)
    state = _State()
    _run(_start_reply(state))

    provider = FakeLLMProvider(draft=ContentDraft("Ответ без имени", ()))
    message = _Message(
        "Подскажите, пожалуйста, какие есть варианты тарифов на туры в Италию?"
    )
    _run(on_reply_subject_received(
        message, state, _journal(), provider, work_repo, _ctx(workspace_id),
        partners, artifact_repo,
    ))

    provider.generate_draft.assert_called_once()
    now = _iso(timedelta(0))
    assert _run(work_repo.list_open_actionable(workspace_id, now=now)) == []
    text, kwargs = message.answers[-1]
    assert "Ответ без имени" in text
    assert kwargs.get("reply_markup") is None


def test_short_name_still_treated_as_label_not_message(tmp_path: Path) -> None:
    """Контрольный случай: короткое «Иван» по-прежнему воспринимается как
    метка клиента, а не как сообщение — второй шаг («Опишите вопрос
    клиента») остаётся, имя сохраняется в WorkSubject."""
    _, work_repo, _, workspace_id = _stack(tmp_path)
    state = _State()
    _run(_start_reply(state))

    subject_message = _run(_submit_subject(state, work_repo, workspace_id, "Иван"))
    assert state.state == AwaitTask.waiting
    assert "Опишите вопрос клиента" in subject_message.answers[0][0]


def test_reply_confirm_revision_guard_is_tenant_scoped(tmp_path: Path) -> None:
    """Валидный id+revision, но подставленный под чужой workspace_context, —
    get_work_item сам workspace-scoped, поэтому это «не найдено», а не
    случайно совпавшая revision в чужом workspace."""
    db_path = tmp_path / "workspace.sqlite3"
    partners = PartnerRepository(db_path)
    _run(partners.init())
    context_a = empty_business_context()
    context_a["specializations"] = ["cruises"]
    workspace_a = _run(partners.provision_partner(
        111222333, "Agency A", "agency-a", business_name="Agency A",
        business_type="independent_agent", short_description="A", context=context_a,
    )).workspace.id
    context_b = empty_business_context()
    context_b["specializations"] = ["ski"]
    workspace_b = _run(partners.provision_partner(
        444555666, "Agency B", "agency-b", business_name="Agency B",
        business_type="independent_agent", short_description="B", context=context_b,
    )).workspace.id

    work_repo = WorkRepository(db_path)
    _run(work_repo.init())
    item = _run(work_repo.create_work_item(
        workspace_a, kind="dialog", loop_state="active_dialog",
        next_step="Отправить подготовленный ответ: Иван",
    ))
    valid_for_a = _run(_current_reply_callback(
        work_repo, workspace_a, item.id, REPLY_CONFIRM_SENT_PREFIX,
    ))
    forged_for_b = _Callback(valid_for_a.data)

    _run(on_reply_confirm_sent(forged_for_b, _ctx(workspace_b), work_repo))

    assert forged_for_b.answers[0] == (_WORK_ITEM_NOT_FOUND, {"show_alert": True})
    untouched = _run(work_repo.get_work_item(workspace_a, item.id))
    assert untouched.loop_state == "active_dialog"
