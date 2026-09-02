"""Shared helper for tests that exercise app.web_api endpoints now gated
behind web-auth (see app.domain.web_auth / Task "web-auth"). Every
resource endpoint requires a real session; mutating ones also require the
CSRF header.

Not a test module itself (no test_ prefix - pytest won't collect it).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

DEFAULT_TEST_PASSWORD = "correcthorsebattery-test-suite"


def _run(coro):
    return asyncio.run(coro)


async def _ensure_active_membership(web_api, workspace_id: int, telegram_user_id: int, role: str) -> None:
    """get_current_principal() now fails closed on PartnerRepository's own
    access model (workspace_memberships) on every request - a web-auth
    binding alone is no longer enough. Most fixtures only called
    ensure_owner_workspace() (workspace + profile, no membership row), so
    login_as() needs a real active membership to exist too, or every
    subsequent authenticated call would 403. Idempotent - does nothing if
    a membership (active or not) already exists, so tests that
    deliberately pre-arrange an inactive/missing membership aren't
    clobbered."""
    existing = await web_api.partner_repository.get_membership(workspace_id, telegram_user_id)
    if existing is not None:
        return
    await web_api.partner_repository.create_membership(
        workspace_id, telegram_user_id, role=role, status="active",
    )


def login_as(
    client, web_api, workspace_id: int, telegram_user_id: int, *,
    email: str = "owner@example.com", password: str = DEFAULT_TEST_PASSWORD,
    role: str = "owner", ensure_membership: bool = True,
) -> str:
    """Issues a one-time invite for (workspace_id, telegram_user_id),
    registers through it, and leaves `client` holding a live session
    cookie plus a default X-CSRF-Token header - so existing
    client.post/put/delete(...) call sites in these tests don't need to
    change individually. Returns the raw CSRF token too, for tests that
    want to exercise the "missing/wrong CSRF header" path explicitly.

    ensure_membership=True (default) auto-grants an active membership for
    (workspace_id, telegram_user_id) if none exists yet - see
    _ensure_active_membership(). Pass False for tests that specifically
    want registration/login itself to fail because no valid membership
    exists (Task "web-auth fail-closed").

    IMPORTANT: the caller's TestClient must be constructed with
    base_url="https://testserver" - the session/CSRF cookies are Secure,
    and httpx's cookie jar (like a real browser) silently drops a Secure
    cookie set against a plain http:// origin.
    """
    from app.services.web_auth_tokens import generate_token, hash_token

    if ensure_membership:
        _run(_ensure_active_membership(web_api, workspace_id, telegram_user_id, role))

    raw_invite = generate_token()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=72)).isoformat()
    _run(web_api.web_auth_repository.create_invite(
        workspace_id, telegram_user_id, hash_token(raw_invite), expires_at,
    ))

    response = client.post("/api/auth/register", json={
        "invite_token": raw_invite, "email": email, "password": password,
    })
    body = response.json()
    assert response.status_code == 200 and "error" not in body, body

    csrf = client.cookies.get("ta_csrf")
    assert csrf, "expected a ta_csrf cookie after registration"
    client.headers["X-CSRF-Token"] = csrf
    return csrf
