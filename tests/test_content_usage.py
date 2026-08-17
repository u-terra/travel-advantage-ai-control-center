"""Content → work memory: «✅ Опубликовал» / «📌 Оставить на потом».

Покрывает: ArtifactRepository.mark_used_if_not_already,
WorkRepository.find_work_item_by_ref, ContentUsageService.mark_used,
Telegram-обработчики app/handlers/content_usage.py и минимальную интеграцию
с DailyActionsService (used-артефакты вне draft-кандидатов, done content вне
open actions, recent_resolved_content доступен как история).
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

from app.domain.partners import WorkspaceContext
from app.handlers.content_usage import on_artifact_keep_later, on_artifact_mark_used
from app.keyboards import (
    ARTIFACT_CHECK_PREFIX,
    ARTIFACT_KEEP_LATER_PREFIX,
    ARTIFACT_MARK_USED_PREFIX,
    material_result_keyboard,
)
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.partner_repository import PartnerRepository, empty_business_context
from app.repositories.work_repository import WorkRepository
from app.services.content_usage import ContentUsageResult, ContentUsageService
from app.services.daily_actions import DailyActionsService


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _Message:
    def __init__(self) -> None:
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


def _ctx(workspace_id: int, role: str = "owner") -> WorkspaceContext:
    return WorkspaceContext(100, workspace_id, role, "active")


def _stack(tmp_path: Path) -> tuple[PartnerRepository, ArtifactRepository, WorkRepository]:
    db_path = tmp_path / "workspace.sqlite3"
    partners = PartnerRepository(db_path)
    _run(partners.init())
    artifacts = ArtifactRepository(db_path)
    _run(artifacts.init())
    work = WorkRepository(db_path)
    _run(work.init())
    return partners, artifacts, work


def _provision(partners: PartnerRepository, telegram_user_id: int, slug: str) -> int:
    context = empty_business_context()
    context["specializations"] = ["круизы"]
    provisioned = _run(partners.provision_partner(
        telegram_user_id, "Тревел Клуб", slug,
        business_name="Тревел Клуб", business_type="independent_agent",
        short_description="Организуем туры.", context=context,
        ta_affiliated=False,
    ))
    return provisioned.workspace.id


# ── 1. кнопка «✅ Опубликовал» под сохранённым Artifact ─────────────────────


def test_material_result_keyboard_has_mark_used_button() -> None:
    keyboard = material_result_keyboard(42)
    callback_datas = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ]
    assert f"{ARTIFACT_MARK_USED_PREFIX}42" in callback_datas
    assert f"{ARTIFACT_KEEP_LATER_PREFIX}42" in callback_datas
    assert f"{ARTIFACT_CHECK_PREFIX}42" in callback_datas  # «Доработать»: существующий flow


# ── ArtifactRepository.mark_used_if_not_already ─────────────────────────────


def test_mark_used_if_not_already_transitions_once(tmp_path: Path) -> None:
    partners, artifacts, work = _stack(tmp_path)
    workspace_id = _provision(partners, 111, "ws-cas")
    artifact, _ = _run(artifacts.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост", content="Текст",
    ))

    updated, transitioned = _run(artifacts.mark_used_if_not_already(workspace_id, artifact.id))
    assert transitioned is True
    assert updated.status == "used"

    updated_again, transitioned_again = _run(
        artifacts.mark_used_if_not_already(workspace_id, artifact.id)
    )
    assert transitioned_again is False
    assert updated_again.status == "used"


def test_mark_used_if_not_already_does_not_transition_archived(tmp_path: Path) -> None:
    partners, artifacts, work = _stack(tmp_path)
    workspace_id = _provision(partners, 112, "ws-archived")
    artifact, _ = _run(artifacts.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост", content="Текст",
    ))
    _run(artifacts.update_artifact_status(workspace_id, artifact.id, "archived"))

    updated, transitioned = _run(artifacts.mark_used_if_not_already(workspace_id, artifact.id))
    assert transitioned is False
    assert updated.status == "archived"


# ── WorkRepository.find_work_item_by_ref ────────────────────────────────────


def test_find_work_item_by_ref_finds_matching_done_content_item(tmp_path: Path) -> None:
    partners, artifacts, work = _stack(tmp_path)
    workspace_id = _provision(partners, 113, "ws-ref")
    artifact, _ = _run(artifacts.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост", content="Текст",
    ))
    created = _run(work.create_work_item(
        workspace_id, kind="content", lifecycle="done",
        ref_type="artifact", ref_id=artifact.id,
    ))

    found = _run(work.find_work_item_by_ref(
        workspace_id, ref_type="artifact", ref_id=artifact.id,
        kind="content", lifecycle="done",
    ))
    assert found is not None
    assert found.id == created.id


def test_find_work_item_by_ref_returns_none_when_absent(tmp_path: Path) -> None:
    partners, artifacts, work = _stack(tmp_path)
    workspace_id = _provision(partners, 114, "ws-ref-absent")
    artifact, _ = _run(artifacts.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост", content="Текст",
    ))
    found = _run(work.find_work_item_by_ref(
        workspace_id, ref_type="artifact", ref_id=artifact.id,
        kind="content", lifecycle="done",
    ))
    assert found is None


# ── 2-4. ContentUsageService.mark_used: transition, work_item, idempotency ──


def test_mark_used_transitions_artifact_status(tmp_path: Path) -> None:
    partners, artifacts, work = _stack(tmp_path)
    workspace_id = _provision(partners, 115, "ws-service")
    artifact, _ = _run(artifacts.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост", content="Текст",
    ))

    result = _run(ContentUsageService(artifacts, work).mark_used(workspace_id, artifact.id))
    assert result.outcome == "marked_used"
    assert result.artifact.status == "used"


def test_mark_used_creates_exactly_one_done_content_work_item(tmp_path: Path) -> None:
    partners, artifacts, work = _stack(tmp_path)
    workspace_id = _provision(partners, 116, "ws-single-item")
    artifact, _ = _run(artifacts.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост", content="Текст",
    ))

    result = _run(ContentUsageService(artifacts, work).mark_used(workspace_id, artifact.id))
    assert result.work_item is not None
    assert result.work_item.kind == "content"
    assert result.work_item.lifecycle == "done"
    assert result.work_item.ref_type == "artifact"
    assert result.work_item.ref_id == artifact.id
    assert result.work_item.resolved_at is not None


def test_mark_used_twice_does_not_create_second_work_item(tmp_path: Path) -> None:
    partners, artifacts, work = _stack(tmp_path)
    workspace_id = _provision(partners, 117, "ws-idempotent")
    artifact, _ = _run(artifacts.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост", content="Текст",
    ))

    first = _run(ContentUsageService(artifacts, work).mark_used(workspace_id, artifact.id))
    second = _run(ContentUsageService(artifacts, work).mark_used(workspace_id, artifact.id))

    assert first.outcome == "marked_used"
    assert second.outcome == "already_used"
    assert second.work_item.id == first.work_item.id

    all_items = _run(work.list_recent_resolved_content(
        workspace_id, since="2000-01-01T00:00:00+00:00", limit=50,
    ))
    matching = [item for item in all_items if item.ref_id == artifact.id]
    assert len(matching) == 1


# ── 8-9. fail-closed: foreign workspace / archived / not-found ─────────────


def test_mark_used_foreign_workspace_artifact_is_fail_closed(tmp_path: Path) -> None:
    partners, artifacts, work = _stack(tmp_path)
    owner_ws = _provision(partners, 118, "ws-owner")
    other_ws = _provision(partners, 119, "ws-other")
    artifact, _ = _run(artifacts.create_artifact_with_initial_version(
        owner_ws, artifact_type="post", title="Пост", content="Текст",
    ))

    result = _run(ContentUsageService(artifacts, work).mark_used(other_ws, artifact.id))
    assert result.outcome == "not_found"

    unchanged = _run(artifacts.get_artifact(owner_ws, artifact.id))
    assert unchanged.status == "draft"


def test_mark_used_archived_artifact_is_fail_closed(tmp_path: Path) -> None:
    partners, artifacts, work = _stack(tmp_path)
    workspace_id = _provision(partners, 120, "ws-archived-service")
    artifact, _ = _run(artifacts.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост", content="Текст",
    ))
    _run(artifacts.update_artifact_status(workspace_id, artifact.id, "archived"))

    result = _run(ContentUsageService(artifacts, work).mark_used(workspace_id, artifact.id))
    assert result.outcome == "archived"
    assert result.work_item is None
    unchanged = _run(artifacts.get_artifact(workspace_id, artifact.id))
    assert unchanged.status == "archived"


def test_mark_used_not_found_artifact_is_fail_closed(tmp_path: Path) -> None:
    partners, artifacts, work = _stack(tmp_path)
    workspace_id = _provision(partners, 121, "ws-not-found")
    result = _run(ContentUsageService(artifacts, work).mark_used(workspace_id, 999999))
    assert result.outcome == "not_found"
    assert result.artifact is None
    assert result.work_item is None


# ── Handler-level: fail-closed callbacks, idempotent repeat tap ────────────


def test_handler_mark_used_success_sends_confirmation_and_clears_keyboard(
    tmp_path: Path,
) -> None:
    partners, artifacts, work = _stack(tmp_path)
    workspace_id = _provision(partners, 122, "ws-handler-ok")
    artifact, _ = _run(artifacts.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост", content="Текст",
    ))

    callback = _Callback(f"{ARTIFACT_MARK_USED_PREFIX}{artifact.id}")
    _run(on_artifact_mark_used(callback, _ctx(workspace_id), artifacts, work))

    assert callback.answers[-1][0] == "✅ Отмечено как опубликованное."
    assert callback.message.reply_markup_edits == [None]
    updated = _run(artifacts.get_artifact(workspace_id, artifact.id))
    assert updated.status == "used"


def test_handler_mark_used_repeat_tap_is_idempotent(tmp_path: Path) -> None:
    partners, artifacts, work = _stack(tmp_path)
    workspace_id = _provision(partners, 123, "ws-handler-repeat")
    artifact, _ = _run(artifacts.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост", content="Текст",
    ))

    first = _Callback(f"{ARTIFACT_MARK_USED_PREFIX}{artifact.id}")
    _run(on_artifact_mark_used(first, _ctx(workspace_id), artifacts, work))
    second = _Callback(f"{ARTIFACT_MARK_USED_PREFIX}{artifact.id}")
    _run(on_artifact_mark_used(second, _ctx(workspace_id), artifacts, work))

    assert second.answers[-1][0] == "Уже отмечено как опубликованное."
    items = _run(work.list_recent_resolved_content(
        workspace_id, since="2000-01-01T00:00:00+00:00", limit=50,
    ))
    assert len([item for item in items if item.ref_id == artifact.id]) == 1


def test_handler_mark_used_foreign_workspace_is_fail_closed(tmp_path: Path) -> None:
    partners, artifacts, work = _stack(tmp_path)
    owner_ws = _provision(partners, 124, "ws-handler-owner")
    other_ws = _provision(partners, 125, "ws-handler-other")
    artifact, _ = _run(artifacts.create_artifact_with_initial_version(
        owner_ws, artifact_type="post", title="Пост", content="Текст",
    ))

    callback = _Callback(f"{ARTIFACT_MARK_USED_PREFIX}{artifact.id}")
    _run(on_artifact_mark_used(callback, _ctx(other_ws), artifacts, work))

    assert callback.answers[-1] == ("Материал недоступен.", {"show_alert": True})
    assert callback.message.reply_markup_edits == []
    unchanged = _run(artifacts.get_artifact(owner_ws, artifact.id))
    assert unchanged.status == "draft"


def test_handler_mark_used_without_workspace_context_is_fail_closed(tmp_path: Path) -> None:
    partners, artifacts, work = _stack(tmp_path)
    workspace_id = _provision(partners, 126, "ws-handler-nocontext")
    artifact, _ = _run(artifacts.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост", content="Текст",
    ))

    callback = _Callback(f"{ARTIFACT_MARK_USED_PREFIX}{artifact.id}")
    _run(on_artifact_mark_used(callback, None, artifacts, work))

    assert callback.answers[-1] == ("Материал недоступен.", {"show_alert": True})
    unchanged = _run(artifacts.get_artifact(workspace_id, artifact.id))
    assert unchanged.status == "draft"


def test_handler_mark_used_archived_artifact_shows_neutral_alert(tmp_path: Path) -> None:
    partners, artifacts, work = _stack(tmp_path)
    workspace_id = _provision(partners, 127, "ws-handler-archived")
    artifact, _ = _run(artifacts.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост", content="Текст",
    ))
    _run(artifacts.update_artifact_status(workspace_id, artifact.id, "archived"))

    callback = _Callback(f"{ARTIFACT_MARK_USED_PREFIX}{artifact.id}")
    _run(on_artifact_mark_used(callback, _ctx(workspace_id), artifacts, work))

    assert callback.answers[-1][1] == {"show_alert": True}
    found = _run(work.find_work_item_by_ref(
        workspace_id, ref_type="artifact", ref_id=artifact.id,
    ))
    assert found is None


def test_handler_keep_later_does_not_change_artifact_or_create_work_item(
    tmp_path: Path,
) -> None:
    partners, artifacts, work = _stack(tmp_path)
    workspace_id = _provision(partners, 128, "ws-keep-later")
    artifact, _ = _run(artifacts.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост", content="Текст",
    ))

    callback = _Callback(f"{ARTIFACT_KEEP_LATER_PREFIX}{artifact.id}")
    _run(on_artifact_keep_later(callback, _ctx(workspace_id), artifacts))

    unchanged = _run(artifacts.get_artifact(workspace_id, artifact.id))
    assert unchanged.status == "draft"
    found = _run(work.find_work_item_by_ref(
        workspace_id, ref_type="artifact", ref_id=artifact.id,
    ))
    assert found is None
    assert callback.answers[-1][1] == {}


def test_handler_keep_later_foreign_workspace_is_fail_closed(tmp_path: Path) -> None:
    partners, artifacts, work = _stack(tmp_path)
    owner_ws = _provision(partners, 129, "ws-keep-owner")
    other_ws = _provision(partners, 130, "ws-keep-other")
    artifact, _ = _run(artifacts.create_artifact_with_initial_version(
        owner_ws, artifact_type="post", title="Пост", content="Текст",
    ))

    callback = _Callback(f"{ARTIFACT_KEEP_LATER_PREFIX}{artifact.id}")
    _run(on_artifact_keep_later(callback, _ctx(other_ws), artifacts))

    assert callback.answers[-1] == ("Материал недоступен.", {"show_alert": True})


# ── 5-7. DailyActionsService integration: used excluded, history available ─


def test_used_artifact_is_excluded_from_draft_candidates(tmp_path: Path) -> None:
    partners, artifacts, work = _stack(tmp_path)
    workspace_id = _provision(partners, 131, "ws-nba-draft")
    artifact, _ = _run(artifacts.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост про круизы", content="Текст",
    ))
    _run(ContentUsageService(artifacts, work).mark_used(workspace_id, artifact.id))

    result = _run(DailyActionsService(work, partners, artifacts).build(workspace_id))
    assert not any(action.artifact_id == artifact.id for action in result.actions)


def test_completed_content_item_is_not_actionable(tmp_path: Path) -> None:
    partners, artifacts, work = _stack(tmp_path)
    workspace_id = _provision(partners, 132, "ws-nba-open")
    artifact, _ = _run(artifacts.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост", content="Текст",
    ))
    _run(ContentUsageService(artifacts, work).mark_used(workspace_id, artifact.id))

    result = _run(DailyActionsService(work, partners, artifacts).build(workspace_id))
    assert not any(action.source == "open_content" for action in result.actions)


def test_recent_resolved_content_is_available_as_history(tmp_path: Path) -> None:
    partners, artifacts, work = _stack(tmp_path)
    workspace_id = _provision(partners, 133, "ws-nba-history")
    artifact, _ = _run(artifacts.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост", content="Текст",
    ))
    _run(ContentUsageService(artifacts, work).mark_used(workspace_id, artifact.id))

    result = _run(DailyActionsService(work, partners, artifacts).build(workspace_id))
    assert any(
        item.ref_id == artifact.id and item.lifecycle == "done"
        for item in result.recent_resolved_content
    )


def test_content_usage_is_tenant_isolated(tmp_path: Path) -> None:
    partners, artifacts, work = _stack(tmp_path)
    workspace_a = _provision(partners, 134, "ws-tenant-a")
    workspace_b = _provision(partners, 135, "ws-tenant-b")
    artifact_a, _ = _run(artifacts.create_artifact_with_initial_version(
        workspace_a, artifact_type="post", title="Пост A", content="Текст A",
    ))
    _run(ContentUsageService(artifacts, work).mark_used(workspace_a, artifact_a.id))

    result_b = _run(DailyActionsService(work, partners, artifacts).build(workspace_b))
    assert result_b.recent_resolved_content == ()
    assert not any(action.artifact_id == artifact_a.id for action in result_b.actions)


# ── 10. no TA-specific wording anywhere in this flow ────────────────────────


def test_content_usage_texts_have_no_ta_specific_wording() -> None:
    from app.handlers import content_usage as module

    forbidden = ("Travel Advantage", "TA ", "Carbon", "Xlife")
    texts = (
        module._NOT_FOUND, module._ARCHIVED, module._ALREADY_USED,
        module._MARKED_USED, module._KEEP_LATER,
    )
    for text in texts:
        for term in forbidden:
            assert term not in text


# ── 11. подтверждение публикации не делает LLM-вызов ────────────────────────


def test_mark_used_flow_never_touches_llm_provider() -> None:
    handler_params = inspect.signature(on_artifact_mark_used).parameters
    assert "llm_provider" not in handler_params
    service_params = inspect.signature(ContentUsageService.mark_used).parameters
    assert "llm_provider" not in service_params
