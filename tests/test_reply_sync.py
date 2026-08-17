"""ReplyWorkSyncService — связывание Reply-черновика с work_item без aiogram.

Не строит клавиатуру (InlineKeyboardMarkup) и не импортирует aiogram —
возвращает WorkItem/None. app/handlers/tasks.py сам решает, какую клавиатуру
показать под результатом.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.partner_repository import PartnerRepository, empty_business_context
from app.repositories.work_repository import WorkRepository
from app.services.reply_sync import ReplyBridgeContext, ReplyWorkSyncService


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _stack(tmp_path: Path) -> tuple[WorkRepository, ArtifactRepository, int]:
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
    artifact_repo = ArtifactRepository(db_path)
    _run(artifact_repo.init())
    return work_repo, artifact_repo, workspace_id


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
    import app.services.reply_sync as module

    assert "aiogram" not in _imported_top_level_modules(module)


def test_sync_creates_work_item_and_artifact_for_new_subject(tmp_path: Path) -> None:
    work_repo, artifact_repo, workspace_id = _stack(tmp_path)
    ivan = _run(work_repo.get_or_create_subject(workspace_id, "Иван"))
    context = ReplyBridgeContext(work_item_id=None, subject_id=ivan.id, subject_name="Иван")

    item = _run(ReplyWorkSyncService(work_repo, artifact_repo).sync(
        workspace_id, "Черновик ответа Ивану", context,
    ))

    assert item is not None
    assert item.kind == "dialog"
    assert item.lifecycle == "open"
    assert item.loop_state == "active_dialog"
    assert item.subject_id == ivan.id
    assert item.ref_type == "artifact"
    assert item.ref_id is not None

    artifacts = _run(artifact_repo.list_artifacts(workspace_id))
    assert len(artifacts) == 1
    assert artifacts[0].artifact_type == "client_message"
    assert artifacts[0].id == item.ref_id


def test_sync_reuses_existing_work_item_without_new_artifact(tmp_path: Path) -> None:
    work_repo, artifact_repo, workspace_id = _stack(tmp_path)
    ivan = _run(work_repo.get_or_create_subject(workspace_id, "Иван"))
    existing = _run(work_repo.create_work_item(
        workspace_id, kind="dialog", subject_id=ivan.id, loop_state="waiting_reply",
        next_step="Ждём ответ: Иван", due_at="2026-01-01T00:00:00+00:00",
    ))
    context = ReplyBridgeContext(
        work_item_id=existing.id, subject_id=ivan.id, subject_name="Иван",
    )

    item = _run(ReplyWorkSyncService(work_repo, artifact_repo).sync(
        workspace_id, "Черновик продолжения", context,
    ))

    assert item is not None
    assert item.id == existing.id
    assert item.loop_state == "active_dialog"
    assert item.due_at is None
    assert _run(artifact_repo.list_artifacts(workspace_id)) == []


def test_sync_without_subject_or_work_item_returns_none(tmp_path: Path) -> None:
    work_repo, artifact_repo, workspace_id = _stack(tmp_path)
    context = ReplyBridgeContext(work_item_id=None, subject_id=None, subject_name=None)

    item = _run(ReplyWorkSyncService(work_repo, artifact_repo).sync(
        workspace_id, "Черновик без адресата", context,
    ))

    assert item is None


def test_sync_works_without_artifact_repository(tmp_path: Path) -> None:
    """artifact_repository опционален: если его нет, work_item всё равно
    создаётся, просто без ref_type/ref_id."""
    work_repo, _artifact_repo, workspace_id = _stack(tmp_path)
    ivan = _run(work_repo.get_or_create_subject(workspace_id, "Иван"))
    context = ReplyBridgeContext(work_item_id=None, subject_id=ivan.id, subject_name="Иван")

    item = _run(ReplyWorkSyncService(work_repo, None).sync(
        workspace_id, "Черновик без artifact_repository", context,
    ))

    assert item is not None
    assert item.ref_type is None
    assert item.ref_id is None


def test_sync_is_tenant_scoped(tmp_path: Path) -> None:
    db_path = tmp_path / "workspace.sqlite3"
    partners = PartnerRepository(db_path)
    _run(partners.init())
    context_a = empty_business_context()
    context_a["specializations"] = ["cruises"]
    workspace_a = _run(partners.provision_partner(
        111222333, "Agency A", "agency-a", business_name="Agency A",
        business_type="independent_agent", short_description="A", context=context_a,
    )).workspace.id

    work_repo = WorkRepository(db_path)
    _run(work_repo.init())
    artifact_repo = ArtifactRepository(db_path)
    _run(artifact_repo.init())

    ivan = _run(work_repo.get_or_create_subject(workspace_a, "Иван"))
    reply_context = ReplyBridgeContext(work_item_id=None, subject_id=ivan.id, subject_name="Иван")

    item = _run(ReplyWorkSyncService(work_repo, artifact_repo).sync(
        workspace_a, "Черновик", reply_context,
    ))

    assert item is not None
    assert item.workspace_id == workspace_a
