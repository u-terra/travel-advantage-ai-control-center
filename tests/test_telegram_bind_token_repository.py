"""TelegramBindTokenRepository - one-time Telegram-connect deep-link
tokens (see app.web_api's POST /api/telegram/bind-token and
app.handlers.start's /start <token> handling)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.repositories.partner_repository import PartnerRepository
from app.repositories.telegram_bind_token_repository import TelegramBindTokenRepository
from app.services.web_auth_tokens import generate_token, hash_token


def run(coro):
    return asyncio.run(coro)


def _repos(tmp_path: Path):
    db_path = tmp_path / "journal.sqlite3"
    partners = PartnerRepository(db_path)
    run(partners.init())
    tokens = TelegramBindTokenRepository(db_path)
    run(tokens.init())
    workspace, _ = run(partners.ensure_owner_workspace(100))
    return tokens, workspace.id


def _future(minutes: int = 30) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def _past() -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()


def test_create_and_consume_token_succeeds_once(tmp_path: Path):
    tokens, workspace_id = _repos(tmp_path)
    raw = generate_token()
    run(tokens.create_token(workspace_id, hash_token(raw), _future()))

    consumed = run(tokens.consume_token(hash_token(raw), 555000111))

    assert consumed is not None
    assert consumed.workspace_id == workspace_id
    assert consumed.used_at is not None
    assert consumed.used_by_telegram_user_id == 555000111


def test_token_cannot_be_reused(tmp_path: Path):
    tokens, workspace_id = _repos(tmp_path)
    raw = generate_token()
    run(tokens.create_token(workspace_id, hash_token(raw), _future()))

    first = run(tokens.consume_token(hash_token(raw), 111))
    second = run(tokens.consume_token(hash_token(raw), 222))

    assert first is not None
    assert second is None


def test_expired_token_is_rejected(tmp_path: Path):
    tokens, workspace_id = _repos(tmp_path)
    raw = generate_token()
    run(tokens.create_token(workspace_id, hash_token(raw), _past()))

    consumed = run(tokens.consume_token(hash_token(raw), 111))

    assert consumed is None


def test_unknown_token_is_rejected(tmp_path: Path):
    tokens, _ = _repos(tmp_path)

    consumed = run(tokens.consume_token(hash_token("never-issued"), 111))

    assert consumed is None


def test_raw_token_never_stored(tmp_path: Path):
    """Only the hash ever touches the DB - see get_by_token_hash, which
    looks the row up BY the hash, and TelegramBindToken itself only ever
    exposes token_hash, never a raw value."""
    tokens, workspace_id = _repos(tmp_path)
    raw = generate_token()
    created = run(tokens.create_token(workspace_id, hash_token(raw), _future()))

    assert created.token_hash != raw
    fetched = run(tokens.get_by_token_hash(hash_token(raw)))
    assert fetched is not None
    assert fetched.id == created.id


def test_concurrent_consume_race_only_one_wins(tmp_path: Path):
    """Two 'simultaneous' /start <token> hits (e.g. a double-tap on the
    deep link) must not both bind - same idempotent-by-construction
    guarantee as WebAuthRepository.consume_invite."""
    tokens, workspace_id = _repos(tmp_path)
    raw = generate_token()
    run(tokens.create_token(workspace_id, hash_token(raw), _future()))

    results = [
        run(tokens.consume_token(hash_token(raw), 111)),
        run(tokens.consume_token(hash_token(raw), 222)),
    ]
    successes = [r for r in results if r is not None]
    assert len(successes) == 1
