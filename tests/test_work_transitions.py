"""WorkTransitionService — переходы состояния work_item без aiogram.

Централизует то, что раньше было размазано по callback-хендлерам
daily_actions.py: revision-guard, допустимость перехода, единый набор
исходов (ok/stale/not_found/invalid_state). Тесты здесь бьют по сервису
напрямую — без Message/CallbackQuery/FSM.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.domain.work import work_item_revision
from app.repositories.partner_repository import PartnerRepository, empty_business_context
from app.repositories.work_repository import WorkRepository
from app.services.work_transitions import TransitionStatus, WorkTransitionService


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _iso(offset: timedelta) -> str:
    return (datetime.now(timezone.utc) + offset).isoformat()


def _stack(tmp_path: Path) -> tuple[WorkRepository, int]:
    db_path = tmp_path / "workspace.sqlite3"
    partners = PartnerRepository(db_path)
    _run(partners.init())
    context = empty_business_context()
    context["specializations"] = ["cruises"]
    provisioned = _run(partners.provision_partner(
        111222333, "Independent Agency", "independent-agency",
        business_name="Independent Agency", business_type="independent_agent",
        short_description="Сторонний тревел-агент.", context=context,
    ))
    workspace_id = provisioned.workspace.id
    work_repo = WorkRepository(db_path)
    _run(work_repo.init())
    return work_repo, workspace_id


def _imported_top_level_modules(module: Any) -> set[str]:
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(module))
    return {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def test_service_module_does_not_import_aiogram() -> None:
    import app.services.work_transitions as module

    assert "aiogram" not in _imported_top_level_modules(module)


def test_mark_sent_moves_active_dialog_to_waiting_reply(tmp_path: Path) -> None:
    work_repo, workspace_id = _stack(tmp_path)
    item = _run(work_repo.create_work_item(
        workspace_id, kind="dialog", loop_state="active_dialog",
        next_step="Отправить подготовленный ответ: Иван",
    ))

    result = _run(WorkTransitionService(work_repo).mark_sent(workspace_id, item.id))

    assert result.status is TransitionStatus.OK
    assert result.work_item.loop_state == "waiting_reply"
    assert result.work_item.due_at is not None


def test_mark_reply_received_moves_waiting_to_active_dialog(tmp_path: Path) -> None:
    work_repo, workspace_id = _stack(tmp_path)
    item = _run(work_repo.create_work_item(
        workspace_id, kind="dialog", loop_state="waiting_reply",
        next_step="Ждём ответ", due_at=_iso(timedelta(days=2)),
    ))

    result = _run(WorkTransitionService(work_repo).mark_reply_received(workspace_id, item.id))

    assert result.status is TransitionStatus.OK
    assert result.work_item.loop_state == "active_dialog"
    assert result.work_item.due_at is None


def test_snooze_moves_due_at_forward(tmp_path: Path) -> None:
    work_repo, workspace_id = _stack(tmp_path)
    item = _run(work_repo.create_work_item(
        workspace_id, kind="dialog", loop_state="waiting_reply",
        next_step="Ждём ответ", due_at=_iso(-timedelta(minutes=1)),
    ))

    result = _run(WorkTransitionService(work_repo).snooze(workspace_id, item.id))

    assert result.status is TransitionStatus.OK
    assert datetime.fromisoformat(result.work_item.due_at) > datetime.now(timezone.utc)


def test_mark_done_resolves_item(tmp_path: Path) -> None:
    work_repo, workspace_id = _stack(tmp_path)
    item = _run(work_repo.create_work_item(
        workspace_id, kind="content", ref_type=None, ref_id=None,
    ))

    result = _run(WorkTransitionService(work_repo).mark_done(workspace_id, item.id))

    assert result.status is TransitionStatus.OK
    assert result.work_item.lifecycle == "done"


def test_mark_dismissed_without_revision_matches_daily_action_dismiss(tmp_path: Path) -> None:
    work_repo, workspace_id = _stack(tmp_path)
    item = _run(work_repo.create_work_item(
        workspace_id, kind="dialog", loop_state="active_dialog", next_step="x",
    ))

    result = _run(WorkTransitionService(work_repo).mark_dismissed(workspace_id, item.id))

    assert result.status is TransitionStatus.OK
    assert result.work_item.lifecycle == "dismissed"


def test_mark_dismissed_not_found_is_reported_without_crash(tmp_path: Path) -> None:
    work_repo, workspace_id = _stack(tmp_path)

    result = _run(WorkTransitionService(work_repo).mark_dismissed(workspace_id, 999))

    assert result.status is TransitionStatus.NOT_FOUND
    assert result.work_item is None


def test_revision_stale_guard_rejects_outdated_sent(tmp_path: Path) -> None:
    """Ключевой сценарий этой линии работы: revision, захваченный до более
    новой мутации того же item, отклоняется как stale — item не мутируется."""
    work_repo, workspace_id = _stack(tmp_path)
    item = _run(work_repo.create_work_item(
        workspace_id, kind="dialog", loop_state="active_dialog", next_step="x",
    ))
    stale_revision = work_item_revision(item)

    _run(work_repo.mark_draft_prepared(workspace_id, item.id, next_step="новый черновик"))

    result = _run(WorkTransitionService(work_repo).mark_sent(
        workspace_id, item.id, revision=stale_revision,
    ))

    assert result.status is TransitionStatus.STALE
    unchanged = _run(work_repo.get_work_item(workspace_id, item.id))
    assert unchanged.loop_state == "active_dialog"


def test_revision_matching_current_state_succeeds(tmp_path: Path) -> None:
    work_repo, workspace_id = _stack(tmp_path)
    item = _run(work_repo.create_work_item(
        workspace_id, kind="dialog", loop_state="active_dialog", next_step="x",
    ))
    current_revision = work_item_revision(item)

    result = _run(WorkTransitionService(work_repo).mark_sent(
        workspace_id, item.id, revision=current_revision,
    ))

    assert result.status is TransitionStatus.OK
    assert result.work_item.loop_state == "waiting_reply"


def test_later_does_not_mutate_item(tmp_path: Path) -> None:
    work_repo, workspace_id = _stack(tmp_path)
    item = _run(work_repo.create_work_item(
        workspace_id, kind="dialog", loop_state="active_dialog", next_step="x",
    ))
    revision = work_item_revision(item)

    result = _run(WorkTransitionService(work_repo).later(
        workspace_id, item.id, revision=revision,
    ))

    assert result.status is TransitionStatus.OK
    unchanged = _run(work_repo.get_work_item(workspace_id, item.id))
    assert unchanged.updated_at == item.updated_at


def test_prepare_bridge_accepts_active_dialog_and_rejects_future_due(tmp_path: Path) -> None:
    work_repo, workspace_id = _stack(tmp_path)
    active_item = _run(work_repo.create_work_item(
        workspace_id, kind="dialog", loop_state="active_dialog", next_step="x",
    ))
    future_item = _run(work_repo.create_work_item(
        workspace_id, kind="dialog", loop_state="waiting_reply",
        next_step="x", due_at=_iso(timedelta(days=2)),
    ))

    service = WorkTransitionService(work_repo)
    ok_result = _run(service.prepare_bridge(workspace_id, active_item.id))
    rejected_result = _run(service.prepare_bridge(workspace_id, future_item.id))

    assert ok_result.status is TransitionStatus.OK
    assert rejected_result.status is TransitionStatus.INVALID_STATE


def test_transitions_are_tenant_scoped(tmp_path: Path) -> None:
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
        workspace_a, kind="dialog", loop_state="active_dialog", next_step="x",
    ))

    result = _run(WorkTransitionService(work_repo).mark_done(workspace_b, item.id))

    assert result.status is TransitionStatus.NOT_FOUND
    untouched = _run(work_repo.get_work_item(workspace_a, item.id))
    assert untouched.lifecycle == "open"
