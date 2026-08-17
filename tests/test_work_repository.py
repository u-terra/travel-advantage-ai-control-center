from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.partner_repository import PartnerRepository, empty_business_context
from app.repositories.work_repository import WorkRepository
from app.repositories.workspace_signal_repository import WorkspaceSignalRepository
from app.domain.work import WorkItemValidationError


OWNER_ID = 586249067


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _iso(offset: timedelta) -> str:
    return (datetime.now(timezone.utc) + offset).isoformat()


def _two_workspaces(tmp_path: Path) -> tuple[Path, int, int]:
    db_path = tmp_path / "workspace.sqlite3"
    partners = PartnerRepository(db_path)
    _run(partners.init())
    ta_workspace, _ = _run(partners.ensure_owner_workspace(OWNER_ID))

    context = empty_business_context()
    context["specializations"] = ["cruises"]
    provisioned = _run(partners.provision_partner(
        111222333, "Independent Agency", "independent-agency",
        business_name="Independent Agency",
        business_type="independent_agent",
        short_description="Сторонний тревел-агент.",
        context=context,
    ))
    return db_path, ta_workspace.id, provisioned.workspace.id


def _work_repo(db_path: Path) -> WorkRepository:
    repository = WorkRepository(db_path)
    _run(repository.init())
    return repository


def _artifact_repo(db_path: Path) -> ArtifactRepository:
    repository = ArtifactRepository(db_path)
    _run(repository.init())
    return repository


def _insert_signal_row(db_path: Path, workspace_id: int, radar_signal_id: int) -> int:
    """Радар-таблица не воспроизводится репозиторием в тестах — сырой INSERT
    той же, что и в test_artifact_repository.py, идиомой прямого доступа к
    фикстурной таблице в обход собственного write-пути репозитория."""
    radar_repo = WorkspaceSignalRepository(db_path, db_path.parent / "radar.sqlite3")
    _run(radar_repo.init(legacy_owner_workspace_id=None))
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as db:
        cursor = db.execute(
            "INSERT INTO workspace_signal_interpretations "
            "(workspace_id, radar_signal_id, status, notes, llm_checked, "
            "created_at, updated_at) VALUES (?, ?, 'new', '', 0, ?, ?)",
            (workspace_id, radar_signal_id, now, now),
        )
        return cursor.lastrowid or 0


# ── tenant isolation ─────────────────────────────────────────────────────


def test_subject_isolated_per_workspace(tmp_path: Path) -> None:
    db_path, workspace_a, workspace_b = _two_workspaces(tmp_path)
    repo = _work_repo(db_path)

    subject_a = _run(repo.get_or_create_subject(workspace_a, "Иван"))
    subject_b = _run(repo.get_or_create_subject(workspace_b, "Иван"))

    assert subject_a.id != subject_b.id
    assert _run(repo.get_subject(workspace_b, subject_a.id)) is None
    assert _run(repo.get_subject(workspace_a, subject_b.id)) is None


def test_create_work_item_rejects_subject_from_other_workspace(tmp_path: Path) -> None:
    db_path, workspace_a, workspace_b = _two_workspaces(tmp_path)
    repo = _work_repo(db_path)
    subject_a = _run(repo.get_or_create_subject(workspace_a, "Иван"))

    with pytest.raises(WorkItemValidationError):
        _run(repo.create_work_item(
            workspace_b, kind="dialog", subject_id=subject_a.id,
            loop_state="waiting_reply",
        ))


def test_work_item_cross_workspace_access_returns_none(tmp_path: Path) -> None:
    db_path, workspace_a, workspace_b = _two_workspaces(tmp_path)
    repo = _work_repo(db_path)
    item = _run(repo.create_work_item(
        workspace_a, kind="dialog", loop_state="waiting_reply",
    ))

    assert _run(repo.get_work_item(workspace_b, item.id)) is None
    assert _run(repo.resolve_done(workspace_b, item.id)) is None
    assert _run(repo.mark_reply_received(workspace_b, item.id)) is None


def test_ref_artifact_from_other_workspace_is_rejected(tmp_path: Path) -> None:
    db_path, workspace_a, workspace_b = _two_workspaces(tmp_path)
    repo = _work_repo(db_path)
    artifacts = _artifact_repo(db_path)
    artifact, _ = _run(artifacts.create_artifact_with_initial_version(
        workspace_a, artifact_type="post", title="Пост A", content="текст",
    ))

    with pytest.raises(WorkItemValidationError):
        _run(repo.create_work_item(
            workspace_b, kind="content", lifecycle="done",
            ref_type="artifact", ref_id=artifact.id,
        ))

    # тот же ref в своём workspace — не ошибка.
    own = _run(repo.create_work_item(
        workspace_a, kind="content", lifecycle="done",
        ref_type="artifact", ref_id=artifact.id,
    ))
    assert own.ref_id == artifact.id


def test_ref_signal_from_other_workspace_is_rejected(tmp_path: Path) -> None:
    db_path, workspace_a, workspace_b = _two_workspaces(tmp_path)
    repo = _work_repo(db_path)
    signal_id = _insert_signal_row(db_path, workspace_a, radar_signal_id=42)

    with pytest.raises(WorkItemValidationError):
        _run(repo.create_work_item(
            workspace_b, kind="content", lifecycle="done",
            ref_type="signal", ref_id=signal_id,
        ))

    own = _run(repo.create_work_item(
        workspace_a, kind="content", lifecycle="done",
        ref_type="signal", ref_id=signal_id,
    ))
    assert own.ref_id == signal_id


# ── Иван/Ольга: state transitions ───────────────────────────────────────


def test_ivan_active_dialog_is_actionable(tmp_path: Path) -> None:
    db_path, workspace_a, _ = _two_workspaces(tmp_path)
    repo = _work_repo(db_path)
    ivan = _run(repo.get_or_create_subject(workspace_a, "Иван"))
    item = _run(repo.create_work_item(
        workspace_a, kind="dialog", subject_id=ivan.id, loop_state="waiting_reply",
        next_step="Ждём ответ Ивана", due_at=_iso(timedelta(days=2)),
    ))

    _run(repo.mark_reply_received(
        workspace_a, item.id, next_step="Иван написал «интересно»",
    ))

    now = _iso(timedelta(0))
    actionable = _run(repo.list_open_actionable(workspace_a, now=now))
    assert len(actionable) == 1
    assert actionable[0].item.loop_state == "active_dialog"
    assert actionable[0].item.due_at is None
    assert actionable[0].subject_name == "Иван"


def test_olga_waiting_reply_future_due_is_not_actionable_but_is_waiting(
    tmp_path: Path,
) -> None:
    db_path, workspace_a, _ = _two_workspaces(tmp_path)
    repo = _work_repo(db_path)
    olga = _run(repo.get_or_create_subject(workspace_a, "Ольга"))
    _run(repo.create_work_item(
        workspace_a, kind="dialog", subject_id=olga.id, loop_state="waiting_reply",
        next_step="Ждём ответ Ольги", due_at=_iso(timedelta(days=2)),
    ))

    now = _iso(timedelta(0))
    assert _run(repo.list_open_actionable(workspace_a, now=now)) == []

    waiting = _run(repo.list_waiting_not_due(workspace_a, now=now))
    assert len(waiting) == 1
    assert waiting[0].subject_name == "Ольга"


def test_olga_becomes_actionable_after_due_at(tmp_path: Path) -> None:
    db_path, workspace_a, _ = _two_workspaces(tmp_path)
    repo = _work_repo(db_path)
    olga = _run(repo.get_or_create_subject(workspace_a, "Ольга"))
    _run(repo.create_work_item(
        workspace_a, kind="dialog", subject_id=olga.id, loop_state="waiting_reply",
        next_step="Ждём ответ Ольги", due_at=_iso(-timedelta(days=1)),
    ))

    now = _iso(timedelta(0))
    actionable = _run(repo.list_open_actionable(workspace_a, now=now))
    assert len(actionable) == 1
    assert actionable[0].subject_name == "Ольга"
    assert _run(repo.list_waiting_not_due(workspace_a, now=now)) == []


def test_resolved_content_item_is_not_open_actionable(tmp_path: Path) -> None:
    db_path, workspace_a, _ = _two_workspaces(tmp_path)
    repo = _work_repo(db_path)
    artifacts = _artifact_repo(db_path)
    artifact, _ = _run(artifacts.create_artifact_with_initial_version(
        workspace_a, artifact_type="post", title="Пост X", content="текст",
    ))
    item = _run(repo.create_work_item(
        workspace_a, kind="content", ref_type="artifact", ref_id=artifact.id,
    ))

    now = _iso(timedelta(0))
    assert len(_run(repo.list_open_actionable(workspace_a, now=now))) == 1

    resolved = _run(repo.resolve_done(workspace_a, item.id))
    assert resolved is not None
    assert resolved.lifecycle == "done"
    assert resolved.resolved_at is not None

    assert _run(repo.list_open_actionable(workspace_a, now=now)) == []
    recent = _run(repo.list_recent_resolved_content(
        workspace_a, since=_iso(-timedelta(days=1)),
    ))
    assert [r.id for r in recent] == [item.id]


def test_snooze_moves_item_out_of_actionable_window(tmp_path: Path) -> None:
    db_path, workspace_a, _ = _two_workspaces(tmp_path)
    repo = _work_repo(db_path)
    item = _run(repo.create_work_item(
        workspace_a, kind="dialog", loop_state="waiting_reply",
        due_at=_iso(-timedelta(days=1)),
    ))
    now = _iso(timedelta(0))
    assert len(_run(repo.list_open_actionable(workspace_a, now=now))) == 1

    _run(repo.snooze(workspace_a, item.id, due_at=_iso(timedelta(days=3))))

    assert _run(repo.list_open_actionable(workspace_a, now=now)) == []
    assert len(_run(repo.list_waiting_not_due(workspace_a, now=now))) == 1


def test_dismiss_removes_item_from_both_lists(tmp_path: Path) -> None:
    db_path, workspace_a, _ = _two_workspaces(tmp_path)
    repo = _work_repo(db_path)
    item = _run(repo.create_work_item(
        workspace_a, kind="dialog", loop_state="waiting_reply",
        due_at=_iso(-timedelta(days=1)),
    ))
    dismissed = _run(repo.resolve_dismissed(workspace_a, item.id))
    assert dismissed is not None
    assert dismissed.lifecycle == "dismissed"

    now = _iso(timedelta(0))
    assert _run(repo.list_open_actionable(workspace_a, now=now)) == []
    assert _run(repo.list_waiting_not_due(workspace_a, now=now)) == []


# ── validation ────────────────────────────────────────────────────────────


def test_create_work_item_rejects_content_with_loop_state(tmp_path: Path) -> None:
    db_path, workspace_a, _ = _two_workspaces(tmp_path)
    repo = _work_repo(db_path)
    with pytest.raises(WorkItemValidationError):
        _run(repo.create_work_item(
            workspace_a, kind="content", loop_state="active_dialog",
        ))


def test_create_work_item_rejects_open_dialog_without_loop_state(tmp_path: Path) -> None:
    db_path, workspace_a, _ = _two_workspaces(tmp_path)
    repo = _work_repo(db_path)
    with pytest.raises(WorkItemValidationError):
        _run(repo.create_work_item(workspace_a, kind="dialog"))


def test_create_work_item_rejects_ref_type_without_ref_id(tmp_path: Path) -> None:
    db_path, workspace_a, _ = _two_workspaces(tmp_path)
    repo = _work_repo(db_path)
    with pytest.raises(WorkItemValidationError):
        _run(repo.create_work_item(
            workspace_a, kind="content", ref_type="artifact", ref_id=None,
        ))


def test_rapid_successive_mutations_get_distinct_updated_at(tmp_path: Path) -> None:
    """На этой машине datetime.now() читает системные часы с разрешением
    ~15.6мс (Windows tick) — без монотонной подстраховки в _now() две быстрые
    подряд идущие мутации одного work_item могли бы получить идентичный
    updated_at и, следовательно, неотличимую optimistic-revision."""
    from app.domain.work import work_item_revision

    db_path, workspace_a, _ = _two_workspaces(tmp_path)
    repo = _work_repo(db_path)
    item = _run(repo.create_work_item(
        workspace_a, kind="dialog", loop_state="active_dialog",
        next_step="Отправить подготовленный ответ: Иван",
    ))

    updated_at_values = {item.updated_at}
    revisions = {work_item_revision(item)}
    current = item
    for _ in range(50):
        current = _run(repo.mark_draft_prepared(
            workspace_a, current.id, next_step="Отправить подготовленный ответ: Иван",
        ))
        assert current is not None
        updated_at_values.add(current.updated_at)
        revisions.add(work_item_revision(current))

    assert len(updated_at_values) == 51
    assert len(revisions) == 51
