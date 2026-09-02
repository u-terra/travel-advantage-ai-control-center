"""POST /api/auth/register, POST /api/auth/login, POST /api/auth/logout,
GET /api/auth/me - the cookie-session web-auth layer that replaces the
hardcoded WEB_WORKSPACE_ID/WEB_TELEGRAM_USER_ID constants.

A web-account is a compatibility layer over the existing
PartnerRepository access model (see app.domain.web_auth) - registration
only succeeds against a real, admin-issued invite bound to an existing
(workspace_id, telegram_user_id) pair.

Cross-cutting security properties are covered here where they concern the
auth endpoints themselves (password/token storage, invite one-time-use,
generic login errors, cookie flags, CSRF, session fixation). Isolation
tests for the now-AuthContext-scoped resource endpoints (conversations,
materials, competitors, profile, ...) live in
tests/test_web_api_auth_integration.py once those endpoints are wired.

Requires the web-only dependencies (requirements-web.txt: fastapi,
uvicorn, markdown, argon2-cffi). Skips cleanly when they're not installed.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("markdown")
pytest.importorskip("argon2")

from fastapi.testclient import TestClient  # noqa: E402

STRONG_PASSWORD = "correcthorsebattery"
OWNER_ID = 586249067


def _run(coro):
    return asyncio.run(coro)


def _future(hours: float = 72) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _past(hours: float = 1) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


@pytest.fixture
def api(tmp_path, monkeypatch):
    db_path = tmp_path / "journal.sqlite3"
    monkeypatch.setenv("JOURNAL_DB_PATH", str(db_path))
    monkeypatch.setenv("PLANNER_OPENAI_API_KEY", "test-key")

    import sys
    sys.modules.pop("app.web_api", None)
    import app.web_api as web_api

    # base_url must be https:// - the session/CSRF cookies are Secure, and
    # httpx's cookie jar (like a real browser) will not send a Secure
    # cookie back on subsequent requests against a plain http:// origin.
    with TestClient(web_api.app, base_url="https://testserver") as client:
        ws, _ = _run(web_api.partner_repository.ensure_owner_workspace(
            OWNER_ID,
        ))
        # Registration/login now fail closed against PartnerRepository's
        # own access model on every request (see get_current_principal()) -
        # ensure_owner_workspace() alone doesn't create a
        # workspace_memberships row, bootstrap_owner_membership() does,
        # exactly like the real bot process does at startup.
        _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
        yield client, web_api, ws.id

    sys.modules.pop("app.web_api", None)


def _create_invite(web_api, workspace_id, telegram_user_id=None, **kwargs):
    from app.services.web_auth_tokens import generate_token, hash_token

    telegram_user_id = telegram_user_id or OWNER_ID
    raw_token = generate_token()
    expires_at = kwargs.pop("expires_at", _future())
    _run(web_api.web_auth_repository.create_invite(
        workspace_id, telegram_user_id, hash_token(raw_token), expires_at, **kwargs,
    ))
    return raw_token


def _csrf(client) -> str:
    return client.cookies.get("ta_csrf")


# ── register ─────────────────────────────────────────────────────────────

def test_register_with_valid_invite_creates_session(api) -> None:
    client, web_api, workspace_id = api
    raw_invite = _create_invite(web_api, workspace_id)

    response = client.post("/api/auth/register", json={
        "invite_token": raw_invite, "email": "partner@example.com",
        "password": STRONG_PASSWORD,
    })

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "partner@example.com"
    assert body["workspace_id"] == workspace_id
    assert "ta_session" in client.cookies
    assert "ta_csrf" in client.cookies


def test_register_rejects_unknown_invite_token(api) -> None:
    client, _, _ = api

    response = client.post("/api/auth/register", json={
        "invite_token": "not-a-real-token", "email": "partner@example.com",
        "password": STRONG_PASSWORD,
    })

    assert response.status_code == 200
    assert "error" in response.json()
    assert "ta_session" not in client.cookies


def test_register_rejects_expired_invite(api) -> None:
    client, web_api, workspace_id = api
    raw_invite = _create_invite(web_api, workspace_id, expires_at=_past())

    response = client.post("/api/auth/register", json={
        "invite_token": raw_invite, "email": "partner@example.com",
        "password": STRONG_PASSWORD,
    })

    assert "error" in response.json()


def test_register_invite_is_one_time_use(api) -> None:
    client, web_api, workspace_id = api
    raw_invite = _create_invite(web_api, workspace_id)

    first = client.post("/api/auth/register", json={
        "invite_token": raw_invite, "email": "first@example.com",
        "password": STRONG_PASSWORD,
    })
    assert first.status_code == 200
    assert "error" not in first.json()

    second = client.post("/api/auth/register", json={
        "invite_token": raw_invite, "email": "second@example.com",
        "password": STRONG_PASSWORD,
    })
    assert "error" in second.json()


def test_register_enforces_minimum_password_length(api) -> None:
    client, web_api, workspace_id = api
    raw_invite = _create_invite(web_api, workspace_id)

    response = client.post("/api/auth/register", json={
        "invite_token": raw_invite, "email": "partner@example.com",
        "password": "short1",
    })

    assert "error" in response.json()
    assert "ta_session" not in client.cookies


def test_register_weak_password_does_not_burn_the_invite(api) -> None:
    """A client-side-fixable mistake (short password) shouldn't waste a
    one-time beta invite - only a used/expired/mismatched-email invite is
    non-recoverable."""
    client, web_api, workspace_id = api
    raw_invite = _create_invite(web_api, workspace_id)

    weak = client.post("/api/auth/register", json={
        "invite_token": raw_invite, "email": "partner@example.com",
        "password": "short1",
    })
    assert "error" in weak.json()

    retry = client.post("/api/auth/register", json={
        "invite_token": raw_invite, "email": "partner@example.com",
        "password": STRONG_PASSWORD,
    })
    assert retry.status_code == 200
    assert "error" not in retry.json()


def test_register_rejects_email_restricted_invite_for_wrong_email(api) -> None:
    client, web_api, workspace_id = api
    raw_invite = _create_invite(
        web_api, workspace_id, email_restriction="only-me@example.com",
    )

    response = client.post("/api/auth/register", json={
        "invite_token": raw_invite, "email": "someone-else@example.com",
        "password": STRONG_PASSWORD,
    })

    assert "error" in response.json()
    assert "ta_session" not in client.cookies


def test_register_accepts_email_restricted_invite_for_matching_email(api) -> None:
    client, web_api, workspace_id = api
    raw_invite = _create_invite(
        web_api, workspace_id, email_restriction="Only-Me@Example.com",
    )

    response = client.post("/api/auth/register", json={
        "invite_token": raw_invite, "email": "only-me@example.com",
        "password": STRONG_PASSWORD,
    })

    assert response.status_code == 200
    assert "error" not in response.json()


def test_register_rejects_already_registered_email(api) -> None:
    client, web_api, workspace_id = api
    raw_invite_1 = _create_invite(web_api, workspace_id)
    client.post("/api/auth/register", json={
        "invite_token": raw_invite_1, "email": "partner@example.com",
        "password": STRONG_PASSWORD,
    })
    client.cookies.clear()

    raw_invite_2 = _create_invite(web_api, workspace_id)
    response = client.post("/api/auth/register", json={
        "invite_token": raw_invite_2, "email": "Partner@Example.com",
        "password": STRONG_PASSWORD,
    })

    assert "error" in response.json()


def test_register_password_is_stored_as_argon2_hash_never_plaintext(api) -> None:
    client, web_api, workspace_id = api
    raw_invite = _create_invite(web_api, workspace_id)

    client.post("/api/auth/register", json={
        "invite_token": raw_invite, "email": "partner@example.com",
        "password": STRONG_PASSWORD,
    })

    user = _run(web_api.web_auth_repository.get_user_by_email("partner@example.com"))
    assert user.password_hash != STRONG_PASSWORD
    assert user.password_hash.startswith("$argon2id$")


def test_register_response_never_contains_password_or_hash(api) -> None:
    client, web_api, workspace_id = api
    raw_invite = _create_invite(web_api, workspace_id)

    response = client.post("/api/auth/register", json={
        "invite_token": raw_invite, "email": "partner@example.com",
        "password": STRONG_PASSWORD,
    })

    body_text = response.text
    assert STRONG_PASSWORD not in body_text
    assert "argon2" not in body_text
    assert "password" not in body_text.lower()


# ── login ────────────────────────────────────────────────────────────────

def _register(client, web_api, workspace_id, email="partner@example.com", password=STRONG_PASSWORD):
    raw_invite = _create_invite(web_api, workspace_id)
    client.post("/api/auth/register", json={
        "invite_token": raw_invite, "email": email, "password": password,
    })
    client.cookies.clear()


def test_login_with_correct_credentials_succeeds(api) -> None:
    client, web_api, workspace_id = api
    _register(client, web_api, workspace_id)

    response = client.post("/api/auth/login", json={
        "email": "partner@example.com", "password": STRONG_PASSWORD,
    })

    assert response.status_code == 200
    assert response.json()["email"] == "partner@example.com"
    assert "ta_session" in client.cookies


def test_login_wrong_password_is_generic_and_no_cookie(api) -> None:
    client, web_api, workspace_id = api
    _register(client, web_api, workspace_id)

    response = client.post("/api/auth/login", json={
        "email": "partner@example.com", "password": "wrong-password-entirely",
    })

    assert response.status_code == 200
    assert response.json() == {"error": "Неверный email или пароль."}
    assert "ta_session" not in client.cookies


def test_login_nonexistent_user_gives_identical_generic_error(api) -> None:
    """No user-enumeration oracle: same message for "no such account" and
    "wrong password"."""
    client, web_api, workspace_id = api
    _register(client, web_api, workspace_id)

    wrong_password = client.post("/api/auth/login", json={
        "email": "partner@example.com", "password": "wrong-password-entirely",
    }).json()
    no_such_user = client.post("/api/auth/login", json={
        "email": "nobody-registered@example.com", "password": "wrong-password-entirely",
    }).json()

    assert wrong_password == no_such_user == {"error": "Неверный email или пароль."}


def test_login_creates_a_fresh_session_each_time(api) -> None:
    """Session-fixation defense: login always mints a brand-new session
    token, never reuses one."""
    client, web_api, workspace_id = api
    _register(client, web_api, workspace_id)

    client.post("/api/auth/login", json={
        "email": "partner@example.com", "password": STRONG_PASSWORD,
    })
    first_session = client.cookies.get("ta_session")

    client.post("/api/auth/login", json={
        "email": "partner@example.com", "password": STRONG_PASSWORD,
    })
    second_session = client.cookies.get("ta_session")

    assert first_session != second_session


def test_login_sets_secure_httponly_samesite_cookie_flags(api) -> None:
    client, web_api, workspace_id = api
    _register(client, web_api, workspace_id)

    response = client.post("/api/auth/login", json={
        "email": "partner@example.com", "password": STRONG_PASSWORD,
    })

    session_cookie_header = next(
        v for k, v in response.headers.multi_items() if k.lower() == "set-cookie"
        and v.startswith("ta_session=")
    )
    assert "HttpOnly" in session_cookie_header
    assert "Secure" in session_cookie_header
    assert "SameSite=lax" in session_cookie_header
    assert "Path=/" in session_cookie_header

    csrf_cookie_header = next(
        v for k, v in response.headers.multi_items() if k.lower() == "set-cookie"
        and v.startswith("ta_csrf=")
    )
    assert "HttpOnly" not in csrf_cookie_header
    assert "Secure" in csrf_cookie_header


# ── logout ───────────────────────────────────────────────────────────────

def test_logout_requires_csrf_header(api) -> None:
    client, web_api, workspace_id = api
    _register(client, web_api, workspace_id)
    client.post("/api/auth/login", json={
        "email": "partner@example.com", "password": STRONG_PASSWORD,
    })

    response = client.post("/api/auth/logout")

    assert response.status_code == 403
    assert "ta_session" in client.cookies  # not cleared


def test_logout_revokes_session_and_clears_cookies(api) -> None:
    client, web_api, workspace_id = api
    _register(client, web_api, workspace_id)
    client.post("/api/auth/login", json={
        "email": "partner@example.com", "password": STRONG_PASSWORD,
    })

    response = client.post("/api/auth/logout", headers={"X-CSRF-Token": _csrf(client)})

    assert response.status_code == 200
    assert response.json() == {"logged_out": True}
    assert "ta_session" not in client.cookies
    assert "ta_csrf" not in client.cookies


def test_revoked_session_cannot_be_used_again(api) -> None:
    client, web_api, workspace_id = api
    _register(client, web_api, workspace_id)
    client.post("/api/auth/login", json={
        "email": "partner@example.com", "password": STRONG_PASSWORD,
    })
    session_token = client.cookies.get("ta_session")
    csrf_token = _csrf(client)

    client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf_token})

    # replay the old (now-revoked) cookie manually.
    client.cookies.set("ta_session", session_token)
    response = client.get("/api/auth/me")

    assert response.status_code == 401


def test_logout_without_prior_session_still_requires_auth(api) -> None:
    client, _, _ = api
    response = client.post("/api/auth/logout", headers={"X-CSRF-Token": "whatever"})
    assert response.status_code == 401


# ── me ───────────────────────────────────────────────────────────────────

def test_me_without_session_returns_401_json(api) -> None:
    client, _, _ = api
    response = client.get("/api/auth/me")

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")


def test_me_with_valid_session_returns_identity(api) -> None:
    client, web_api, workspace_id = api
    _register(client, web_api, workspace_id)
    client.post("/api/auth/login", json={
        "email": "partner@example.com", "password": STRONG_PASSWORD,
    })

    response = client.get("/api/auth/me")

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "partner@example.com"
    assert body["workspace_id"] == workspace_id
    assert "role" in body


def test_me_never_exposes_secrets_or_tokens(api) -> None:
    client, web_api, workspace_id = api
    _register(client, web_api, workspace_id)
    client.post("/api/auth/login", json={
        "email": "partner@example.com", "password": STRONG_PASSWORD,
    })

    response = client.get("/api/auth/me")
    body_text = response.text

    assert "password" not in body_text.lower()
    assert "hash" not in body_text.lower()
    assert "csrf" not in body_text.lower()
    assert "session" not in body_text.lower()
    assert "telegram" not in body_text.lower()


def test_expired_session_is_rejected(api) -> None:
    client, web_api, workspace_id = api
    _register(client, web_api, workspace_id)
    client.post("/api/auth/login", json={
        "email": "partner@example.com", "password": STRONG_PASSWORD,
    })

    from app.services.web_auth_tokens import hash_token
    import aiosqlite

    session_token = client.cookies.get("ta_session")

    async def expire_it():
        db_path = web_api.settings.journal_db_path
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "UPDATE web_auth_sessions SET expires_at = '2000-01-01T00:00:00+00:00' "
                "WHERE session_token_hash = ?",
                (hash_token(session_token),),
            )
            await db.commit()

    _run(expire_it())

    response = client.get("/api/auth/me")
    assert response.status_code == 401
