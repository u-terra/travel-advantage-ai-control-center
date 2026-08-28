"""Conversation Core Foundation (F1) - restart-safe working-state storage.

Same file, same style as every other repository here (ArtifactRepository,
CompetitorRepository, WorkRepository): plain aiosqlite, ``CREATE TABLE IF
NOT EXISTS`` migrations only (additive, never destructive), workspace
isolation enforced by including workspace_id (and telegram_user_id) in every
WHERE clause rather than by a separate authorization check.

F1 is infrastructure only - nothing in app/handlers reads or writes through
this repository yet. It exists in parallel with the current Telegram flow
without controlling it (see the Conversation Core Foundation F1 report).

Three tables. conversation_offer's invariant is "at most one active offer
per (workspace_id, telegram_user_id, offer_type)"; conversation_pending_
question's is "at most one active question per (workspace_id,
telegram_user_id)" (no per-type split - unlike offers, this repo has no
case yet of two *different* pending-question types needing to be active
for the same user at once). Both enforced by a partial UNIQUE index rather
than application-level locking:

- conversation_state: one row per (workspace_id, telegram_user_id). Holds
  ``current_artifact_id`` only - NOT a cached current/previous version
  number. ArtifactRepository.get_current_artifact_version /
  list_artifact_versions remain the single source of truth for versions;
  duplicating that here would create a second, driftable source of truth.
- conversation_offer: a structured, expiring offer (e.g. 3 content topics,
  or 3 Radar content ideas). F2D: scoped per offer_type (see below) -
  before F2D the index was per (workspace_id, telegram_user_id) only,
  which meant a still-active Radar offer would block creating a
  content_topics offer for the same user (and vice versa) even though
  they are unrelated, independent objects. Fixed by including offer_type
  in the unique index - a user can have at most one active offer *of each
  type* at a time, never two of the *same* type.
- conversation_pending_question: a single expiring question slot, separate
  from any message history/transcript.

TTL handling: expired rows are never returned by the ``get_active_*`` reads
(filtered by ``expires_at``), and are auto-superseded (marked
consumed/answered) the moment a new offer/question is created for the same
(workspace_id, telegram_user_id[, offer_type]) - so an old, forgotten,
expired row can never permanently block a new one. Creating a new
offer/question while a *non-expired* one is still active (same scope)
raises ``ConversationStateConflictError`` instead of silently overwriting
live state - this is exactly the "explicit new request replaces the
previous one of the same type" semantics F2D asked for, not a generic
multi-offer system.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

import aiosqlite

from app.domain.conversation_state import (
    ConversationState,
    ConversationStateValidationError,
    OfferItem,
    PendingOffer,
    PendingQuestion,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_state (
    workspace_id INTEGER NOT NULL,
    telegram_user_id INTEGER NOT NULL,
    active_module TEXT,
    current_task TEXT,
    current_subject_ref_type TEXT,
    current_subject_ref_id INTEGER,
    current_artifact_id INTEGER,
    last_action TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, telegram_user_id)
);

CREATE TABLE IF NOT EXISTS conversation_offer (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    telegram_user_id INTEGER NOT NULL,
    offer_type TEXT NOT NULL,
    items_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    consumed_at TEXT,
    CHECK (length(trim(offer_type)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_conversation_offer_scope
    ON conversation_offer(workspace_id, telegram_user_id, id DESC);

-- F2D additive migration: replaces ux_conversation_offer_active (unique per
-- (workspace_id, telegram_user_id) only) with a per-offer_type index, so an
-- active Radar offer and an active content_topics offer can coexist for the
-- same user. DROP is safe/idempotent - this repository has never been
-- deployed with data depending on the old index (see the F1/F2A/F2B/F2D
-- reports: every stage explicitly stopped short of deploying).
DROP INDEX IF EXISTS ux_conversation_offer_active;

CREATE UNIQUE INDEX IF NOT EXISTS ux_conversation_offer_active_by_type
    ON conversation_offer(workspace_id, telegram_user_id, offer_type)
    WHERE consumed_at IS NULL;

CREATE TABLE IF NOT EXISTS conversation_pending_question (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    telegram_user_id INTEGER NOT NULL,
    question_type TEXT NOT NULL,
    subject_ref_type TEXT,
    subject_ref_id INTEGER,
    prompt_text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    answered_at TEXT,
    CHECK (length(trim(question_type)) > 0),
    CHECK (length(trim(prompt_text)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_conversation_pending_question_scope
    ON conversation_pending_question(workspace_id, telegram_user_id, id DESC);

CREATE UNIQUE INDEX IF NOT EXISTS ux_conversation_pending_question_active
    ON conversation_pending_question(workspace_id, telegram_user_id)
    WHERE answered_at IS NULL;
"""


class ConversationStateConflictError(RuntimeError):
    """A new offer/question would violate the single-active-per-user invariant.

    Raised only when a *non-expired* active row already exists - an expired
    one is auto-superseded instead (see module docstring).
    """


class ConversationStateSerializationError(ValueError):
    """Offer items could not be serialized to/deserialized from storage.

    Fail-closed: this is raised instead of silently storing/returning a
    partially-serialized or corrupt payload.
    """


class _UnsetType:
    """Sentinel distinguishing "field not passed" from "field set to None".

    ``patch_state`` needs this precisely because ConversationState's own
    fields are all Optional - ``None`` is a legitimate value (e.g. "no
    current subject"), so it cannot double as "leave this field alone".
    """

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return "UNSET"


UNSET: Any = _UnsetType()


class ConversationStateRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.executescript(_SCHEMA)
            await db.commit()

    # ── ConversationState ───────────────────────────────────────────────

    async def get_state(
        self, workspace_id: int, telegram_user_id: int
    ) -> ConversationState | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await self._state_row(db, workspace_id, telegram_user_id)
        return _state_from_row(row) if row is not None else None

    async def upsert_state(
        self,
        workspace_id: int,
        telegram_user_id: int,
        *,
        active_module: str | None = None,
        current_task: str | None = None,
        current_subject_ref_type: str | None = None,
        current_subject_ref_id: int | None = None,
        current_artifact_id: int | None = None,
        last_action: str | None = None,
    ) -> ConversationState:
        """Fully replaces the working-state row for this (workspace, user).

        This is a replace, not a per-field merge: any field left at its
        default (None) overwrites whatever was stored before. Callers that
        want to preserve a field must pass its current value explicitly, or
        use ``patch_state`` instead - live handlers/services should prefer
        ``patch_state`` for exactly this reason (see its docstring).
        """
        now = _now()
        state = ConversationState(
            workspace_id=workspace_id,
            telegram_user_id=telegram_user_id,
            active_module=active_module,
            current_task=current_task,
            current_subject_ref_type=current_subject_ref_type,
            current_subject_ref_id=current_subject_ref_id,
            current_artifact_id=current_artifact_id,
            last_action=last_action,
            updated_at=now,
        )
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await self._write_state(db, state)
            await db.commit()
            row = await self._state_row(db, workspace_id, telegram_user_id)
        if row is None:
            raise RuntimeError("Не удалось сохранить working state")
        return _state_from_row(row)

    async def patch_state(
        self,
        workspace_id: int,
        telegram_user_id: int,
        *,
        active_module: Any = UNSET,
        current_task: Any = UNSET,
        current_subject_ref_type: Any = UNSET,
        current_subject_ref_id: Any = UNSET,
        current_artifact_id: Any = UNSET,
        last_action: Any = UNSET,
    ) -> ConversationState:
        """Partially updates the working-state row - "not passed" != "NULL".

        Any field left at the ``UNSET`` default keeps whatever was already
        stored (or None, if there was no row yet); pass ``None`` explicitly
        to clear a nullable field. This is the API live handlers/services
        must use for incremental updates (e.g. "just record last_action") -
        ``upsert_state`` remains available as the low-level full-replace
        primitive but must not be used for partial updates, since any field
        omitted there is silently set to None rather than preserved.

        current_subject_ref_type/current_subject_ref_id are patched
        atomically as a pair: both UNSET (leave the ref alone) or both
        given (set it, or clear it with two Nones) - patching only one
        would risk ending up with a half-set ref pair, which
        ConversationState.__post_init__ already rejects at construction
        time, so this fails fast in the repository instead of surfacing as
        a confusing validation error deep in a merge.
        """
        ref_type_given = current_subject_ref_type is not UNSET
        ref_id_given = current_subject_ref_id is not UNSET
        if ref_type_given != ref_id_given:
            raise ConversationStateValidationError(
                "current_subject_ref_type and current_subject_ref_id must be "
                "patched together (both UNSET, or both given)"
            )

        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await self._state_row(db, workspace_id, telegram_user_id)
                current = _state_from_row(row) if row is not None else None

                def _resolve(value: Any, field: str) -> Any:
                    if value is not UNSET:
                        return value
                    return getattr(current, field) if current is not None else None

                merged = ConversationState(
                    workspace_id=workspace_id,
                    telegram_user_id=telegram_user_id,
                    active_module=_resolve(active_module, "active_module"),
                    current_task=_resolve(current_task, "current_task"),
                    current_subject_ref_type=_resolve(
                        current_subject_ref_type, "current_subject_ref_type"
                    ),
                    current_subject_ref_id=_resolve(
                        current_subject_ref_id, "current_subject_ref_id"
                    ),
                    current_artifact_id=_resolve(
                        current_artifact_id, "current_artifact_id"
                    ),
                    last_action=_resolve(last_action, "last_action"),
                    updated_at=now,
                )
                await self._write_state(db, merged)
                await db.commit()
                row = await self._state_row(db, workspace_id, telegram_user_id)
            except BaseException:
                await db.rollback()
                raise
        if row is None:
            raise RuntimeError("Не удалось обновить working state")
        return _state_from_row(row)

    @staticmethod
    async def _write_state(db: aiosqlite.Connection, state: ConversationState) -> None:
        await db.execute(
            "INSERT INTO conversation_state "
            "(workspace_id, telegram_user_id, active_module, current_task, "
            "current_subject_ref_type, current_subject_ref_id, "
            "current_artifact_id, last_action, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(workspace_id, telegram_user_id) DO UPDATE SET "
            "active_module = excluded.active_module, "
            "current_task = excluded.current_task, "
            "current_subject_ref_type = excluded.current_subject_ref_type, "
            "current_subject_ref_id = excluded.current_subject_ref_id, "
            "current_artifact_id = excluded.current_artifact_id, "
            "last_action = excluded.last_action, "
            "updated_at = excluded.updated_at",
            (
                state.workspace_id, state.telegram_user_id, state.active_module,
                state.current_task, state.current_subject_ref_type,
                state.current_subject_ref_id, state.current_artifact_id,
                state.last_action, state.updated_at,
            ),
        )

    async def clear_state(self, workspace_id: int, telegram_user_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM conversation_state "
                "WHERE workspace_id = ? AND telegram_user_id = ?",
                (workspace_id, telegram_user_id),
            )
            await db.commit()

    # ── PendingOffer ─────────────────────────────────────────────────────

    async def create_offer(
        self,
        workspace_id: int,
        telegram_user_id: int,
        offer_type: str,
        items: tuple[OfferItem, ...],
        *,
        expires_at: str | None = None,
    ) -> PendingOffer:
        now = _now()
        items_json = _serialize_items(items)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                # TTL supersession: a stale, never-consumed offer of the SAME
                # offer_type must not permanently block a new one of that
                # type. Scoped by offer_type (F2D) so creating a new
                # content_topics offer never touches an unrelated, still-
                # relevant Radar offer (or vice versa) even if that other
                # offer happens to be expired too - each offer_type manages
                # its own lifecycle independently.
                await db.execute(
                    "UPDATE conversation_offer SET consumed_at = ? "
                    "WHERE workspace_id = ? AND telegram_user_id = ? AND offer_type = ? "
                    "AND consumed_at IS NULL "
                    "AND expires_at IS NOT NULL AND expires_at <= ?",
                    (now, workspace_id, telegram_user_id, offer_type, now),
                )
                cursor = await db.execute(
                    "INSERT INTO conversation_offer "
                    "(workspace_id, telegram_user_id, offer_type, items_json, "
                    "created_at, expires_at, consumed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                    (
                        workspace_id, telegram_user_id, offer_type, items_json,
                        now, expires_at,
                    ),
                )
                offer_id = cursor.lastrowid or 0
                row = await self._offer_row(db, workspace_id, telegram_user_id, offer_id)
                await db.commit()
            except aiosqlite.IntegrityError as exc:
                await db.rollback()
                raise ConversationStateConflictError(
                    f"an active {offer_type!r} offer already exists for this "
                    "workspace/user - consume it (or let it expire) first"
                ) from exc
            except BaseException:
                await db.rollback()
                raise
        if row is None:
            raise RuntimeError("Не удалось сохранить offer")
        return _offer_from_row(row)

    async def get_active_offer(
        self, workspace_id: int, telegram_user_id: int, offer_type: str
    ) -> PendingOffer | None:
        """Reads the active offer of a specific offer_type only.

        F2D: offer_type is required (not optional) precisely because more
        than one offer_type can now be active at once for the same user
        (see the unique index change in the module docstring) - a caller
        that wants "the Radar offer" must not accidentally get back a
        content_topics offer, or vice versa.
        """
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM conversation_offer "
                "WHERE workspace_id = ? AND telegram_user_id = ? AND offer_type = ? "
                "AND consumed_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > ?)",
                (workspace_id, telegram_user_id, offer_type, now),
            )
            row = await cursor.fetchone()
        return _offer_from_row(row) if row is not None else None

    async def consume_offer(
        self, workspace_id: int, telegram_user_id: int, offer_id: int
    ) -> PendingOffer | None:
        """Atomically marks the offer consumed - exactly one caller succeeds.

        Returns None if the offer does not exist, belongs to another
        workspace/user, is already consumed, or has expired - all
        indistinguishable to the caller, same convention as
        CompetitorRepository.update_label.
        """
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "UPDATE conversation_offer SET consumed_at = ? "
                "WHERE workspace_id = ? AND telegram_user_id = ? AND id = ? "
                "AND consumed_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > ?)",
                (now, workspace_id, telegram_user_id, offer_id, now),
            )
            consumed = cursor.rowcount == 1
            await db.commit()
            row = await self._offer_row(db, workspace_id, telegram_user_id, offer_id)
        if not consumed or row is None:
            return None
        return _offer_from_row(row)

    @staticmethod
    async def _offer_row(
        db: aiosqlite.Connection, workspace_id: int, telegram_user_id: int, offer_id: int
    ) -> aiosqlite.Row | None:
        cursor = await db.execute(
            "SELECT * FROM conversation_offer "
            "WHERE workspace_id = ? AND telegram_user_id = ? AND id = ?",
            (workspace_id, telegram_user_id, offer_id),
        )
        return await cursor.fetchone()

    # ── PendingQuestion ──────────────────────────────────────────────────

    async def create_question(
        self,
        workspace_id: int,
        telegram_user_id: int,
        question_type: str,
        prompt_text: str,
        *,
        subject_ref_type: str | None = None,
        subject_ref_id: int | None = None,
        expires_at: str | None = None,
    ) -> PendingQuestion:
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    "UPDATE conversation_pending_question SET answered_at = ? "
                    "WHERE workspace_id = ? AND telegram_user_id = ? "
                    "AND answered_at IS NULL "
                    "AND expires_at IS NOT NULL AND expires_at <= ?",
                    (now, workspace_id, telegram_user_id, now),
                )
                cursor = await db.execute(
                    "INSERT INTO conversation_pending_question "
                    "(workspace_id, telegram_user_id, question_type, "
                    "subject_ref_type, subject_ref_id, prompt_text, "
                    "created_at, expires_at, answered_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                    (
                        workspace_id, telegram_user_id, question_type,
                        subject_ref_type, subject_ref_id, prompt_text,
                        now, expires_at,
                    ),
                )
                question_id = cursor.lastrowid or 0
                row = await self._question_row(
                    db, workspace_id, telegram_user_id, question_id
                )
                await db.commit()
            except aiosqlite.IntegrityError as exc:
                await db.rollback()
                raise ConversationStateConflictError(
                    "an active pending question already exists for this "
                    "workspace/user - answer it (or let it expire) first"
                ) from exc
            except BaseException:
                await db.rollback()
                raise
        if row is None:
            raise RuntimeError("Не удалось сохранить pending question")
        return _question_from_row(row)

    async def get_active_question(
        self, workspace_id: int, telegram_user_id: int
    ) -> PendingQuestion | None:
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM conversation_pending_question "
                "WHERE workspace_id = ? AND telegram_user_id = ? "
                "AND answered_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > ?)",
                (workspace_id, telegram_user_id, now),
            )
            row = await cursor.fetchone()
        return _question_from_row(row) if row is not None else None

    async def answer_question(
        self, workspace_id: int, telegram_user_id: int, question_id: int
    ) -> PendingQuestion | None:
        """Atomically marks the question answered - exactly one caller succeeds.

        Same not-found/wrong-scope/already-answered/expired collapse as
        ``consume_offer``.
        """
        now = _now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "UPDATE conversation_pending_question SET answered_at = ? "
                "WHERE workspace_id = ? AND telegram_user_id = ? AND id = ? "
                "AND answered_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > ?)",
                (now, workspace_id, telegram_user_id, question_id, now),
            )
            answered = cursor.rowcount == 1
            await db.commit()
            row = await self._question_row(db, workspace_id, telegram_user_id, question_id)
        if not answered or row is None:
            return None
        return _question_from_row(row)

    @staticmethod
    async def _question_row(
        db: aiosqlite.Connection, workspace_id: int, telegram_user_id: int, question_id: int
    ) -> aiosqlite.Row | None:
        cursor = await db.execute(
            "SELECT * FROM conversation_pending_question "
            "WHERE workspace_id = ? AND telegram_user_id = ? AND id = ?",
            (workspace_id, telegram_user_id, question_id),
        )
        return await cursor.fetchone()

    @staticmethod
    async def _state_row(
        db: aiosqlite.Connection, workspace_id: int, telegram_user_id: int
    ) -> aiosqlite.Row | None:
        cursor = await db.execute(
            "SELECT * FROM conversation_state "
            "WHERE workspace_id = ? AND telegram_user_id = ?",
            (workspace_id, telegram_user_id),
        )
        return await cursor.fetchone()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_plain(value: Any) -> Any:
    """Reverses domain-side JSON-safe freezing (MappingProxyType/tuple) back
    into plain dict/list so ``json.dumps`` can serialize it."""
    if isinstance(value, MappingProxyType):
        return {key: _to_plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_to_plain(item) for item in value]
    return value


def _serialize_items(items: tuple[OfferItem, ...]) -> str:
    try:
        plain = [
            {"id": item.id, "label": item.label, "payload": _to_plain(item.payload)}
            for item in items
        ]
        return json.dumps(plain, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ConversationStateSerializationError(
            "offer items are not JSON-safe"
        ) from exc


def _deserialize_items(raw: str) -> tuple[OfferItem, ...]:
    try:
        plain = json.loads(raw)
        if not isinstance(plain, list) or not plain:
            raise ValueError("items_json must decode to a non-empty list")
        return tuple(
            OfferItem(
                id=entry["id"], label=entry["label"], payload=entry.get("payload", {}),
            )
            for entry in plain
        )
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise ConversationStateSerializationError(
            "stored offer items are corrupt or not JSON-safe"
        ) from exc


def _state_from_row(row: aiosqlite.Row) -> ConversationState:
    return ConversationState(
        workspace_id=row["workspace_id"],
        telegram_user_id=row["telegram_user_id"],
        active_module=row["active_module"],
        current_task=row["current_task"],
        current_subject_ref_type=row["current_subject_ref_type"],
        current_subject_ref_id=row["current_subject_ref_id"],
        current_artifact_id=row["current_artifact_id"],
        last_action=row["last_action"],
        updated_at=row["updated_at"],
    )


def _offer_from_row(row: aiosqlite.Row) -> PendingOffer:
    return PendingOffer(
        id=row["id"],
        workspace_id=row["workspace_id"],
        telegram_user_id=row["telegram_user_id"],
        offer_type=row["offer_type"],
        items=_deserialize_items(row["items_json"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        consumed_at=row["consumed_at"],
    )


def _question_from_row(row: aiosqlite.Row) -> PendingQuestion:
    return PendingQuestion(
        id=row["id"],
        workspace_id=row["workspace_id"],
        telegram_user_id=row["telegram_user_id"],
        question_type=row["question_type"],
        subject_ref_type=row["subject_ref_type"],
        subject_ref_id=row["subject_ref_id"],
        prompt_text=row["prompt_text"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        answered_at=row["answered_at"],
    )
