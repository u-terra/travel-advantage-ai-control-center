"""Work-домен: «кто» (WorkSubject) и «что осталось сделать» (WorkItem).

Это не CRM. WorkSubject хранит только имя, достаточное чтобы связать между
собой несколько WorkItem одного и того же человека — без канала, телефона,
почты и тегов. lifecycle (жив ли сам WorkItem как объект учёта) и loop_state
(чья сейчас очередь действовать в диалоге с субъектом) — два независимых
уровня модели и намеренно не смешаны в один enum. «follow-up наступил» не
хранится нигде: это вычисляемое условие (loop_state='waiting_reply' AND
due_at <= now), а не отдельное состояние — иначе кто-то должен был бы
физически переводить строку в это состояние ровно в момент наступления срока.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


WORK_ITEM_KINDS = frozenset({"dialog", "content"})
WORK_ITEM_LIFECYCLES = frozenset({"open", "done", "dismissed"})
WORK_ITEM_LOOP_STATES = frozenset({"active_dialog", "waiting_reply"})
WORK_ITEM_REF_TYPES = frozenset({"artifact", "signal"})


class WorkSubjectValidationError(ValueError):
    pass


class WorkItemValidationError(ValueError):
    pass


def normalize_subject_name(name: str) -> str:
    """Ключ дедупликации субъекта: trim + схлопывание пробелов + lower.

    "Иван", "иван", " ИВАН " должны дать один и тот же ключ. Схлопывание
    делает ``" ".join(name.split())`` — это же попутно убирает табы и
    внутренние двойные пробелы, не только края строки. ``str.lower()``
    (а не expression-индекс в SQLite) выбран потому, что стоковый SQLite
    без ICU-расширения не гарантирует корректный ``lower()`` для кириллицы
    в выражении индекса — а имена здесь русские.
    """
    if not isinstance(name, str):
        raise WorkSubjectValidationError("Имя субъекта должно быть строкой")
    collapsed = " ".join(name.split())
    if not collapsed:
        raise WorkSubjectValidationError("Имя субъекта не должно быть пустым")
    return collapsed.lower()


def display_subject_name(name: str) -> str:
    """Показ-версия имени: trim + схлопывание пробелов, регистр сохранён."""
    if not isinstance(name, str):
        raise WorkSubjectValidationError("Имя субъекта должно быть строкой")
    collapsed = " ".join(name.split())
    if not collapsed:
        raise WorkSubjectValidationError("Имя субъекта не должно быть пустым")
    return collapsed


@dataclass(frozen=True)
class WorkSubject:
    id: int
    workspace_id: int
    name: str
    normalized_name: str
    created_at: str


@dataclass(frozen=True)
class WorkItem:
    id: int
    workspace_id: int
    subject_id: int | None
    kind: str
    lifecycle: str
    loop_state: str | None
    next_step: str
    due_at: str | None
    ref_type: str | None
    ref_id: int | None
    created_at: str
    updated_at: str
    resolved_at: str | None


def work_item_revision(item: WorkItem) -> str:
    """Короткий (10 hex) optimistic-revision токен из updated_at — для
    callback_data кнопок, показанных под конкретным Reply-черновиком.

    Нет отдельной колонки version: updated_at уже меняется при КАЖДОЙ
    мутации work_item (mark_reply_sent/received, snooze, resolve_*,
    подготовка нового черновика) — этого достаточно, чтобы отличить кнопки,
    показанные под текущим состоянием, от кнопок под уже устаревшим. SHA-256
    (не сам updated_at) — чтобы токен помещался в 64-байтный лимит
    callback_data вместе с префиксом и id.
    """
    return hashlib.sha256(item.updated_at.encode("utf-8")).hexdigest()[:10]


@dataclass(frozen=True)
class WorkItemView:
    """WorkItem + отображаемое имя субъекта — то, что реально отдают
    list_open_actionable/list_waiting_not_due. Тот же приём, что и
    WorkspaceSignalRecord: сырая строка таблицы (WorkItem) остаётся чистым
    зеркалом схемы, а join для показа живёт в отдельной view-обёртке."""

    item: WorkItem
    subject_name: str | None


# ── NextBestAction: входные и выходные формы ────────────────────────────────

ACTION_SOURCES = frozenset({
    "active_dialog",
    "due_follow_up",
    "open_content",
    "draft_content",
    "signal_opportunity",
    "profile_incomplete",
    "cold_contact_fallback",
})


@dataclass(frozen=True)
class SignalOpportunity:
    """Минимальный signal-кандидат для NextBestActionService.

    Намеренно не тот же тип, что WorkspaceSignalRecord/LeadSignal — сервис
    не должен знать про Lead Radar, radar_db_path или recommender. Вызывающий
    код (будущий хендлер) сам строит этот маленький объект из уже
    авторизованной записи сигнала.
    """

    signal_interpretation_id: int
    title: str
    reason: str


@dataclass(frozen=True)
class NextAction:
    source: str
    headline: str
    detail: str
    work_item_id: int | None = None
    subject_name: str | None = None
    artifact_id: int | None = None
    signal_interpretation_id: int | None = None

    def __post_init__(self) -> None:
        if self.source not in ACTION_SOURCES:
            raise WorkItemValidationError(f"Неизвестный source NextAction: {self.source!r}")


@dataclass(frozen=True)
class WaitingSubject:
    work_item_id: int
    subject_id: int | None
    subject_name: str | None
    next_step: str
    due_at: str | None


@dataclass(frozen=True)
class DailyActions:
    actions: tuple[NextAction, ...]
    waiting: tuple[WaitingSubject, ...]
    # История недавно опубликованного/использованного контента (kind='content',
    # lifecycle='done') — контекст для будущих этапов (повтор темы, история
    # публикаций), сейчас нигде не рендерится в UI. Не влияет на ranking/actions.
    recent_resolved_content: tuple[WorkItem, ...] = ()
