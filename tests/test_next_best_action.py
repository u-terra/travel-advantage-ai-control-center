from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app.domain.work import WorkItem, WorkItemView
from app.repositories.partner_repository import PartnerRepository, empty_business_context
from app.repositories.work_repository import WorkRepository
from app.services.next_best_action import MAX_ACTIONS, NextBestActionService


OWNER_ID = 586249067
WORKSPACE_ID = 1


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _iso(offset: timedelta) -> str:
    return (datetime.now(timezone.utc) + offset).isoformat()


def _dialog_view(
    *,
    item_id: int,
    loop_state: str,
    subject_name: str | None,
    next_step: str = "",
    due_at: str | None = None,
    workspace_id: int = WORKSPACE_ID,
) -> WorkItemView:
    now = _iso(timedelta(0))
    return WorkItemView(
        item=WorkItem(
            id=item_id, workspace_id=workspace_id, subject_id=item_id,
            kind="dialog", lifecycle="open", loop_state=loop_state,
            next_step=next_step, due_at=due_at, ref_type=None, ref_id=None,
            created_at=now, updated_at=now, resolved_at=None,
        ),
        subject_name=subject_name,
    )


# ── pure ranking/cap logic ───────────────────────────────────────────────


def test_active_dialog_outranks_cold_contact_fallback() -> None:
    ivan = _dialog_view(item_id=1, loop_state="active_dialog", subject_name="Иван")
    service = NextBestActionService()

    result = service.build(
        workspace_id=WORKSPACE_ID, ta_affiliated=True,
        open_work_items=[ivan], include_cold_contact_fallback=True,
    )

    sources = [action.source for action in result.actions]
    assert sources[0] == "active_dialog"
    assert "cold_contact_fallback" in sources
    assert sources.index("active_dialog") < sources.index("cold_contact_fallback")


def test_actions_are_capped_at_three() -> None:
    views = [
        _dialog_view(item_id=i, loop_state="active_dialog", subject_name=f"Контакт {i}")
        for i in range(1, 6)
    ]
    service = NextBestActionService()

    result = service.build(
        workspace_id=WORKSPACE_ID, ta_affiliated=True,
        open_work_items=views, include_cold_contact_fallback=True,
    )

    assert len(result.actions) == MAX_ACTIONS
    assert all(action.source == "active_dialog" for action in result.actions)


def test_independent_workspace_gets_no_ta_wording_in_fallback() -> None:
    service = NextBestActionService()

    result = service.build(
        workspace_id=WORKSPACE_ID, ta_affiliated=False,
        include_cold_contact_fallback=True,
    )

    assert len(result.actions) == 1
    action = result.actions[0]
    assert action.source == "cold_contact_fallback"
    assert "Travel Advantage" not in action.headline
    assert "Travel Advantage" not in action.detail


def test_ta_workspace_gets_ta_wording_in_fallback() -> None:
    service = NextBestActionService()

    result = service.build(
        workspace_id=WORKSPACE_ID, ta_affiliated=True,
        include_cold_contact_fallback=True,
    )

    assert "Travel Advantage" in result.actions[0].detail


def test_cross_workspace_work_item_is_rejected() -> None:
    other_workspace_view = _dialog_view(
        item_id=1, loop_state="active_dialog", subject_name="Чужой",
        workspace_id=WORKSPACE_ID + 1,
    )
    service = NextBestActionService()

    with pytest.raises(PermissionError):
        service.build(
            workspace_id=WORKSPACE_ID, ta_affiliated=True,
            open_work_items=[other_workspace_view],
        )


# ── Иван/Ольга: полный стек WorkRepository + NextBestActionService ───────


def _provisioned_workspace(tmp_path: Path) -> tuple[Path, int]:
    db_path = tmp_path / "workspace.sqlite3"
    partners = PartnerRepository(db_path)
    _run(partners.init())
    context = empty_business_context()
    context["specializations"] = ["cruises"]
    provisioned = _run(partners.provision_partner(
        111222333, "Independent Agency", "independent-agency",
        business_name="Independent Agency",
        business_type="independent_agent",
        short_description="Сторонний тревел-агент.",
        context=context,
    ))
    return db_path, provisioned.workspace.id


def test_ivan_and_olga_daily_actions_end_to_end(tmp_path: Path) -> None:
    db_path, workspace_id = _provisioned_workspace(tmp_path)
    repo = WorkRepository(db_path)
    _run(repo.init())

    ivan = _run(repo.get_or_create_subject(workspace_id, "Иван"))
    ivan_item = _run(repo.create_work_item(
        workspace_id, kind="dialog", subject_id=ivan.id, loop_state="waiting_reply",
        next_step="Ждём ответ Ивана на первое сообщение", due_at=_iso(timedelta(days=2)),
    ))
    _run(repo.mark_reply_received(
        workspace_id, ivan_item.id, next_step="Иван написал «интересно»",
    ))

    olga = _run(repo.get_or_create_subject(workspace_id, "Ольга"))
    _run(repo.create_work_item(
        workspace_id, kind="dialog", subject_id=olga.id, loop_state="waiting_reply",
        next_step="Ждём ответ Ольги", due_at=_iso(timedelta(days=2)),
    ))

    now = _iso(timedelta(0))
    open_items = _run(repo.list_open_actionable(workspace_id, now=now))
    waiting_items = _run(repo.list_waiting_not_due(workspace_id, now=now))

    result = NextBestActionService().build(
        workspace_id=workspace_id, ta_affiliated=False,
        open_work_items=open_items, waiting_not_due=waiting_items,
    )

    assert result.actions[0].source == "active_dialog"
    assert result.actions[0].subject_name == "Иван"
    assert [w.subject_name for w in result.waiting] == ["Ольга"]
    assert "Иван" not in [w.subject_name for w in result.waiting]


def test_olga_appears_in_actions_only_after_due_at(tmp_path: Path) -> None:
    db_path, workspace_id = _provisioned_workspace(tmp_path)
    repo = WorkRepository(db_path)
    _run(repo.init())

    olga = _run(repo.get_or_create_subject(workspace_id, "Ольга"))
    _run(repo.create_work_item(
        workspace_id, kind="dialog", subject_id=olga.id, loop_state="waiting_reply",
        next_step="Написать Ольге ещё раз", due_at=_iso(-timedelta(minutes=1)),
    ))

    now = _iso(timedelta(0))
    open_items = _run(repo.list_open_actionable(workspace_id, now=now))
    waiting_items = _run(repo.list_waiting_not_due(workspace_id, now=now))

    result = NextBestActionService().build(
        workspace_id=workspace_id, ta_affiliated=False,
        open_work_items=open_items, waiting_not_due=waiting_items,
        include_cold_contact_fallback=False,
    )

    assert len(result.actions) == 1
    assert result.actions[0].subject_name == "Ольга"
    assert result.waiting == ()
