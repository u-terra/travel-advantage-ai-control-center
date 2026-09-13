"""One-time Telegram-connect deep-link tokens (see app.web_api's POST
/api/telegram/bind-token and app.handlers.start's /start <token> handling).

Same conventions and the same security shape as
app.repositories.web_auth_repository's invites: only a SHA-256 hash of the
raw token is ever stored (app.services.web_auth_tokens), never the raw
value; consume_token() atomically validates (exists, not expired, not
already used) and marks the token used in one transaction, so two
concurrent /start attempts with the same token can never both succeed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS telegram_bind_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    used_by_telegram_user_id INTEGER,
    FOREIGN KEY (workspace_id) REFERENCES partner_workspaces(id)
);
CREATE INDEX IF NOT EXISTS idx_telegram_bind_tokens_hash
    ON telegram_bind_tokens(token_hash);
"""


@dataclass(frozen=True)
class TelegramBindToken:
    id: int
    workspace_id: int
    token_hash: str
    created_at: str
    expires_at: str
    used_at: str | None
    used_by_telegram_user_id: int | None


class TelegramBindTokenRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.executescript(_SCHEMA)
            await db.commit()

    async def create_token(
        self, workspace_id: int, token_hash: str, expires_at: str,
    ) -> TelegramBindToken:
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            cursor = await db.execute(
                "INSERT INTO telegram_bind_tokens "
                "(workspace_id, token_hash, created_at, expires_at, used_at, "
                "used_by_telegram_user_id) "
                "VALUES (?, ?, ?, ?, NULL, NULL)",
                (workspace_id, token_hash, now, expires_at),
            )
            await db.commit()
            row = await self._row_by_id(db, cursor.lastrowid or 0)
        if row is None:
            raise RuntimeError("Не удалось создать токен привязки Telegram")
        return _from_row(row)

    async def get_by_token_hash(self, token_hash: str) -> TelegramBindToken | None:
        """Read-only peek, mirrors WebAuthRepository.get_invite_by_token_hash -
        callers validate expiry/used_at with this first so a caller can
        show a clear error without burning a still-valid token by racing
        into consume_token()."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await self._row_by_hash(db, token_hash)
        return _from_row(row) if row is not None else None

    async def consume_token(
        self, token_hash: str, telegram_user_id: int,
    ) -> TelegramBindToken | None:
        """Atomically validates (exists, not expired, not already used) and
        marks the token used in one transaction. Returns None on any
        failure (unknown/expired/already-used token) - the caller can't
        distinguish which, by design, same as
        WebAuthRepository.consume_invite."""
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await self._row_by_hash(db, token_hash)
                if row is None or row["used_at"] is not None or row["expires_at"] <= now:
                    await db.rollback()
                    return None
                cursor = await db.execute(
                    "UPDATE telegram_bind_tokens SET used_at = ?, "
                    "used_by_telegram_user_id = ? "
                    "WHERE id = ? AND used_at IS NULL",
                    (now, telegram_user_id, row["id"]),
                )
                if cursor.rowcount == 0:
                    await db.rollback()
                    return None
                updated_row = await self._row_by_id(db, row["id"])
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return _from_row(updated_row) if updated_row is not None else None

    @staticmethod
    async def _row_by_id(db: aiosqlite.Connection, token_id: int):
        cursor = await db.execute(
            "SELECT * FROM telegram_bind_tokens WHERE id = ?", (token_id,),
        )
        return await cursor.fetchone()

    @staticmethod
    async def _row_by_hash(db: aiosqlite.Connection, token_hash: str):
        cursor = await db.execute(
            "SELECT * FROM telegram_bind_tokens WHERE token_hash = ?", (token_hash,),
        )
        return await cursor.fetchone()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _from_row(row: aiosqlite.Row) -> TelegramBindToken:
    return TelegramBindToken(
        id=row["id"],
        workspace_id=row["workspace_id"],
        token_hash=row["token_hash"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        used_at=row["used_at"],
        used_by_telegram_user_id=row["used_by_telegram_user_id"],
    )
