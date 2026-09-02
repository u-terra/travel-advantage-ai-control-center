"""Web-auth compatibility layer.

A web-account (email + password) is NOT a second business/access model -
PartnerRepository's workspace_memberships (role/status) stays the one
source of truth for who can do what inside a workspace. A web-account only
needs to answer one question: "which existing (workspace_id,
telegram_user_id) pair does this browser session act as?" - see
WebAuthBinding. Everything downstream (materials, competitors, chat,
usage accounting, ...) keeps working exactly as it did with the old
hardcoded WEB_WORKSPACE_ID/WEB_TELEGRAM_USER_ID, just sourced from a real
session instead of a constant.

A web-account can have more than one binding (future: multiple
workspaces per person) - the schema supports it, but there is no
workspace-picker UI yet, so callers use the first/only binding.
"""

from __future__ import annotations

from dataclasses import dataclass

USER_STATUSES = frozenset({"active", "disabled"})


@dataclass(frozen=True)
class WebAuthUser:
    id: int
    email: str
    password_hash: str
    status: str
    created_at: str
    last_login_at: str | None


@dataclass(frozen=True)
class WebAuthBinding:
    id: int
    web_user_id: int
    workspace_id: int
    telegram_user_id: int
    created_at: str


@dataclass(frozen=True)
class WebAuthInvite:
    id: int
    workspace_id: int
    telegram_user_id: int
    token_hash: str
    email_restriction: str | None
    created_at: str
    expires_at: str
    used_at: str | None


@dataclass(frozen=True)
class WebPrincipal:
    """Request-scoped identity resolved from the session cookie - the
    replacement for the old WEB_WORKSPACE_ID/WEB_TELEGRAM_USER_ID
    constants. role comes straight from PartnerRepository's own access
    model (resolve_workspace_context), never invented here."""
    web_user_id: int
    email: str
    workspace_id: int
    telegram_user_id: int
    role: str
    session_id: int
    csrf_token_hash: str
