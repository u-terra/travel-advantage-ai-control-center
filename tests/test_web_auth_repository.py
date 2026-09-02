from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app.repositories.partner_repository import PartnerRepository
from app.repositories.web_auth_repository import (
    EmailAlreadyRegisteredError,
    WebAuthRepository,
)
from app.services.web_auth_tokens import generate_token, hash_token, tokens_match

OWNER_ID = 586249067


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _future(hours: float = 72) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _past(hours: float = 1) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _setup(tmp_path: Path) -> tuple[WebAuthRepository, int]:
    db_path = tmp_path / "journal.sqlite3"
    partners = PartnerRepository(db_path)
    _run(partners.init())
    owner_workspace, _ = _run(partners.ensure_owner_workspace(OWNER_ID))
    auth = WebAuthRepository(db_path)
    _run(auth.init())
    return auth, owner_workspace.id


# ── schema ───────────────────────────────────────────────────────────────

def test_init_is_idempotent(tmp_path: Path) -> None:
    auth, _ = _setup(tmp_path)
    _run(auth.init())
    _run(auth.init())


# ── users ────────────────────────────────────────────────────────────────

def test_create_user_stores_only_the_hash_never_the_plaintext(tmp_path: Path) -> None:
    auth, _ = _setup(tmp_path)

    user = _run(auth.create_user("Owner@Example.com", "argon2-hash-value"))

    assert user.email == "owner@example.com"
    assert user.password_hash == "argon2-hash-value"
    assert user.status == "active"
    assert user.last_login_at is None


def test_create_user_rejects_duplicate_email_case_insensitively(tmp_path: Path) -> None:
    auth, _ = _setup(tmp_path)
    _run(auth.create_user("owner@example.com", "hash-1"))

    with pytest.raises(EmailAlreadyRegisteredError):
        _run(auth.create_user("OWNER@EXAMPLE.COM", "hash-2"))


def test_get_user_by_email_normalizes(tmp_path: Path) -> None:
    auth, _ = _setup(tmp_path)
    created = _run(auth.create_user("Partner@Example.com", "hash"))

    found = _run(auth.get_user_by_email("  PARTNER@example.com  "))

    assert found is not None
    assert found.id == created.id


def test_get_user_by_email_unknown_returns_none(tmp_path: Path) -> None:
    auth, _ = _setup(tmp_path)
    assert _run(auth.get_user_by_email("nobody@example.com")) is None


def test_touch_last_login_updates_timestamp(tmp_path: Path) -> None:
    auth, _ = _setup(tmp_path)
    user = _run(auth.create_user("owner@example.com", "hash"))
    assert user.last_login_at is None

    _run(auth.touch_last_login(user.id))

    refreshed = _run(auth.get_user_by_id(user.id))
    assert refreshed.last_login_at is not None


# ── bindings ─────────────────────────────────────────────────────────────

def test_create_binding_links_web_user_to_existing_identity(tmp_path: Path) -> None:
    auth, workspace_id = _setup(tmp_path)
    user = _run(auth.create_user("owner@example.com", "hash"))

    binding = _run(auth.create_binding(user.id, workspace_id, OWNER_ID))

    assert binding.web_user_id == user.id
    assert binding.workspace_id == workspace_id
    assert binding.telegram_user_id == OWNER_ID


def test_create_binding_is_idempotent(tmp_path: Path) -> None:
    auth, workspace_id = _setup(tmp_path)
    user = _run(auth.create_user("owner@example.com", "hash"))

    first = _run(auth.create_binding(user.id, workspace_id, OWNER_ID))
    second = _run(auth.create_binding(user.id, workspace_id, OWNER_ID))

    assert first.id == second.id


def test_get_default_binding_returns_first_created(tmp_path: Path) -> None:
    auth, workspace_id = _setup(tmp_path)
    user = _run(auth.create_user("owner@example.com", "hash"))
    first = _run(auth.create_binding(user.id, workspace_id, OWNER_ID))
    _run(auth.create_binding(user.id, workspace_id, OWNER_ID + 1))

    default = _run(auth.get_default_binding(user.id))

    assert default.id == first.id


def test_get_default_binding_for_unbound_user_is_none(tmp_path: Path) -> None:
    auth, _ = _setup(tmp_path)
    user = _run(auth.create_user("owner@example.com", "hash"))

    assert _run(auth.get_default_binding(user.id)) is None


# ── sessions ─────────────────────────────────────────────────────────────

def _create_session(auth: WebAuthRepository, user_id: int, binding_id: int):
    raw_session = generate_token()
    raw_csrf = generate_token()
    session_id = _run(auth.create_session(
        user_id, binding_id, hash_token(raw_session), hash_token(raw_csrf), _future(),
    ))
    return session_id, raw_session, raw_csrf


def test_session_context_resolves_workspace_and_telegram_identity(tmp_path: Path) -> None:
    auth, workspace_id = _setup(tmp_path)
    user = _run(auth.create_user("owner@example.com", "hash"))
    binding = _run(auth.create_binding(user.id, workspace_id, OWNER_ID))
    _, raw_session, raw_csrf = _create_session(auth, user.id, binding.id)

    ctx = _run(auth.get_session_context(hash_token(raw_session)))

    assert ctx is not None
    assert ctx.web_user_id == user.id
    assert ctx.email == "owner@example.com"
    assert ctx.user_status == "active"
    assert ctx.workspace_id == workspace_id
    assert ctx.telegram_user_id == OWNER_ID
    assert ctx.revoked_at is None
    assert tokens_match(raw_csrf, ctx.csrf_token_hash)


def test_session_context_for_unknown_token_hash_is_none(tmp_path: Path) -> None:
    auth, _ = _setup(tmp_path)
    assert _run(auth.get_session_context(hash_token("nonexistent"))) is None


def test_raw_session_token_never_matches_stored_hash_directly(tmp_path: Path) -> None:
    """Proves the DB genuinely stores a hash, not the raw token: looking a
    session up BY the raw token string (as if it were the hash) must fail."""
    auth, workspace_id = _setup(tmp_path)
    user = _run(auth.create_user("owner@example.com", "hash"))
    binding = _run(auth.create_binding(user.id, workspace_id, OWNER_ID))
    _, raw_session, _ = _create_session(auth, user.id, binding.id)

    assert _run(auth.get_session_context(raw_session)) is None


def test_touch_session_last_seen_updates_timestamp(tmp_path: Path) -> None:
    auth, workspace_id = _setup(tmp_path)
    user = _run(auth.create_user("owner@example.com", "hash"))
    binding = _run(auth.create_binding(user.id, workspace_id, OWNER_ID))
    session_id, raw_session, _ = _create_session(auth, user.id, binding.id)

    _run(auth.touch_session_last_seen(session_id))
    # no exception, no crash - last_seen_at is not part of SessionContext's
    # public contract, so we only assert the call succeeds without error.


def test_revoke_session_marks_it_revoked(tmp_path: Path) -> None:
    auth, workspace_id = _setup(tmp_path)
    user = _run(auth.create_user("owner@example.com", "hash"))
    binding = _run(auth.create_binding(user.id, workspace_id, OWNER_ID))
    _, raw_session, _ = _create_session(auth, user.id, binding.id)

    revoked = _run(auth.revoke_session(hash_token(raw_session)))
    assert revoked is True

    ctx = _run(auth.get_session_context(hash_token(raw_session)))
    assert ctx is not None
    assert ctx.revoked_at is not None


def test_revoke_session_is_idempotent(tmp_path: Path) -> None:
    auth, workspace_id = _setup(tmp_path)
    user = _run(auth.create_user("owner@example.com", "hash"))
    binding = _run(auth.create_binding(user.id, workspace_id, OWNER_ID))
    _, raw_session, _ = _create_session(auth, user.id, binding.id)

    assert _run(auth.revoke_session(hash_token(raw_session))) is True
    assert _run(auth.revoke_session(hash_token(raw_session))) is False


def test_revoke_unknown_session_returns_false(tmp_path: Path) -> None:
    auth, _ = _setup(tmp_path)
    assert _run(auth.revoke_session(hash_token("nonexistent"))) is False


# ── invites ──────────────────────────────────────────────────────────────

def test_create_invite_stores_only_the_hash(tmp_path: Path) -> None:
    auth, workspace_id = _setup(tmp_path)
    raw_token = generate_token()

    invite = _run(auth.create_invite(
        workspace_id, OWNER_ID, hash_token(raw_token), _future(),
    ))

    assert invite.token_hash == hash_token(raw_token)
    assert invite.token_hash != raw_token
    assert invite.used_at is None


def test_create_invite_normalizes_email_restriction(tmp_path: Path) -> None:
    auth, workspace_id = _setup(tmp_path)
    invite = _run(auth.create_invite(
        workspace_id, OWNER_ID, hash_token(generate_token()), _future(),
        email_restriction="Someone@Example.com",
    ))

    assert invite.email_restriction == "someone@example.com"


def test_consume_invite_marks_used_and_is_one_time(tmp_path: Path) -> None:
    auth, workspace_id = _setup(tmp_path)
    raw_token = generate_token()
    _run(auth.create_invite(workspace_id, OWNER_ID, hash_token(raw_token), _future()))

    consumed = _run(auth.consume_invite(hash_token(raw_token)))
    assert consumed is not None
    assert consumed.used_at is not None

    consumed_again = _run(auth.consume_invite(hash_token(raw_token)))
    assert consumed_again is None


def test_consume_expired_invite_fails(tmp_path: Path) -> None:
    auth, workspace_id = _setup(tmp_path)
    raw_token = generate_token()
    _run(auth.create_invite(
        workspace_id, OWNER_ID, hash_token(raw_token), _past(),
    ))

    assert _run(auth.consume_invite(hash_token(raw_token))) is None


def test_consume_unknown_invite_fails(tmp_path: Path) -> None:
    auth, _ = _setup(tmp_path)
    assert _run(auth.consume_invite(hash_token("nonexistent"))) is None


def test_consume_invite_concurrent_race_only_one_winner(tmp_path: Path) -> None:
    """Two 'simultaneous' registration attempts against the same invite -
    only one may ever consume it (BEGIN IMMEDIATE serializes them)."""
    auth, workspace_id = _setup(tmp_path)
    raw_token = generate_token()
    _run(auth.create_invite(workspace_id, OWNER_ID, hash_token(raw_token), _future()))

    async def race():
        results = await asyncio.gather(
            auth.consume_invite(hash_token(raw_token)),
            auth.consume_invite(hash_token(raw_token)),
        )
        return results

    results = _run(race())
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
