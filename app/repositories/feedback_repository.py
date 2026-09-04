"""assistant_feedback - see app/domain/feedback.py. One row per
(web_user_id, message_id): resubmitting (changing 👍 to 👎, or editing the
comment) upserts the same row rather than accumulating duplicates.

Never stores the message/conversation content itself - conversation_id/
message_id are references only, resolved back through
WebConversationRepository (workspace/user-scoped) when actually needed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from app.domain.feedback import AssistantFeedback, FeedbackRating, FeedbackStatus

_MAX_COMMENT_CHARS = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS assistant_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    web_user_id INTEGER NOT NULL,
    conversation_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    rating TEXT NOT NULL CHECK (rating IN ('up', 'down')),
    reason TEXT,
    comment TEXT,
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'reviewed', 'resolved')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (web_user_id, message_id),
    FOREIGN KEY (workspace_id) REFERENCES partner_workspaces(id)
);
CREATE INDEX IF NOT EXISTS idx_assistant_feedback_workspace
    ON assistant_feedback(workspace_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_assistant_feedback_status
    ON assistant_feedback(status, id DESC);
"""


class FeedbackRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.executescript(_SCHEMA)
            await db.commit()

    async def submit(
        self, *, workspace_id: int, web_user_id: int, conversation_id: int,
        message_id: int, rating: FeedbackRating, reason: str | None = None,
        comment: str | None = None,
    ) -> AssistantFeedback:
        now = _now()
        safe_comment = (comment or "").strip()[:_MAX_COMMENT_CHARS] or None
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            cursor = await db.execute(
                "INSERT INTO assistant_feedback "
                "(workspace_id, web_user_id, conversation_id, message_id, rating, "
                "reason, comment, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?, ?) "
                "ON CONFLICT(web_user_id, message_id) DO UPDATE SET "
                "rating=excluded.rating, reason=excluded.reason, "
                "comment=excluded.comment, updated_at=excluded.updated_at",
                (workspace_id, web_user_id, conversation_id, message_id, rating.value,
                 reason, safe_comment, now, now),
            )
            await db.commit()
            if cursor.lastrowid:
                row = await self._row_by_id(db, cursor.lastrowid)
            else:
                row = await self._row_by_user_message(db, web_user_id, message_id)
        if row is None:
            raise RuntimeError("Не удалось сохранить feedback")
        return _from_row(row)

    async def count_since(self, since_iso: str, *, rating: str | None = None) -> int:
        """Beta Control Center dashboard only (app/admin_api.py) - global
        (cross-tenant) count."""
        clause = "created_at >= ?"
        params: list[object] = [since_iso]
        if rating is not None:
            clause += " AND rating = ?"
            params.append(rating)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                f"SELECT COUNT(*) FROM assistant_feedback WHERE {clause}", params,
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def list_recent(
        self, *, status: str | None = None, workspace_id: int | None = None,
        limit: int = 100,
    ) -> list[AssistantFeedback]:
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if workspace_id is not None:
            clauses.append("workspace_id = ?")
            params.append(workspace_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"SELECT * FROM assistant_feedback {where} ORDER BY id DESC LIMIT ?",
                (*params, limit),
            )
            rows = await cursor.fetchall()
        return [_from_row(row) for row in rows]

    async def get(self, feedback_id: int) -> AssistantFeedback | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await self._row_by_id(db, feedback_id)
        return _from_row(row) if row is not None else None

    async def set_status(self, feedback_id: int, status: FeedbackStatus) -> AssistantFeedback | None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE assistant_feedback SET status=?, updated_at=? WHERE id=?",
                (status.value, _now(), feedback_id),
            )
            await db.commit()
            db.row_factory = aiosqlite.Row
            row = await self._row_by_id(db, feedback_id)
        return _from_row(row) if row is not None else None

    @staticmethod
    async def _row_by_id(db: aiosqlite.Connection, feedback_id: int):
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM assistant_feedback WHERE id = ?", (feedback_id,),
        )
        return await cursor.fetchone()

    @staticmethod
    async def _row_by_user_message(db: aiosqlite.Connection, web_user_id: int, message_id: int):
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM assistant_feedback WHERE web_user_id = ? AND message_id = ?",
            (web_user_id, message_id),
        )
        return await cursor.fetchone()


def _from_row(row: aiosqlite.Row) -> AssistantFeedback:
    return AssistantFeedback(
        id=row["id"], workspace_id=row["workspace_id"], web_user_id=row["web_user_id"],
        conversation_id=row["conversation_id"], message_id=row["message_id"],
        rating=FeedbackRating(row["rating"]), reason=row["reason"], comment=row["comment"],
        status=FeedbackStatus(row["status"]), created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
