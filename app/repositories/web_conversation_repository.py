"""Persisted history for the web Ассистент - additive schema in the shared
journal DB, same conventions as every other repository here
(ArtifactRepository, CompetitorRepository, WorkspaceMemoryRepository):
plain aiosqlite, ``CREATE TABLE IF NOT EXISTS`` only (never destructive),
workspace isolation enforced by including workspace_id in every WHERE
clause.

Two tables:

- web_conversations: one row per conversation. ``telegram_user_id`` is the
  same "user identity" column every other per-user table in this codebase
  already uses (workspace_user_preferences, conversation_state,
  usage_events) - not a new identity concept, and structurally ready for
  real web-auth the same way those tables already are (see web_api.py's
  WEB_TELEGRAM_USER_ID docstring: "Temporary until web authentication is
  implemented"). Every read/write here is scoped by BOTH workspace_id AND
  telegram_user_id - a conversation is private to the user who owns it,
  not shared workspace-wide like Materials/Competitors.
- web_conversation_messages: one row per turn. No workspace_id/
  telegram_user_id of its own (normalized - ownership flows through the
  parent conversation); every access verifies the parent row's
  (workspace_id, telegram_user_id) first, in the same transaction as any
  write, so a foreign conversation_id can never be read or written to.

Only plain-text content is stored - never rendered HTML, never
knowledge/business-profile/workspace-memory context, never a system
prompt. See app.domain.web_conversation for the "why" in more detail.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from app.domain.web_conversation import (
    MESSAGE_ROLES,
    WebConversation,
    WebConversationMessage,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS web_conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    telegram_user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    activity_seq INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES partner_workspaces(id),
    CHECK (length(trim(title)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_web_conversations_scope
    ON web_conversations(workspace_id, telegram_user_id, activity_seq DESC);

CREATE TABLE IF NOT EXISTS web_conversation_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES web_conversations(id),
    CHECK (length(trim(content)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_web_conversation_messages_scope
    ON web_conversation_messages(conversation_id, id ASC);
"""

_DEFAULT_TITLE = "Новый диалог"
_MAX_TITLE_LEN = 80
_MAX_TITLE_INPUT_LEN = 4000  # guards against pathologically long first messages


def derive_conversation_title(first_message: str) -> str:
    """No LLM call - just the first user message, whitespace-collapsed and
    truncated at a word boundary (never cuts a word in half) with an
    ellipsis marking the cut - same "careful" truncation philosophy as
    truncateTakeawayAtBoundary() in app/templates/chat.html."""
    normalized = " ".join((first_message or "")[:_MAX_TITLE_INPUT_LEN].split())
    if not normalized:
        return _DEFAULT_TITLE
    if len(normalized) <= _MAX_TITLE_LEN:
        return normalized

    truncated = normalized[:_MAX_TITLE_LEN]
    last_space = truncated.rfind(" ")
    if last_space > 20:
        truncated = truncated[:last_space]
    return truncated.rstrip(" .,;:!?") + "…"


class WebConversationRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.executescript(_SCHEMA)
            await db.commit()

    async def count_conversations_created_since(self, since_iso: str) -> int:
        """Beta Control Center dashboard only (app/admin_api.py) - global
        (cross-tenant) count, unlike every other read here."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM web_conversations WHERE created_at >= ?",
                (since_iso,),
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def count_messages_created_since(
        self, since_iso: str, *, role: str | None = None,
    ) -> int:
        """Beta Control Center dashboard only - global count."""
        clause = "created_at >= ?"
        params: list[object] = [since_iso]
        if role is not None:
            clause += " AND role = ?"
            params.append(role)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                f"SELECT COUNT(*) FROM web_conversation_messages WHERE {clause}",
                params,
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def create_conversation(
        self, workspace_id: int, telegram_user_id: int, *, title: str = _DEFAULT_TITLE,
    ) -> WebConversation:
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "INSERT INTO web_conversations "
                    "(workspace_id, telegram_user_id, title, activity_seq, created_at, updated_at) "
                    "VALUES (?, ?, ?, (SELECT COALESCE(MAX(activity_seq), 0) + 1 FROM web_conversations), ?, ?)",
                    (workspace_id, telegram_user_id, title.strip() or _DEFAULT_TITLE, now, now),
                )
                row = await self._conversation_row(
                    db, workspace_id, telegram_user_id, cursor.lastrowid or 0,
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        if row is None:
            raise RuntimeError("Не удалось создать диалог")
        return _conversation_from_row(row)

    async def list_conversations(
        self, workspace_id: int, telegram_user_id: int, *, limit: int = 50,
    ) -> list[WebConversation]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM web_conversations "
                "WHERE workspace_id = ? AND telegram_user_id = ? "
                "ORDER BY activity_seq DESC LIMIT ?",
                (workspace_id, telegram_user_id, _limit(limit)),
            )
            rows = await cursor.fetchall()
        return [_conversation_from_row(row) for row in rows]

    async def get_conversation(
        self, workspace_id: int, telegram_user_id: int, conversation_id: int,
    ) -> WebConversation | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await self._conversation_row(
                db, workspace_id, telegram_user_id, conversation_id,
            )
        return _conversation_from_row(row) if row is not None else None

    async def set_conversation_title(
        self, workspace_id: int, telegram_user_id: int, conversation_id: int, title: str,
    ) -> WebConversation | None:
        cleaned = title.strip() or _DEFAULT_TITLE
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                "UPDATE web_conversations SET title = ? "
                "WHERE workspace_id = ? AND telegram_user_id = ? AND id = ?",
                (cleaned, workspace_id, telegram_user_id, conversation_id),
            )
            await db.commit()
            row = await self._conversation_row(
                db, workspace_id, telegram_user_id, conversation_id,
            )
        return _conversation_from_row(row) if row is not None else None

    async def list_messages(
        self, workspace_id: int, telegram_user_id: int, conversation_id: int, *, limit: int = 200,
    ) -> list[WebConversationMessage]:
        """Chronological (oldest first). Returns an empty list - not an
        error - when the conversation does not exist or belongs to another
        workspace/user, same fail-closed convention as the rest of this
        repository's read methods."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            owner_row = await self._conversation_row(
                db, workspace_id, telegram_user_id, conversation_id,
            )
            if owner_row is None:
                return []
            cursor = await db.execute(
                "SELECT * FROM web_conversation_messages "
                "WHERE conversation_id = ? ORDER BY id ASC LIMIT ?",
                (conversation_id, _limit(limit)),
            )
            rows = await cursor.fetchall()
        return [_message_from_row(row) for row in rows]

    async def add_message(
        self, workspace_id: int, telegram_user_id: int, conversation_id: int,
        role: str, content: str,
    ) -> WebConversationMessage | None:
        """Appends one turn and bumps the conversation's updated_at (so the
        История list orders by last activity). Returns None - never
        raises - if the conversation is not owned by this workspace/user,
        the role is invalid, or content is blank; the caller (web_api.py)
        treats None as "nothing was persisted", exactly like
        ArtifactRepository.add_artifact_version_if_current."""
        if role not in MESSAGE_ROLES:
            return None
        cleaned = content.strip()
        if not cleaned:
            return None

        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                owner_row = await self._conversation_row(
                    db, workspace_id, telegram_user_id, conversation_id,
                )
                if owner_row is None:
                    await db.rollback()
                    return None
                cursor = await db.execute(
                    "INSERT INTO web_conversation_messages "
                    "(conversation_id, role, content, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (conversation_id, role, cleaned, now),
                )
                message_id = cursor.lastrowid or 0
                await db.execute(
                    "UPDATE web_conversations SET updated_at = ?, "
                    "activity_seq = (SELECT COALESCE(MAX(activity_seq), 0) + 1 FROM web_conversations) "
                    "WHERE workspace_id = ? AND telegram_user_id = ? AND id = ?",
                    (now, workspace_id, telegram_user_id, conversation_id),
                )
                row = await self._message_row(db, message_id)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return _message_from_row(row) if row is not None else None

    async def delete_conversation(
        self, workspace_id: int, telegram_user_id: int, conversation_id: int,
    ) -> bool:
        """Permanently deletes one conversation and all of its messages.

        Ownership is enforced the same way as everywhere else in this
        repository - the DELETE's own WHERE clause requires
        (workspace_id, telegram_user_id, id) to match, so a foreign or
        already-deleted conversation_id simply deletes nothing and this
        returns False (fail closed, no separate authorization check
        needed). Messages are deleted first (no ON DELETE CASCADE on
        conversation_id in the schema), inside one transaction, so a
        deleted conversation's messages can never be read back by
        list_messages()/the /api/chat context builder afterward.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                owner_row = await self._conversation_row(
                    db, workspace_id, telegram_user_id, conversation_id,
                )
                if owner_row is None:
                    await db.rollback()
                    return False
                await db.execute(
                    "DELETE FROM web_conversation_messages WHERE conversation_id = ?",
                    (conversation_id,),
                )
                await db.execute(
                    "DELETE FROM web_conversations "
                    "WHERE workspace_id = ? AND telegram_user_id = ? AND id = ?",
                    (workspace_id, telegram_user_id, conversation_id),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return True

    async def delete_conversations(
        self, workspace_id: int, telegram_user_id: int, conversation_ids: list[int],
    ) -> int:
        """Bulk delete - same per-row ownership check as delete_conversation,
        just looped inside one transaction. Returns how many of the
        requested ids actually belonged to this workspace/user and were
        deleted; ids that don't (foreign, already gone, bad id) are simply
        skipped, never raise."""
        if not conversation_ids:
            return 0
        deleted = 0
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                for conversation_id in conversation_ids:
                    owner_row = await self._conversation_row(
                        db, workspace_id, telegram_user_id, conversation_id,
                    )
                    if owner_row is None:
                        continue
                    await db.execute(
                        "DELETE FROM web_conversation_messages WHERE conversation_id = ?",
                        (conversation_id,),
                    )
                    await db.execute(
                        "DELETE FROM web_conversations "
                        "WHERE workspace_id = ? AND telegram_user_id = ? AND id = ?",
                        (workspace_id, telegram_user_id, conversation_id),
                    )
                    deleted += 1
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return deleted

    async def clear_conversations(
        self, workspace_id: int, telegram_user_id: int,
    ) -> int:
        """Deletes every conversation (and its messages) owned by this
        workspace/user - the "очистить всю историю" action. Scoped to
        (workspace_id, telegram_user_id) exactly like every other method
        here, so this can never touch another user's or workspace's
        conversations."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                ids_rows = await (await db.execute(
                    "SELECT id FROM web_conversations "
                    "WHERE workspace_id = ? AND telegram_user_id = ?",
                    (workspace_id, telegram_user_id),
                )).fetchall()
                ids = [row["id"] for row in ids_rows]
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    await db.execute(
                        f"DELETE FROM web_conversation_messages "
                        f"WHERE conversation_id IN ({placeholders})",
                        ids,
                    )
                    await db.execute(
                        "DELETE FROM web_conversations "
                        "WHERE workspace_id = ? AND telegram_user_id = ?",
                        (workspace_id, telegram_user_id),
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return len(ids)

    @staticmethod
    async def _conversation_row(
        db: aiosqlite.Connection, workspace_id: int, telegram_user_id: int, conversation_id: int,
    ) -> aiosqlite.Row | None:
        cursor = await db.execute(
            "SELECT * FROM web_conversations "
            "WHERE workspace_id = ? AND telegram_user_id = ? AND id = ?",
            (workspace_id, telegram_user_id, conversation_id),
        )
        return await cursor.fetchone()

    @staticmethod
    async def _message_row(
        db: aiosqlite.Connection, message_id: int,
    ) -> aiosqlite.Row | None:
        cursor = await db.execute(
            "SELECT * FROM web_conversation_messages WHERE id = ?",
            (message_id,),
        )
        return await cursor.fetchone()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _limit(value: int) -> int:
    if value <= 0:
        raise ValueError("limit должен быть положительным")
    return value


def _conversation_from_row(row: aiosqlite.Row) -> WebConversation:
    return WebConversation(
        id=row["id"],
        workspace_id=row["workspace_id"],
        telegram_user_id=row["telegram_user_id"],
        title=row["title"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _message_from_row(row: aiosqlite.Row) -> WebConversationMessage:
    return WebConversationMessage(
        id=row["id"],
        conversation_id=row["conversation_id"],
        role=row["role"],
        content=row["content"],
        created_at=row["created_at"],
    )
