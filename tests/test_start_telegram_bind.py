"""app.handlers.start's /start <token> handling - the Telegram side of the
one-time Telegram-connect deep link (see POST /api/telegram/bind-token,
app.repositories.telegram_bind_token_repository).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.handlers.lobby import WELCOME_NEW_TEXT
from app.handlers.start import (
    TELEGRAM_BIND_CONFLICT_TEXT,
    TELEGRAM_BIND_FAILED_TEXT,
    _try_bind_telegram,
    cmd_start,
)
from app.repositories.partner_repository import PartnerRepository
from app.repositories.telegram_bind_token_repository import TelegramBindTokenRepository
from app.services.web_auth_tokens import generate_token, hash_token


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class _FromUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class _Message:
    def __init__(self, from_user_id: int = 1) -> None:
        self.from_user = _FromUser(from_user_id)
        self.answers: list[tuple[str, Any]] = []

    async def answer(self, text: str, reply_markup: Any = None, **kwargs: Any) -> None:
        self.answers.append((text, reply_markup))


class _Command:
    def __init__(self, args: str | None) -> None:
        self.args = args


class _State:
    async def clear(self) -> None:
        pass


def _repos(tmp_path: Path):
    db_path = tmp_path / "journal.sqlite3"
    partners = PartnerRepository(db_path)
    run(partners.init())
    tokens = TelegramBindTokenRepository(db_path)
    run(tokens.init())
    return partners, tokens


def _future(minutes: int = 30) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


# ── _try_bind_telegram ───────────────────────────────────────────────────

def test_try_bind_telegram_success_renames_the_placeholder(tmp_path: Path):
    partners, tokens = _repos(tmp_path)
    provisioned = run(partners.provision_self_service_workspace("Bind Handler Test"))
    raw = generate_token()
    run(tokens.create_token(provisioned.workspace.id, hash_token(raw), _future()))

    reply = run(_try_bind_telegram(raw, 777000111, partners, tokens))

    assert "подключён" in reply.lower()
    context = run(partners.resolve_workspace_context(777000111))
    assert context is not None
    assert context.workspace_id == provisioned.workspace.id
    # The token is one-time - a second attempt with the same raw value fails.
    second = run(_try_bind_telegram(raw, 999888777, partners, tokens))
    assert second == TELEGRAM_BIND_FAILED_TEXT


def test_try_bind_telegram_unknown_token_fails_closed(tmp_path: Path):
    partners, tokens = _repos(tmp_path)
    reply = run(_try_bind_telegram("bogus-token-never-issued", 111, partners, tokens))
    assert reply == TELEGRAM_BIND_FAILED_TEXT


def test_try_bind_telegram_rejects_account_that_already_owns_a_workspace(tmp_path: Path):
    """Tenant isolation: a Telegram account already owning workspace A
    cannot use a bind token minted for self-service workspace B."""
    partners, tokens = _repos(tmp_path)
    run(partners.provision_partner(
        555444333, "Existing Owner", "existing-owner-bind",
        business_name="Existing Owner", business_type="other",
        short_description="", context={},
    ))
    provisioned = run(partners.provision_self_service_workspace("New Signup Bind"))
    raw = generate_token()
    run(tokens.create_token(provisioned.workspace.id, hash_token(raw), _future()))

    reply = run(_try_bind_telegram(raw, 555444333, partners, tokens))

    assert reply == TELEGRAM_BIND_CONFLICT_TEXT
    # The other workspace's own binding is untouched.
    context = run(partners.resolve_workspace_context(555444333))
    assert context.workspace_id != provisioned.workspace.id


def test_try_bind_telegram_missing_repositories_fails_closed():
    reply = run(_try_bind_telegram("anything", 111, None, None))
    assert reply == TELEGRAM_BIND_FAILED_TEXT


# ── cmd_start with a /start <token> payload ─────────────────────────────

def test_cmd_start_with_valid_token_binds_and_replies(tmp_path: Path):
    partners, tokens = _repos(tmp_path)
    provisioned = run(partners.provision_self_service_workspace("Cmd Start Bind"))
    raw = generate_token()
    run(tokens.create_token(provisioned.workspace.id, hash_token(raw), _future()))

    message = _Message(from_user_id=888000111)
    run(cmd_start(
        message, _State(), command=_Command(raw),
        partner_repository=partners, telegram_bind_token_repository=tokens,
    ))

    assert len(message.answers) == 1
    assert "подключён" in message.answers[0][0].lower()
    context = run(partners.resolve_workspace_context(888000111))
    assert context is not None


def test_cmd_start_with_expired_token_shows_failure_text(tmp_path: Path):
    partners, tokens = _repos(tmp_path)
    provisioned = run(partners.provision_self_service_workspace("Expired Token"))
    raw = generate_token()
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    run(tokens.create_token(provisioned.workspace.id, hash_token(raw), past))

    message = _Message(from_user_id=222)
    run(cmd_start(
        message, _State(), command=_Command(raw),
        partner_repository=partners, telegram_bind_token_repository=tokens,
    ))

    assert message.answers[0][0] == TELEGRAM_BIND_FAILED_TEXT


def test_cmd_start_without_command_object_is_unaffected() -> None:
    """No `command` kwarg at all (e.g. a call site that predates this
    feature) - must not raise, and must behave exactly like plain /start."""
    message = _Message()
    run(cmd_start(message, _State()))

    assert message.answers[0][0] == WELCOME_NEW_TEXT


def test_cmd_start_with_empty_args_behaves_like_plain_start() -> None:
    message = _Message()
    run(cmd_start(message, _State(), command=_Command(None)))

    assert message.answers[0][0] == WELCOME_NEW_TEXT


def test_new_client_gets_bot_access_without_telegram_allowed_user_ids(tmp_path: Path):
    """AllowlistMiddleware (app/access.py) no longer gates anything - the
    real gate is workspace_memberships (see app/access_state_gate.py,
    app/workspace_context.py). A self-service signup's Telegram account
    must resolve a working workspace_context purely from the bind, with
    TELEGRAM_ALLOWED_USER_IDS empty/unset the whole time."""
    from app.access import parse_allowed_user_ids

    partners, tokens = _repos(tmp_path)
    provisioned = run(partners.provision_self_service_workspace("No Allowlist Needed"))
    raw = generate_token()
    run(tokens.create_token(provisioned.workspace.id, hash_token(raw), _future()))
    real_telegram_id = 424242424

    allowed = parse_allowed_user_ids("")
    assert real_telegram_id not in allowed
    assert allowed == frozenset()

    message = _Message(from_user_id=real_telegram_id)
    run(cmd_start(
        message, _State(), command=_Command(raw),
        partner_repository=partners, telegram_bind_token_repository=tokens,
    ))

    context = run(partners.resolve_workspace_context(real_telegram_id))
    assert context is not None
    assert context.workspace_id == provisioned.workspace.id
    assert context.role == "owner"
