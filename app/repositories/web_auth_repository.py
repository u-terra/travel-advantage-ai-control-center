"""Web-auth accounts, workspace bindings, server-side sessions, and beta
invites - additive schema in the shared journal DB, same conventions as
every other repository here: plain aiosqlite, ``CREATE TABLE IF NOT
EXISTS`` only, workspace/user isolation via WHERE-clause scoping.

See app.domain.web_auth for why this is a *compatibility layer* over
PartnerRepository's existing access model, not a second business model.

Nothing here ever stores a raw session/CSRF/invite token or a plaintext
password - see app.services.web_auth_tokens / app.services.web_auth_passwords
for the hashing primitives callers are expected to use before calling in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from app.domain.web_auth import (
    WebAuthBinding,
    WebAuthInvite,
    WebAuthUser,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS web_auth_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS web_auth_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    web_user_id INTEGER NOT NULL,
    workspace_id INTEGER NOT NULL,
    telegram_user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    onboarding_completed_at TEXT,
    FOREIGN KEY (web_user_id) REFERENCES web_auth_users(id),
    FOREIGN KEY (workspace_id) REFERENCES partner_workspaces(id),
    UNIQUE (web_user_id, workspace_id, telegram_user_id)
);

CREATE INDEX IF NOT EXISTS idx_web_auth_bindings_user
    ON web_auth_bindings(web_user_id, id ASC);

CREATE TABLE IF NOT EXISTS web_auth_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    web_user_id INTEGER NOT NULL,
    binding_id INTEGER NOT NULL,
    session_token_hash TEXT NOT NULL UNIQUE,
    csrf_token_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    last_seen_at TEXT,
    FOREIGN KEY (web_user_id) REFERENCES web_auth_users(id),
    FOREIGN KEY (binding_id) REFERENCES web_auth_bindings(id)
);

CREATE INDEX IF NOT EXISTS idx_web_auth_sessions_token
    ON web_auth_sessions(session_token_hash);

CREATE TABLE IF NOT EXISTS web_auth_invites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    telegram_user_id INTEGER NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    email_restriction TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    FOREIGN KEY (workspace_id) REFERENCES partner_workspaces(id)
);

CREATE INDEX IF NOT EXISTS idx_web_auth_invites_token
    ON web_auth_invites(token_hash);
"""


class EmailAlreadyRegisteredError(RuntimeError):
    """UNIQUE(email) would be violated."""


@dataclass(frozen=True)
class SessionContext:
    """A session row joined with its binding + user - everything
    get_current_principal() needs in one query, on every authenticated
    request."""
    session_id: int
    web_user_id: int
    email: str
    user_status: str
    binding_id: int
    workspace_id: int
    telegram_user_id: int
    csrf_token_hash: str
    expires_at: str
    revoked_at: str | None


class WebAuthRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.executescript(_SCHEMA)
            await self._migrate_onboarding_column(db)
            await db.commit()

    @staticmethod
    async def _migrate_onboarding_column(db: aiosqlite.Connection) -> None:
        """Additive migration for bindings created before the onboarding
        flag existed. CREATE TABLE IF NOT EXISTS in _SCHEMA is a no-op
        against an already-existing production table, so a pre-existing
        web_auth_bindings table won't pick up the new column on its own.

        Grandfathers in every binding that existed at migration time as
        already onboarded (onboarding_completed_at = now) - a production
        user must never be dropped into a first-run onboarding form just
        because we deployed this column. Only runs the ALTER/backfill once:
        on every later startup the column already exists and this is a
        no-op. Bindings created after this point (create_binding()) don't
        set the column, so they correctly start out NULL/incomplete.
        """
        cursor = await db.execute("PRAGMA table_info(web_auth_bindings)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "onboarding_completed_at" in columns:
            return
        await db.execute(
            "ALTER TABLE web_auth_bindings ADD COLUMN onboarding_completed_at TEXT"
        )
        await db.execute(
            "UPDATE web_auth_bindings SET onboarding_completed_at = ? "
            "WHERE onboarding_completed_at IS NULL",
            (_now(),),
        )

    # ── users ────────────────────────────────────────────────────────

    async def create_user(self, email: str, password_hash: str) -> WebAuthUser:
        normalized = _normalize_email(email)
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            try:
                cursor = await db.execute(
                    "INSERT INTO web_auth_users "
                    "(email, password_hash, status, created_at, last_login_at) "
                    "VALUES (?, ?, 'active', ?, NULL)",
                    (normalized, password_hash, now),
                )
                await db.commit()
            except aiosqlite.IntegrityError as exc:
                raise EmailAlreadyRegisteredError(
                    "Этот email уже зарегистрирован."
                ) from exc
            row = await self._user_row_by_id(db, cursor.lastrowid or 0)
        if row is None:
            raise RuntimeError("Не удалось создать web-аккаунт")
        return _user_from_row(row)

    async def get_user_by_email(self, email: str) -> WebAuthUser | None:
        normalized = _normalize_email(email)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM web_auth_users WHERE email = ?", (normalized,),
            )
            row = await cursor.fetchone()
        return _user_from_row(row) if row is not None else None

    async def get_user_by_id(self, user_id: int) -> WebAuthUser | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await self._user_row_by_id(db, user_id)
        return _user_from_row(row) if row is not None else None

    async def count_users_created_since(self, since_iso: str) -> int:
        """Beta Control Center dashboard only (app/admin_api.py) - global
        (cross-tenant) count, unlike every other read here."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM web_auth_users WHERE created_at >= ?",
                (since_iso,),
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def touch_last_login(self, user_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE web_auth_users SET last_login_at = ? WHERE id = ?",
                (_now(), user_id),
            )
            await db.commit()

    # ── bindings ─────────────────────────────────────────────────────

    async def create_binding(
        self, web_user_id: int, workspace_id: int, telegram_user_id: int,
    ) -> WebAuthBinding:
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            cursor = await db.execute(
                "INSERT OR IGNORE INTO web_auth_bindings "
                "(web_user_id, workspace_id, telegram_user_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (web_user_id, workspace_id, telegram_user_id, now),
            )
            await db.commit()
            if cursor.lastrowid:
                row = await self._binding_row_by_id(db, cursor.lastrowid)
            else:
                row = await self._binding_row(
                    db, web_user_id, workspace_id, telegram_user_id,
                )
        if row is None:
            raise RuntimeError("Не удалось создать привязку web-аккаунта")
        return _binding_from_row(row)

    async def get_default_binding(self, web_user_id: int) -> WebAuthBinding | None:
        """No workspace-picker UI yet - first binding, oldest first."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM web_auth_bindings "
                "WHERE web_user_id = ? ORDER BY id ASC LIMIT 1",
                (web_user_id,),
            )
            row = await cursor.fetchone()
        return _binding_from_row(row) if row is not None else None

    async def get_binding_by_id(self, binding_id: int) -> WebAuthBinding | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await self._binding_row_by_id(db, binding_id)
        return _binding_from_row(row) if row is not None else None

    async def mark_onboarding_completed(self, binding_id: int) -> WebAuthBinding:
        """Idempotent - COALESCE keeps the original completion timestamp if
        the user re-opens /onboarding and saves again later (see
        web_api.py's onboarding_complete endpoint), rather than sliding it
        forward on every re-save."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                "UPDATE web_auth_bindings SET onboarding_completed_at = "
                "COALESCE(onboarding_completed_at, ?) WHERE id = ?",
                (_now(), binding_id),
            )
            await db.commit()
            row = await self._binding_row_by_id(db, binding_id)
        if row is None:
            raise RuntimeError("Привязка web-аккаунта не найдена")
        return _binding_from_row(row)

    # ── sessions ─────────────────────────────────────────────────────

    async def create_session(
        self, web_user_id: int, binding_id: int,
        session_token_hash: str, csrf_token_hash: str, expires_at: str,
    ) -> int:
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            cursor = await db.execute(
                "INSERT INTO web_auth_sessions "
                "(web_user_id, binding_id, session_token_hash, csrf_token_hash, "
                "created_at, expires_at, revoked_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
                (web_user_id, binding_id, session_token_hash, csrf_token_hash,
                 now, expires_at, now),
            )
            await db.commit()
        return cursor.lastrowid or 0

    async def get_session_context(
        self, session_token_hash: str,
    ) -> SessionContext | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT s.id AS session_id, s.web_user_id, u.email, u.status AS user_status, "
                "s.binding_id, b.workspace_id, b.telegram_user_id, "
                "s.csrf_token_hash, s.expires_at, s.revoked_at "
                "FROM web_auth_sessions AS s "
                "JOIN web_auth_users AS u ON u.id = s.web_user_id "
                "JOIN web_auth_bindings AS b ON b.id = s.binding_id "
                "WHERE s.session_token_hash = ?",
                (session_token_hash,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return SessionContext(
            session_id=row["session_id"],
            web_user_id=row["web_user_id"],
            email=row["email"],
            user_status=row["user_status"],
            binding_id=row["binding_id"],
            workspace_id=row["workspace_id"],
            telegram_user_id=row["telegram_user_id"],
            csrf_token_hash=row["csrf_token_hash"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
        )

    async def touch_session_last_seen(self, session_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE web_auth_sessions SET last_seen_at = ? WHERE id = ?",
                (_now(), session_id),
            )
            await db.commit()

    async def revoke_session(self, session_token_hash: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "UPDATE web_auth_sessions SET revoked_at = ? "
                "WHERE session_token_hash = ? AND revoked_at IS NULL",
                (_now(), session_token_hash),
            )
            await db.commit()
        return cursor.rowcount > 0

    # ── invites ──────────────────────────────────────────────────────

    async def create_invite(
        self, workspace_id: int, telegram_user_id: int, token_hash: str,
        expires_at: str, *, email_restriction: str | None = None,
    ) -> WebAuthInvite:
        now = _now()
        normalized_email = (
            _normalize_email(email_restriction) if email_restriction else None
        )
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            cursor = await db.execute(
                "INSERT INTO web_auth_invites "
                "(workspace_id, telegram_user_id, token_hash, email_restriction, "
                "created_at, expires_at, used_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (workspace_id, telegram_user_id, token_hash, normalized_email,
                 now, expires_at),
            )
            await db.commit()
            row = await self._invite_row_by_id(db, cursor.lastrowid or 0)
        if row is None:
            raise RuntimeError("Не удалось создать приглашение")
        return _invite_from_row(row)

    async def get_invite_by_token_hash(self, token_hash: str) -> WebAuthInvite | None:
        """Read-only peek - does NOT mark the invite used. Callers validate
        with this first (expiry, email_restriction) so a legitimate
        mismatch (e.g. wrong email typed) doesn't burn the one-time invite;
        consume_invite() is still the sole atomic "mark used" operation."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await self._invite_row_by_hash(db, token_hash)
        return _invite_from_row(row) if row is not None else None

    async def consume_invite(self, token_hash: str) -> WebAuthInvite | None:
        """Atomically validates (exists, not expired, not already used) and
        marks the invite used in one transaction, so two concurrent
        registration attempts with the same token can never both
        succeed - same optimistic-concurrency shape as
        ArtifactRepository.add_artifact_version_if_current. Returns None on
        any failure (unknown token, expired, already used); the caller
        can't distinguish which, by design."""
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await self._invite_row_by_hash(db, token_hash)
                if row is None or row["used_at"] is not None or row["expires_at"] <= now:
                    await db.rollback()
                    return None
                cursor = await db.execute(
                    "UPDATE web_auth_invites SET used_at = ? "
                    "WHERE id = ? AND used_at IS NULL",
                    (now, row["id"]),
                )
                if cursor.rowcount == 0:
                    await db.rollback()
                    return None
                updated_row = await self._invite_row_by_id(db, row["id"])
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return _invite_from_row(updated_row) if updated_row is not None else None

    # ── row helpers ──────────────────────────────────────────────────

    @staticmethod
    async def _user_row_by_id(db: aiosqlite.Connection, user_id: int):
        cursor = await db.execute(
            "SELECT * FROM web_auth_users WHERE id = ?", (user_id,),
        )
        return await cursor.fetchone()

    @staticmethod
    async def _binding_row_by_id(db: aiosqlite.Connection, binding_id: int):
        cursor = await db.execute(
            "SELECT * FROM web_auth_bindings WHERE id = ?", (binding_id,),
        )
        return await cursor.fetchone()

    @staticmethod
    async def _binding_row(
        db: aiosqlite.Connection, web_user_id: int, workspace_id: int,
        telegram_user_id: int,
    ):
        cursor = await db.execute(
            "SELECT * FROM web_auth_bindings "
            "WHERE web_user_id = ? AND workspace_id = ? AND telegram_user_id = ?",
            (web_user_id, workspace_id, telegram_user_id),
        )
        return await cursor.fetchone()

    @staticmethod
    async def _invite_row_by_id(db: aiosqlite.Connection, invite_id: int):
        cursor = await db.execute(
            "SELECT * FROM web_auth_invites WHERE id = ?", (invite_id,),
        )
        return await cursor.fetchone()

    @staticmethod
    async def _invite_row_by_hash(db: aiosqlite.Connection, token_hash: str):
        cursor = await db.execute(
            "SELECT * FROM web_auth_invites WHERE token_hash = ?", (token_hash,),
        )
        return await cursor.fetchone()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _user_from_row(row: aiosqlite.Row) -> WebAuthUser:
    return WebAuthUser(
        id=row["id"],
        email=row["email"],
        password_hash=row["password_hash"],
        status=row["status"],
        created_at=row["created_at"],
        last_login_at=row["last_login_at"],
    )


def _binding_from_row(row: aiosqlite.Row) -> WebAuthBinding:
    return WebAuthBinding(
        id=row["id"],
        web_user_id=row["web_user_id"],
        workspace_id=row["workspace_id"],
        telegram_user_id=row["telegram_user_id"],
        created_at=row["created_at"],
        onboarding_completed_at=row["onboarding_completed_at"],
    )


def _invite_from_row(row: aiosqlite.Row) -> WebAuthInvite:
    return WebAuthInvite(
        id=row["id"],
        workspace_id=row["workspace_id"],
        telegram_user_id=row["telegram_user_id"],
        token_hash=row["token_hash"],
        email_restriction=row["email_restriction"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        used_at=row["used_at"],
    )
