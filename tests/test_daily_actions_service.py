"""DailyActionsService — сборка данных для «Что делать сегодня» без aiogram.

Проверяет, что вынесенный сервис даёт тот же набор actions/waiting, что и
раньше давал handler (ranking/NextBestActionService не менялись — это
регрессия на сборку входа, не на ranking).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

from app.domain.work import DailyActions
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.partner_repository import PartnerRepository, empty_business_context
from app.repositories.work_repository import WorkRepository
from app.services.daily_actions import DailyActionsService


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _iso(offset: timedelta) -> str:
    from datetime import datetime, timezone
    return (datetime.now(timezone.utc) + offset).isoformat()


def _stack(tmp_path: Path, *, ta_affiliated: bool = False) -> tuple[
    WorkRepository, PartnerRepository, ArtifactRepository, int,
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
    return work_repo, partners, artifact_repo, workspace_id


def test_build_returns_plain_domain_result() -> None:
    """Публичный результат — DailyActions, обычный dataclass из
    app.domain.work, без единого aiogram-импорта в сервисе. Проверяем
    реальные import-выражения, а не любое упоминание слова "aiogram" —
    докстрины сервиса сами объясняют, что он не зависит от aiogram."""
    import ast
    import inspect
    import app.services.daily_actions as module

    tree = ast.parse(inspect.getsource(module))
    imported_modules = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "aiogram" not in imported_modules
    assert DailyActionsService.build.__annotations__.get("return") == "DailyActions"


def test_build_surfaces_active_dialog_above_waiting(tmp_path: Path) -> None:
    work_repo, partners, artifacts, workspace_id = _stack(tmp_path)
    ivan = _run(work_repo.get_or_create_subject(workspace_id, "Иван"))
    _run(work_repo.create_work_item(
        workspace_id, kind="dialog", subject_id=ivan.id, loop_state="active_dialog",
        next_step="Продолжить с Иваном",
    ))
    olga = _run(work_repo.get_or_create_subject(workspace_id, "Ольга"))
    _run(work_repo.create_work_item(
        workspace_id, kind="dialog", subject_id=olga.id, loop_state="waiting_reply",
        next_step="Ждём ответ: Ольга", due_at=_iso(timedelta(days=2)),
    ))

    daily = _run(DailyActionsService(work_repo, partners, artifacts).build(workspace_id))

    assert isinstance(daily, DailyActions)
    assert daily.actions[0].source == "active_dialog"
    assert daily.actions[0].subject_name == "Иван"
    assert [w.subject_name for w in daily.waiting] == ["Ольга"]


def test_build_respects_due_at_boundary(tmp_path: Path) -> None:
    """Тот же сценарий Ольги, что и на уровне handler/repository: future
    due_at -> waiting, не action; после due_at -> action."""
    work_repo, partners, artifacts, workspace_id = _stack(tmp_path)
    olga = _run(work_repo.get_or_create_subject(workspace_id, "Ольга"))
    _run(work_repo.create_work_item(
        workspace_id, kind="dialog", subject_id=olga.id, loop_state="waiting_reply",
        next_step="Ждём ответ: Ольга", due_at=_iso(-timedelta(minutes=1)),
    ))

    daily = _run(DailyActionsService(work_repo, partners, artifacts).build(
        workspace_id, now=_iso(timedelta(0)),
    ))

    assert daily.actions[0].source == "due_follow_up"
    assert daily.waiting == ()


def test_build_ta_wording_follows_profile(tmp_path: Path) -> None:
    """TA-formulировка cold-contact fallback зависит от ta_affiliated —
    сервис не хардкодит его, просто передаёт в уже существующий
    NextBestActionService, как раньше делал handler."""
    work_repo, partners, artifacts, workspace_id = _stack(tmp_path, ta_affiliated=True)

    daily = _run(DailyActionsService(work_repo, partners, artifacts).build(workspace_id))

    fallback = next(a for a in daily.actions if a.source == "cold_contact_fallback")
    assert "Travel Advantage" in fallback.detail


def test_build_is_tenant_scoped(tmp_path: Path) -> None:
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
    artifacts = ArtifactRepository(db_path)
    _run(artifacts.init())

    ivan = _run(work_repo.get_or_create_subject(workspace_a, "Иван"))
    _run(work_repo.create_work_item(
        workspace_a, kind="dialog", subject_id=ivan.id, loop_state="active_dialog",
        next_step="Продолжить с Иваном",
    ))

    daily_b = _run(DailyActionsService(work_repo, partners, artifacts).build(workspace_b))

    assert not any(a.subject_name == "Иван" for a in daily_b.actions)
