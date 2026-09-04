"""Metadata for files attached to web-Ассистент messages - additive table
in the same shared journal DB, same conventions as WebConversationRepository
(plain aiosqlite, ``CREATE TABLE IF NOT EXISTS`` only, workspace_id +
telegram_user_id in every WHERE clause). No parallel conversation model:
every row references an existing web_conversations row, and (once sent)
an existing web_conversation_messages row.

``public_id`` (a high-entropy random token, not the autoincrement ``id``)
is the only identifier ever handed to the browser - see app.web_api's
content-serving endpoint. Combined with the ownership check every read
here performs, a foreign workspace/user can neither guess nor read
another tenant's attachment.

Lifecycle: create_pending() (right after upload, message_id NULL) ->
attach_to_message() (once the user actually sends the message) -> read
via list_for_conversation_messages()/get_for_workspace() thereafter.
delete_orphans_older_than() reaps attachments that were uploaded but never
attached to a message (user changed their mind, or the tab was closed) -
see app.web_api's startup reaper. delete_for_conversation() is the hook a
future "delete conversation" feature would call; no such feature exists
yet in this codebase (see app.web_api), so nothing calls it today.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from app.domain.web_attachment import WebAttachment

_SCHEMA = """
CREATE TABLE IF NOT EXISTS web_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id TEXT NOT NULL,
    workspace_id INTEGER NOT NULL,
    telegram_user_id INTEGER NOT NULL,
    conversation_id INTEGER NOT NULL,
    message_id INTEGER,
    original_filename TEXT NOT NULL,
    stored_filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('image', 'pdf', 'text')),
    size_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES partner_workspaces(id),
    FOREIGN KEY (conversation_id) REFERENCES web_conversations(id),
    FOREIGN KEY (message_id) REFERENCES web_conversation_messages(id),
    CHECK (length(trim(original_filename)) > 0),
    CHECK (length(trim(stored_filename)) > 0),
    CHECK (size_bytes >= 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_web_attachments_public_id
    ON web_attachments(public_id);

CREATE INDEX IF NOT EXISTS idx_web_attachments_conversation
    ON web_attachments(conversation_id, id ASC);

CREATE INDEX IF NOT EXISTS idx_web_attachments_message
    ON web_attachments(message_id);

CREATE INDEX IF NOT EXISTS idx_web_attachments_pending
    ON web_attachments(message_id, created_at);
"""

_PUBLIC_ID_ATTEMPTS = 5


class WebAttachmentRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.executescript(_SCHEMA)
            await db.commit()

    async def count_uploads_since(self, since_iso: str) -> int:
        """Beta Control Center dashboard only (app/admin_api.py) - global
        (cross-tenant) count, unlike every other read here."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM web_attachments WHERE created_at >= ?",
                (since_iso,),
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def create_pending(
        self, *, workspace_id: int, telegram_user_id: int, conversation_id: int,
        original_filename: str, stored_filename: str, content_type: str,
        kind: str, size_bytes: int,
    ) -> WebAttachment:
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")

            last_error: Exception | None = None
            for _ in range(_PUBLIC_ID_ATTEMPTS):
                public_id = secrets.token_urlsafe(24)
                try:
                    cursor = await db.execute(
                        "INSERT INTO web_attachments "
                        "(public_id, workspace_id, telegram_user_id, conversation_id, "
                        "message_id, original_filename, stored_filename, content_type, "
                        "kind, size_bytes, created_at) "
                        "VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)",
                        (
                            public_id, workspace_id, telegram_user_id, conversation_id,
                            original_filename, stored_filename, content_type,
                            kind, size_bytes, now,
                        ),
                    )
                    await db.commit()
                    row = await self._row_by_id(db, cursor.lastrowid or 0)
                    break
                except aiosqlite.IntegrityError as exc:
                    last_error = exc
                    continue
            else:
                raise RuntimeError("Не удалось сохранить вложение") from last_error

        if row is None:
            raise RuntimeError("Не удалось сохранить вложение")
        return _from_row(row)

    async def get_pending_for_conversation(
        self, workspace_id: int, telegram_user_id: int, conversation_id: int, public_id: str,
    ) -> WebAttachment | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM web_attachments WHERE workspace_id = ? "
                "AND telegram_user_id = ? AND conversation_id = ? AND public_id = ? "
                "AND message_id IS NULL",
                (workspace_id, telegram_user_id, conversation_id, public_id),
            )
            row = await cursor.fetchone()
        return _from_row(row) if row is not None else None

    async def get_for_workspace(
        self, workspace_id: int, telegram_user_id: int, public_id: str,
    ) -> WebAttachment | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM web_attachments WHERE workspace_id = ? "
                "AND telegram_user_id = ? AND public_id = ?",
                (workspace_id, telegram_user_id, public_id),
            )
            row = await cursor.fetchone()
        return _from_row(row) if row is not None else None

    async def attach_to_message(
        self, workspace_id: int, telegram_user_id: int, conversation_id: int,
        public_ids: list[str], message_id: int,
    ) -> list[WebAttachment]:
        """Binds already-uploaded pending attachments to the message that
        was just persisted for them. Scoped by workspace_id/
        telegram_user_id/conversation_id/message_id IS NULL, same as
        get_pending_for_conversation() - a public_id that doesn't match all
        of those (foreign, already attached, wrong conversation) is simply
        not updated, never silently reattached from elsewhere."""
        if not public_ids:
            return []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            placeholders = ",".join("?" for _ in public_ids)
            await db.execute(
                f"UPDATE web_attachments SET message_id = ? "
                f"WHERE workspace_id = ? AND telegram_user_id = ? "
                f"AND conversation_id = ? AND message_id IS NULL "
                f"AND public_id IN ({placeholders})",
                (message_id, workspace_id, telegram_user_id, conversation_id, *public_ids),
            )
            await db.commit()
            cursor = await db.execute(
                "SELECT * FROM web_attachments WHERE message_id = ? ORDER BY id ASC",
                (message_id,),
            )
            rows = await cursor.fetchall()
        return [_from_row(row) for row in rows]

    async def list_for_conversation_messages(
        self, workspace_id: int, telegram_user_id: int, conversation_id: int,
    ) -> dict[int, list[WebAttachment]]:
        """message_id -> attachments, for every already-sent message in
        this conversation. Caller (GET /api/conversations/{id}/messages)
        has already verified the conversation itself belongs to this
        workspace/user; the workspace_id/telegram_user_id filter here is
        defense in depth, not the only check."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM web_attachments WHERE workspace_id = ? "
                "AND telegram_user_id = ? AND conversation_id = ? "
                "AND message_id IS NOT NULL ORDER BY id ASC",
                (workspace_id, telegram_user_id, conversation_id),
            )
            rows = await cursor.fetchall()

        grouped: dict[int, list[WebAttachment]] = {}
        for row in rows:
            attachment = _from_row(row)
            grouped.setdefault(attachment.message_id, []).append(attachment)
        return grouped

    async def delete_pending(
        self, workspace_id: int, telegram_user_id: int, public_id: str,
    ) -> WebAttachment | None:
        """Used by the composer's "remove chip" action - only ever deletes
        a still-pending (not yet sent) attachment; an already-attached one
        is part of message history and this deliberately can't touch it."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await self._pending_row(db, workspace_id, telegram_user_id, public_id)
            if row is None:
                return None
            await db.execute(
                "DELETE FROM web_attachments WHERE id = ?", (row["id"],),
            )
            await db.commit()
        return _from_row(row)

    async def delete_orphans_older_than(self, cutoff_iso: str) -> list[WebAttachment]:
        """Rows still pending (never attached to a sent message) older
        than cutoff_iso - abandoned uploads from a composer that was never
        submitted. Returns the deleted rows so the caller can remove the
        matching physical files too (see app.web_api's startup reaper)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM web_attachments WHERE message_id IS NULL "
                "AND created_at < ?",
                (cutoff_iso,),
            )
            rows = await cursor.fetchall()
            if rows:
                ids = [row["id"] for row in rows]
                placeholders = ",".join("?" for _ in ids)
                await db.execute(
                    f"DELETE FROM web_attachments WHERE id IN ({placeholders})", ids,
                )
                await db.commit()
        return [_from_row(row) for row in rows]

    async def delete_for_conversation(
        self, workspace_id: int, telegram_user_id: int, conversation_id: int,
    ) -> list[WebAttachment]:
        """Ready-to-use hook for a future "delete conversation" feature -
        no such endpoint exists yet in this codebase, so nothing calls
        this today. Returns the deleted rows so the caller can remove the
        matching physical files."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM web_attachments WHERE workspace_id = ? "
                "AND telegram_user_id = ? AND conversation_id = ?",
                (workspace_id, telegram_user_id, conversation_id),
            )
            rows = await cursor.fetchall()
            if rows:
                await db.execute(
                    "DELETE FROM web_attachments WHERE workspace_id = ? "
                    "AND telegram_user_id = ? AND conversation_id = ?",
                    (workspace_id, telegram_user_id, conversation_id),
                )
                await db.commit()
        return [_from_row(row) for row in rows]

    @staticmethod
    async def _row_by_id(db: aiosqlite.Connection, attachment_id: int) -> aiosqlite.Row | None:
        cursor = await db.execute(
            "SELECT * FROM web_attachments WHERE id = ?", (attachment_id,),
        )
        return await cursor.fetchone()

    @staticmethod
    async def _pending_row(
        db: aiosqlite.Connection, workspace_id: int, telegram_user_id: int, public_id: str,
    ) -> aiosqlite.Row | None:
        cursor = await db.execute(
            "SELECT * FROM web_attachments WHERE workspace_id = ? "
            "AND telegram_user_id = ? AND public_id = ? AND message_id IS NULL",
            (workspace_id, telegram_user_id, public_id),
        )
        return await cursor.fetchone()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _from_row(row: aiosqlite.Row) -> WebAttachment:
    return WebAttachment(
        id=row["id"],
        public_id=row["public_id"],
        workspace_id=row["workspace_id"],
        telegram_user_id=row["telegram_user_id"],
        conversation_id=row["conversation_id"],
        message_id=row["message_id"],
        original_filename=row["original_filename"],
        stored_filename=row["stored_filename"],
        content_type=row["content_type"],
        kind=row["kind"],
        size_bytes=row["size_bytes"],
        created_at=row["created_at"],
    )
