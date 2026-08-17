"""Переходы состояния work_item — transport-independent слой.

Централизует правила, которые раньше были размазаны по callback-хендлерам
app/handlers/daily_actions.py: revision-guard, допустимость перехода
(lifecycle/loop_state), единый набор исходов. Workspace ownership обеспечен
самим WorkRepository — все его методы уже workspace-scoped (WHERE
workspace_id = ?), здесь это не переизобретается.

Ничего не знает про aiogram: ни CallbackQuery, ни callback.answer, ни
Telegram-текстов — только WorkItem и типизированный TransitionResult.
Telegram-хендлер (и будущий VK-хендлер) сам решает, каким сообщением
ответить на каждый TransitionStatus.

Каждый метод сохраняет РОВНО тот же паттерн вызовов WorkRepository, что был
в хендлере до выноса (где не было pre-fetch — его и здесь нет; где revision
требовал сначала прочитать item — это сохранено). Это не архитектурная
прихоть, а условие «не менять поведение»: часть существующих тестов уже
проверяет точное число и аргументы вызовов repository-методов.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from app.domain.work import WorkItem, work_item_revision
from app.repositories.work_repository import WorkRepository

_SNOOZE_OFFSET = timedelta(days=2)


class TransitionStatus(str, Enum):
    OK = "ok"
    STALE = "stale"
    NOT_FOUND = "not_found"
    INVALID_STATE = "invalid_state"


@dataclass(frozen=True)
class TransitionResult:
    status: TransitionStatus
    work_item: WorkItem | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_promptable(item: WorkItem, *, now: str) -> bool:
    """Может ли work_item сейчас вести в Reply flow («Продолжить разговор» /
    «Написать снова»): lifecycle='open' и либо active_dialog, либо
    waiting_reply с уже наступившим due_at."""
    if item.lifecycle != "open":
        return False
    if item.loop_state == "active_dialog":
        return True
    if item.loop_state == "waiting_reply" and item.due_at is not None and item.due_at <= now:
        return True
    return False


class WorkTransitionService:
    def __init__(self, work_repository: WorkRepository) -> None:
        self._work_repository = work_repository

    async def prepare_bridge(
        self, workspace_id: int, work_item_id: int, *, now: str | None = None,
    ) -> TransitionResult:
        item = await self._work_repository.get_work_item(workspace_id, work_item_id)
        if item is None:
            return TransitionResult(TransitionStatus.NOT_FOUND)
        if not _is_promptable(item, now=now or _now()):
            return TransitionResult(TransitionStatus.INVALID_STATE, work_item=item)
        return TransitionResult(TransitionStatus.OK, work_item=item)

    async def mark_reply_received(
        self, workspace_id: int, work_item_id: int,
    ) -> TransitionResult:
        updated = await self._work_repository.mark_reply_received(workspace_id, work_item_id)
        if updated is None:
            return TransitionResult(TransitionStatus.NOT_FOUND)
        return TransitionResult(TransitionStatus.OK, work_item=updated)

    async def mark_sent(
        self, workspace_id: int, work_item_id: int, *,
        revision: str | None = None, now: str | None = None,
    ) -> TransitionResult:
        """«✅ Отправил»: active_dialog/waiting_reply -> waiting_reply с новым
        due_at. next_step («Ждём ответ: <subject>») — состояние work_item,
        не UI-текст, поэтому вычисляется здесь, а не в хендлере."""
        item = await self._work_repository.get_work_item(workspace_id, work_item_id)
        if item is None:
            return TransitionResult(TransitionStatus.NOT_FOUND)
        if revision is not None and work_item_revision(item) != revision:
            return TransitionResult(TransitionStatus.STALE, work_item=item)

        subject_name: str | None = None
        if item.subject_id is not None:
            subject = await self._work_repository.get_subject(workspace_id, item.subject_id)
            subject_name = subject.name if subject is not None else None
        next_step = f"Ждём ответ: {subject_name}" if subject_name else "Ждём ответ."

        due_at = _add_offset(now or _now())
        updated = await self._work_repository.mark_reply_sent(
            workspace_id, work_item_id, due_at=due_at, next_step=next_step,
        )
        if updated is None:
            return TransitionResult(TransitionStatus.INVALID_STATE, work_item=item)
        return TransitionResult(TransitionStatus.OK, work_item=updated)

    async def later(
        self, workspace_id: int, work_item_id: int, *, revision: str | None = None,
    ) -> TransitionResult:
        """«🕒 Позже»: ничего не меняет — только проверяет, что item
        существует и (если передан revision) актуален."""
        item = await self._work_repository.get_work_item(workspace_id, work_item_id)
        if item is None:
            return TransitionResult(TransitionStatus.NOT_FOUND)
        if revision is not None and work_item_revision(item) != revision:
            return TransitionResult(TransitionStatus.STALE, work_item=item)
        return TransitionResult(TransitionStatus.OK, work_item=item)

    async def snooze(
        self, workspace_id: int, work_item_id: int, *, now: str | None = None,
    ) -> TransitionResult:
        due_at = _add_offset(now or _now())
        updated = await self._work_repository.snooze(
            workspace_id, work_item_id, due_at=due_at,
        )
        if updated is None:
            return TransitionResult(TransitionStatus.NOT_FOUND)
        return TransitionResult(TransitionStatus.OK, work_item=updated)

    async def mark_done(self, workspace_id: int, work_item_id: int) -> TransitionResult:
        updated = await self._work_repository.resolve_done(workspace_id, work_item_id)
        if updated is None:
            return TransitionResult(TransitionStatus.NOT_FOUND)
        return TransitionResult(TransitionStatus.OK, work_item=updated)

    async def mark_dismissed(
        self, workspace_id: int, work_item_id: int, *, revision: str | None = None,
    ) -> TransitionResult:
        """Общий для daily_action:dismiss (без revision) и reply_confirm:
        dismiss (с revision) — единственная разница в том, сверяется ли
        revision перед resolve_dismissed."""
        if revision is not None:
            item = await self._work_repository.get_work_item(workspace_id, work_item_id)
            if item is None:
                return TransitionResult(TransitionStatus.NOT_FOUND)
            if work_item_revision(item) != revision:
                return TransitionResult(TransitionStatus.STALE, work_item=item)

        updated = await self._work_repository.resolve_dismissed(workspace_id, work_item_id)
        if updated is None:
            return TransitionResult(TransitionStatus.NOT_FOUND)
        return TransitionResult(TransitionStatus.OK, work_item=updated)


def _add_offset(now: str) -> str:
    return (datetime.fromisoformat(now) + _SNOOZE_OFFSET).isoformat()
