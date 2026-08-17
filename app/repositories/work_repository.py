"""Workspace-scoped хранилище WorkSubject/WorkItem — фундамент «Что делать сегодня».

Только хранение, чтение и явные переходы состояния. Приоритезация и выбор
1-3 действий здесь не живут — это работа NextBestActionService, который
получает уже отфильтрованные, workspace-scoped списки из этого репозитория.

Никакого произвольного ref_table: ref_type — закрытый CHECK-enum
('artifact' | 'signal'), а принадлежность ref_id нужному workspace
проверяется прямым SELECT в соответствующую таблицу перед записью — по
аналогии с тем, как ArtifactRepository.create_artifact уже проверяет
принадлежность source_id workspace перед вставкой.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from app.domain.work import (
    WORK_ITEM_KINDS,
    WORK_ITEM_LIFECYCLES,
    WORK_ITEM_LOOP_STATES,
    WORK_ITEM_REF_TYPES,
    WorkItem,
    WorkItemValidationError,
    WorkSubject,
    WorkSubjectValidationError,
    WorkItemView,
    display_subject_name,
    normalize_subject_name,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS work_subject (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES partner_workspaces(id),
    UNIQUE (workspace_id, normalized_name),
    CHECK (length(trim(name)) > 0),
    CHECK (length(trim(normalized_name)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_work_subject_workspace
    ON work_subject(workspace_id, id DESC);

CREATE TABLE IF NOT EXISTS work_item (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    subject_id INTEGER,
    kind TEXT NOT NULL CHECK (kind IN ('dialog', 'content')),
    lifecycle TEXT NOT NULL DEFAULT 'open'
        CHECK (lifecycle IN ('open', 'done', 'dismissed')),
    loop_state TEXT
        CHECK (loop_state IN ('active_dialog', 'waiting_reply') OR loop_state IS NULL),
    next_step TEXT NOT NULL DEFAULT '',
    due_at TEXT,
    ref_type TEXT CHECK (ref_type IN ('artifact', 'signal') OR ref_type IS NULL),
    ref_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY (workspace_id) REFERENCES partner_workspaces(id),
    FOREIGN KEY (subject_id) REFERENCES work_subject(id),
    CHECK ((ref_type IS NULL) = (ref_id IS NULL)),
    CHECK (lifecycle = 'open' OR resolved_at IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_work_item_open
    ON work_item(workspace_id, lifecycle, due_at);
CREATE INDEX IF NOT EXISTS idx_work_item_subject
    ON work_item(subject_id);
"""

# ref_type уже проверен на принадлежность WORK_ITEM_REF_TYPES до того, как имя
# таблицы попадает в SQL — это не пользовательский ввод, а фиксированный
# маппинг из двух литералов.
_REF_TABLES = {"artifact": "artifacts", "signal": "workspace_signal_interpretations"}


class WorkRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.executescript(_SCHEMA)
            await db.commit()

    # ── WorkSubject ──────────────────────────────────────────────────────

    async def get_or_create_subject(self, workspace_id: int, name: str) -> WorkSubject:
        normalized = normalize_subject_name(name)
        display_name = display_subject_name(name)
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute(
                "INSERT INTO work_subject (workspace_id, name, normalized_name, created_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(workspace_id, normalized_name) DO NOTHING",
                (workspace_id, display_name, normalized, now),
            )
            await db.commit()
            row = await (await db.execute(
                "SELECT * FROM work_subject WHERE workspace_id = ? AND normalized_name = ?",
                (workspace_id, normalized),
            )).fetchone()
        if row is None:
            raise RuntimeError("Не удалось создать или найти субъект")
        return _subject_from_row(row)

    async def get_subject(self, workspace_id: int, subject_id: int) -> WorkSubject | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                "SELECT * FROM work_subject WHERE workspace_id = ? AND id = ?",
                (workspace_id, subject_id),
            )).fetchone()
        return _subject_from_row(row) if row is not None else None

    # ── WorkItem: создание ───────────────────────────────────────────────

    async def create_work_item(
        self,
        workspace_id: int,
        *,
        kind: str,
        subject_id: int | None = None,
        lifecycle: str = "open",
        loop_state: str | None = None,
        next_step: str = "",
        due_at: str | None = None,
        ref_type: str | None = None,
        ref_id: int | None = None,
    ) -> WorkItem:
        """Создаёт work_item. lifecycle по умолчанию 'open', но можно сразу
        создать уже закрытый item (например content, зафиксированный как
        опубликованный задним числом) — тогда resolved_at проставляется
        автоматически, чтобы не нарушить CHECK-инвариант схемы."""
        _validate_kind(kind)
        _validate_lifecycle(lifecycle)
        _validate_loop_state(loop_state)
        _validate_kind_loop_state_pair(kind, lifecycle, loop_state)
        _validate_ref_pair(ref_type, ref_id)

        now = _now()
        resolved_at = now if lifecycle != "open" else None

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                if subject_id is not None:
                    subject_row = await (await db.execute(
                        "SELECT 1 FROM work_subject WHERE workspace_id = ? AND id = ?",
                        (workspace_id, subject_id),
                    )).fetchone()
                    if subject_row is None:
                        raise WorkItemValidationError("Субъект не принадлежит workspace")
                if ref_type is not None:
                    await _assert_ref_belongs_to_workspace(db, workspace_id, ref_type, ref_id)

                cursor = await db.execute(
                    "INSERT INTO work_item (workspace_id, subject_id, kind, lifecycle, "
                    "loop_state, next_step, due_at, ref_type, ref_id, created_at, "
                    "updated_at, resolved_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        workspace_id, subject_id, kind, lifecycle, loop_state, next_step,
                        due_at, ref_type, ref_id, now, now, resolved_at,
                    ),
                )
                row = await self._work_item_row(db, workspace_id, cursor.lastrowid or 0)
                if row is None:
                    raise RuntimeError("Не удалось создать work_item")
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return _work_item_from_row(row)

    async def get_work_item(self, workspace_id: int, work_item_id: int) -> WorkItem | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await self._work_item_row(db, workspace_id, work_item_id)
        return _work_item_from_row(row) if row is not None else None

    # ── WorkItem: чтение для NextBestActionService ──────────────────────

    async def list_open_actionable(
        self, workspace_id: int, *, now: str, limit: int = 50
    ) -> list[WorkItemView]:
        """active_dialog + наступивший waiting_reply + все открытые content —
        ровно explicit-кандидаты, готовые попасть в actions без derived-логики."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT w.*, s.name AS subject_name FROM work_item w "
                "LEFT JOIN work_subject s ON s.id = w.subject_id "
                "WHERE w.workspace_id = ? AND w.lifecycle = 'open' AND ("
                "  (w.kind = 'dialog' AND w.loop_state = 'active_dialog')"
                "  OR (w.kind = 'dialog' AND w.loop_state = 'waiting_reply'"
                "      AND w.due_at IS NOT NULL AND w.due_at <= ?)"
                "  OR (w.kind = 'content')"
                ") ORDER BY w.id DESC LIMIT ?",
                (workspace_id, now, _limit(limit)),
            )).fetchall()
        return [_view_from_row(row) for row in rows]

    async def list_waiting_not_due(
        self, workspace_id: int, *, now: str, limit: int = 50
    ) -> list[WorkItemView]:
        """waiting_reply, чей due_at ещё не наступил (или не задан вовсе) —
        компактный блок «Ждём ответ: …», отдельно от actions."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT w.*, s.name AS subject_name FROM work_item w "
                "LEFT JOIN work_subject s ON s.id = w.subject_id "
                "WHERE w.workspace_id = ? AND w.lifecycle = 'open' "
                "AND w.kind = 'dialog' AND w.loop_state = 'waiting_reply' "
                "AND (w.due_at IS NULL OR w.due_at > ?) "
                "ORDER BY w.due_at IS NULL, w.due_at ASC, w.id DESC LIMIT ?",
                (workspace_id, now, _limit(limit)),
            )).fetchall()
        return [_view_from_row(row) for row in rows]

    async def list_recent_resolved_content(
        self, workspace_id: int, *, since: str, limit: int = 20
    ) -> list[WorkItem]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT * FROM work_item WHERE workspace_id = ? AND kind = 'content' "
                "AND lifecycle = 'done' AND resolved_at >= ? "
                "ORDER BY resolved_at DESC LIMIT ?",
                (workspace_id, since, _limit(limit)),
            )).fetchall()
        return [_work_item_from_row(row) for row in rows]

    # ── WorkItem: переходы состояния ─────────────────────────────────────

    async def mark_reply_received(
        self, workspace_id: int, work_item_id: int, *, next_step: str | None = None
    ) -> WorkItem | None:
        """waiting_reply -> active_dialog, снимает due_at. Возвращает None
        (no-op), если item уже не в waiting_reply или закрыт — вызывающий
        код не должен ронять сценарий на устаревшей ссылке на work_item."""
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await self._work_item_row(db, workspace_id, work_item_id)
                if (
                    row is None
                    or row["lifecycle"] != "open"
                    or row["kind"] != "dialog"
                    or row["loop_state"] != "waiting_reply"
                ):
                    await db.rollback()
                    return None
                updated_next_step = next_step if next_step is not None else row["next_step"]
                await db.execute(
                    "UPDATE work_item SET loop_state = 'active_dialog', due_at = NULL, "
                    "next_step = ?, updated_at = ? WHERE workspace_id = ? AND id = ?",
                    (updated_next_step, now, workspace_id, work_item_id),
                )
                updated = await self._work_item_row(db, workspace_id, work_item_id)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return _work_item_from_row(updated) if updated is not None else None

    async def snooze(
        self, workspace_id: int, work_item_id: int, *, due_at: str
    ) -> WorkItem | None:
        """Переносит due_at открытого dialog work_item. Не меняет loop_state —
        перенос применим и к waiting_reply, и (на будущее) к иным срокам."""
        if not isinstance(due_at, str) or not due_at.strip():
            raise WorkItemValidationError("due_at не должен быть пустым")
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await self._work_item_row(db, workspace_id, work_item_id)
                if row is None or row["lifecycle"] != "open" or row["kind"] != "dialog":
                    await db.rollback()
                    return None
                await db.execute(
                    "UPDATE work_item SET due_at = ?, updated_at = ? "
                    "WHERE workspace_id = ? AND id = ?",
                    (due_at, now, workspace_id, work_item_id),
                )
                updated = await self._work_item_row(db, workspace_id, work_item_id)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return _work_item_from_row(updated) if updated is not None else None

    async def resolve_done(
        self, workspace_id: int, work_item_id: int, *, resolved_at: str | None = None
    ) -> WorkItem | None:
        return await self._resolve(workspace_id, work_item_id, "done", resolved_at)

    async def resolve_dismissed(
        self, workspace_id: int, work_item_id: int, *, resolved_at: str | None = None
    ) -> WorkItem | None:
        return await self._resolve(workspace_id, work_item_id, "dismissed", resolved_at)

    async def _resolve(
        self, workspace_id: int, work_item_id: int, lifecycle: str, resolved_at: str | None
    ) -> WorkItem | None:
        now = resolved_at or _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await self._work_item_row(db, workspace_id, work_item_id)
                if row is None or row["lifecycle"] != "open":
                    await db.rollback()
                    return None
                await db.execute(
                    "UPDATE work_item SET lifecycle = ?, resolved_at = ?, updated_at = ? "
                    "WHERE workspace_id = ? AND id = ?",
                    (lifecycle, now, now, workspace_id, work_item_id),
                )
                updated = await self._work_item_row(db, workspace_id, work_item_id)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return _work_item_from_row(updated) if updated is not None else None

    @staticmethod
    async def _work_item_row(
        db: aiosqlite.Connection, workspace_id: int, work_item_id: int
    ) -> aiosqlite.Row | None:
        cursor = await db.execute(
            "SELECT * FROM work_item WHERE workspace_id = ? AND id = ?",
            (workspace_id, work_item_id),
        )
        return await cursor.fetchone()


def _validate_kind(kind: str) -> None:
    if kind not in WORK_ITEM_KINDS:
        raise WorkItemValidationError(f"Недопустимый kind work_item: {kind!r}")


def _validate_lifecycle(lifecycle: str) -> None:
    if lifecycle not in WORK_ITEM_LIFECYCLES:
        raise WorkItemValidationError(f"Недопустимый lifecycle work_item: {lifecycle!r}")


def _validate_loop_state(loop_state: str | None) -> None:
    if loop_state is not None and loop_state not in WORK_ITEM_LOOP_STATES:
        raise WorkItemValidationError(f"Недопустимый loop_state work_item: {loop_state!r}")


def _validate_kind_loop_state_pair(kind: str, lifecycle: str, loop_state: str | None) -> None:
    if kind == "content" and loop_state is not None:
        raise WorkItemValidationError("content work_item не должен иметь loop_state")
    if kind == "dialog" and lifecycle == "open" and loop_state is None:
        raise WorkItemValidationError(
            "Открытый dialog work_item должен иметь loop_state"
        )


def _validate_ref_pair(ref_type: str | None, ref_id: int | None) -> None:
    if (ref_type is None) != (ref_id is None):
        raise WorkItemValidationError(
            "ref_type и ref_id должны быть заданы вместе или оба отсутствовать"
        )
    if ref_type is not None and ref_type not in WORK_ITEM_REF_TYPES:
        raise WorkItemValidationError(f"Недопустимый ref_type: {ref_type!r}")
    if ref_id is not None and (type(ref_id) is not int or ref_id < 1):
        raise WorkItemValidationError("ref_id должен быть положительным целым числом")


async def _assert_ref_belongs_to_workspace(
    db: aiosqlite.Connection, workspace_id: int, ref_type: str, ref_id: int | None
) -> None:
    table = _REF_TABLES[ref_type]
    row = await (await db.execute(
        f"SELECT 1 FROM {table} WHERE workspace_id = ? AND id = ?",
        (workspace_id, ref_id),
    )).fetchone()
    if row is None:
        raise WorkItemValidationError(f"{ref_type} не принадлежит workspace")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _limit(value: int) -> int:
    if value <= 0:
        raise ValueError("limit должен быть положительным")
    return value


def _subject_from_row(row: aiosqlite.Row) -> WorkSubject:
    return WorkSubject(
        id=row["id"], workspace_id=row["workspace_id"], name=row["name"],
        normalized_name=row["normalized_name"], created_at=row["created_at"],
    )


def _work_item_from_row(row: aiosqlite.Row) -> WorkItem:
    return WorkItem(
        id=row["id"], workspace_id=row["workspace_id"], subject_id=row["subject_id"],
        kind=row["kind"], lifecycle=row["lifecycle"], loop_state=row["loop_state"],
        next_step=row["next_step"], due_at=row["due_at"], ref_type=row["ref_type"],
        ref_id=row["ref_id"], created_at=row["created_at"], updated_at=row["updated_at"],
        resolved_at=row["resolved_at"],
    )


def _view_from_row(row: aiosqlite.Row) -> WorkItemView:
    return WorkItemView(item=_work_item_from_row(row), subject_name=row["subject_name"])
