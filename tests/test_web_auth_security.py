"""Cross-cutting web-auth security properties that don't belong to any
single endpoint's own test file - the explicit checklist for this
milestone:

- CSRF is enforced on every mutating endpoint, not just /api/auth/logout
- a client cannot override its own identity by putting workspace_id/
  telegram_user_id in a request body
- the email displayed in the UI is XSS-safe (bound as data, not markup)
- a second, real, independently-authenticated session cannot read or
  mutate another account's materials
- no endpoint response ever contains a secret-looking field

Everything else on the checklist (password/session/invite hashing,
one-time invites, expiry, generic login errors, cookie flags, logout
revocation) is already covered by tests/test_web_auth_repository.py,
tests/test_web_api_auth.py, and tests/test_create_beta_invite_script.py.

Requires the web-only dependencies. Skips cleanly when they're not
installed.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("markdown")
pytest.importorskip("argon2")

from fastapi.testclient import TestClient  # noqa: E402

from tests._web_auth_test_helpers import login_as  # noqa: E402

OWNER_ID = 586249067
STRONG_PASSWORD = "correcthorsebattery"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def api(tmp_path, monkeypatch):
    db_path = tmp_path / "journal.sqlite3"
    monkeypatch.setenv("JOURNAL_DB_PATH", str(db_path))
    monkeypatch.setenv("PLANNER_OPENAI_API_KEY", "test-key")

    import sys
    sys.modules.pop("app.web_api", None)
    import app.web_api as web_api

    with TestClient(web_api.app, base_url="https://testserver") as client:
        ws, _ = _run(web_api.partner_repository.ensure_owner_workspace(OWNER_ID))
        login_as(client, web_api, ws.id, OWNER_ID)
        yield client, web_api, ws.id

    sys.modules.pop("app.web_api", None)


# ── CSRF is enforced everywhere, not just logout ─────────────────────────

def test_create_conversation_requires_csrf(api) -> None:
    client, _, _ = api
    token = client.headers.pop("X-CSRF-Token")
    try:
        response = client.post("/api/conversations")
    finally:
        client.headers["X-CSRF-Token"] = token
    assert response.status_code == 403


def test_chat_requires_csrf(api) -> None:
    client, _, _ = api
    conversation_id = client.post("/api/conversations").json()["conversation"]["id"]

    token = client.headers.pop("X-CSRF-Token")
    try:
        response = client.post(
            "/api/chat", json={"message": "Привет", "conversation_id": conversation_id},
        )
    finally:
        client.headers["X-CSRF-Token"] = token
    assert response.status_code == 403


def test_material_edit_requires_csrf(api) -> None:
    client, web_api, workspace_id = api
    artifact, version = _run(web_api.artifact_repository.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост", content="Текст.",
    ))

    token = client.headers.pop("X-CSRF-Token")
    try:
        response = client.put(
            f"/api/materials/{artifact.id}",
            json={"content": "Взлом без CSRF", "expected_version_id": version.id},
        )
    finally:
        client.headers["X-CSRF-Token"] = token
    assert response.status_code == 403

    current = _run(web_api.artifact_repository.get_current_artifact_version(
        workspace_id, artifact.id,
    ))
    assert current.content == "Текст."


def test_material_delete_requires_csrf(api) -> None:
    client, web_api, workspace_id = api
    artifact, _ = _run(web_api.artifact_repository.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост", content="Текст.",
    ))

    token = client.headers.pop("X-CSRF-Token")
    try:
        response = client.delete(f"/api/materials/{artifact.id}")
    finally:
        client.headers["X-CSRF-Token"] = token
    assert response.status_code == 403
    assert _run(web_api.artifact_repository.get_artifact(workspace_id, artifact.id)) is not None


def test_profile_business_update_requires_csrf(api) -> None:
    client, web_api, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))

    token = client.headers.pop("X-CSRF-Token")
    try:
        response = client.put("/api/profile/business", json={
            "business_name": "Захват", "business_type": "agency",
            "short_description": "x",
        })
    finally:
        client.headers["X-CSRF-Token"] = token
    assert response.status_code == 403


def test_wrong_csrf_token_is_also_rejected(api) -> None:
    """Not just missing - a value that doesn't match this session's stored
    hash must be rejected too (rules out a naive 'header present' check)."""
    client, _, _ = api
    token = client.headers["X-CSRF-Token"]
    client.headers["X-CSRF-Token"] = "not-the-real-token"
    try:
        response = client.post("/api/conversations")
    finally:
        client.headers["X-CSRF-Token"] = token
    assert response.status_code == 403


# ── client cannot override its own identity ──────────────────────────────

def test_chat_ignores_client_supplied_workspace_and_user_ids(api) -> None:
    client, web_api, workspace_id = api
    conversation_id = client.post("/api/conversations").json()["conversation"]["id"]

    def fake_generate(**kwargs):
        from app.chat_provider import ChatResult
        return ChatResult(text="ok", usage=None)

    web_api.chat_provider.generate = fake_generate

    response = client.post("/api/chat", json={
        "message": "Привет",
        "conversation_id": conversation_id,
        "workspace_id": 999999,
        "telegram_user_id": 999999,
    })

    # extra JSON fields the Pydantic model doesn't declare are simply
    # ignored - identity still resolves from the session, not the body.
    assert response.status_code == 200
    assert "error" not in response.json()

    messages = _run(web_api.web_conversation_repository.list_messages(
        workspace_id, OWNER_ID, conversation_id,
    ))
    assert len(messages) == 2  # saved under the REAL session identity


def test_material_update_ignores_client_supplied_workspace_id(api) -> None:
    client, web_api, workspace_id = api
    artifact, version = _run(web_api.artifact_repository.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост", content="Текст.",
    ))

    response = client.put(
        f"/api/materials/{artifact.id}",
        json={
            "content": "Новый текст", "expected_version_id": version.id,
            "workspace_id": 999999,
        },
    )

    assert response.status_code == 200
    assert "error" not in response.json()
    current = _run(web_api.artifact_repository.get_current_artifact_version(
        workspace_id, artifact.id,
    ))
    assert current.content == "Новый текст"


# ── a second, real, independent session cannot touch this account's data ──

def test_second_real_session_cannot_edit_or_delete_foreign_materials(api) -> None:
    client, web_api, workspace_id = api
    artifact, version = _run(web_api.artifact_repository.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост", content="Оригинал.",
    ))

    other = _run(web_api.partner_repository.provision_partner(
        222335000, "Other Agency", "other-agency-security-test",
        business_name="Other Agency", business_type="independent_agent",
        short_description="Другое рабочее пространство.", context={},
    ))

    with TestClient(web_api.app, base_url="https://testserver") as other_client:
        login_as(
            other_client, web_api, other.workspace.id, 222335000,
            email="intruder@example.com",
        )

        edit_response = other_client.put(
            f"/api/materials/{artifact.id}",
            json={"content": "Взлом", "expected_version_id": version.id},
        )
        assert "error" in edit_response.json()

        delete_response = other_client.delete(f"/api/materials/{artifact.id}")
        assert delete_response.json()["deleted"] is False

    current = _run(web_api.artifact_repository.get_current_artifact_version(
        workspace_id, artifact.id,
    ))
    assert current.content == "Оригинал."


# ── XSS: email is bound as data (textContent), never markup ─────────────

CHAT_HTML = Path(__file__).resolve().parent.parent / "app" / "templates" / "chat.html"
node = shutil.which("node")
_XSS_PAYLOAD = "<img src=x onerror=alert(1)><script>alert(2)</script>"


def test_api_me_returns_a_payload_looking_email_verbatim_as_json_data(tmp_path, monkeypatch) -> None:
    """RegisterRequest.email is a plain str (no format validation) - the
    API layer must not attempt its own HTML-escaping on it either, it must
    come back unmodified as JSON data. Sanitization is entirely the
    client's job (textContent) - proven separately below - defense in
    depth means proving each half independently."""
    db_path = tmp_path / "journal.sqlite3"
    monkeypatch.setenv("JOURNAL_DB_PATH", str(db_path))
    monkeypatch.setenv("PLANNER_OPENAI_API_KEY", "test-key")

    import sys
    sys.modules.pop("app.web_api", None)
    import app.web_api as web_api

    payload_email = _XSS_PAYLOAD + "@example.com"

    with TestClient(web_api.app, base_url="https://testserver") as client:
        ws, _ = _run(web_api.partner_repository.ensure_owner_workspace(OWNER_ID))
        _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
        raw_invite = _run(_issue_invite(web_api, ws.id, OWNER_ID))

        register_response = client.post("/api/auth/register", json={
            "invite_token": raw_invite, "email": payload_email,
            "password": STRONG_PASSWORD,
        })
        assert "error" not in register_response.json()

        me_response = client.get("/api/auth/me")
        assert me_response.json()["email"] == payload_email.lower()

    sys.modules.pop("app.web_api", None)


async def _issue_invite(web_api, workspace_id, telegram_user_id):
    from app.services.web_auth_tokens import generate_token, hash_token
    from datetime import datetime, timedelta, timezone

    raw_token = generate_token()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=72)).isoformat()
    await web_api.web_auth_repository.create_invite(
        workspace_id, telegram_user_id, hash_token(raw_token), expires_at,
    )
    return raw_token


@pytest.mark.skipif(node is None, reason="node is not available on PATH")
def test_footer_email_is_bound_via_textcontent_not_innerhtml() -> None:
    source = CHAT_HTML.read_text(encoding="utf-8")
    match = re.search(r"<script>(.*)</script>", source, re.S)
    script = match.group(1)

    start = script.index("async function loadCurrentUser(")
    brace_start = script.index("{", start)
    depth = 0
    end = brace_start
    for index in range(brace_start, len(script)):
        if script[index] == "{":
            depth += 1
        elif script[index] == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    load_current_user = script[start:end]

    assert "footerEmail.textContent" in load_current_user
    assert "innerHTML" not in load_current_user

    node_script = f"""
class FakeElement {{
    constructor() {{ this._text = ""; }}
    set textContent(value) {{ this._text = value; }}
    get textContent() {{ return this._text; }}
}}
const footerEmail = new FakeElement();
global.fetch = async () => ({{
    ok: true,
    json: async () => ({{ email: {json.dumps(_XSS_PAYLOAD)} }}),
}});
{load_current_user}
loadCurrentUser().then(() => {{ console.log(JSON.stringify(footerEmail.textContent)); }});
"""
    result = subprocess.run(
        [node, "-e", node_script], capture_output=True, text=True, timeout=30,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout.strip())
    # the payload is present as plain text (proving it's real data) but
    # was never assigned through innerHTML/markup.
    assert rendered == _XSS_PAYLOAD


# ── no endpoint ever leaks a secret-looking field ─────────────────────────

_FORBIDDEN_SUBSTRINGS = ("password", "argon2", "session_token", "csrf_token_hash", "token_hash")


def test_no_endpoint_response_contains_secret_looking_fields(api, monkeypatch) -> None:
    client, web_api, workspace_id = api

    def fake_generate(**kwargs):
        from app.chat_provider import ChatResult
        return ChatResult(text="ok", usage=None)

    monkeypatch.setattr(web_api.chat_provider, "generate", fake_generate)

    conversation_id = client.post("/api/conversations").json()["conversation"]["id"]
    client.post(
        "/api/chat", json={"message": "Привет", "conversation_id": conversation_id},
    )

    responses = [
        client.get("/api/auth/me"),
        client.get("/api/conversations"),
        client.get(f"/api/conversations/{conversation_id}/messages"),
        client.get("/api/profile"),
        client.get("/api/materials"),
        client.get("/api/competitors"),
    ]

    for response in responses:
        lowered = response.text.lower()
        for forbidden in _FORBIDDEN_SUBSTRINGS:
            assert forbidden not in lowered, f"{forbidden!r} leaked in {response.url}"


# ── fail-closed: membership/workspace state is re-checked every request ──
#
# A web-auth binding (app.domain.web_auth.WebAuthBinding) only records
# "this account claims to act as this (workspace_id, telegram_user_id)
# pair" - it is never itself proof of access. get_current_principal() re-
# verifies against PartnerRepository's own access model
# (resolve_workspace_context) on every single request, so a membership or
# workspace that stops being valid AFTER a session was created must
# immediately cut that session off - not just for the one endpoint that
# happened to already have its own internal check.

def test_membership_deactivated_after_login_blocks_further_access(api) -> None:
    client, web_api, workspace_id = api
    # sanity: the session works before revocation.
    assert client.get("/api/conversations").status_code == 200

    _run(web_api.partner_repository.set_partner_membership_status(OWNER_ID, "inactive"))

    response = client.get("/api/conversations")
    assert response.status_code == 403


def test_membership_deleted_after_login_does_not_fall_back_to_member_role(api) -> None:
    """No membership row at all (not just inactive) must deny access too -
    proves there's no silent role='member' default keeping the request
    alive when resolve_workspace_context() finds nothing."""
    client, web_api, workspace_id = api
    assert client.get("/api/conversations").status_code == 200

    _run(_delete_membership(web_api, workspace_id, OWNER_ID))

    response = client.get("/api/conversations")
    assert response.status_code == 403

    # the endpoint must not have been reached at all - no partial success.
    assert "conversations" not in response.json()


async def _delete_membership(web_api, workspace_id: int, telegram_user_id: int) -> None:
    import aiosqlite

    async with aiosqlite.connect(web_api.settings.journal_db_path) as db:
        await db.execute(
            "DELETE FROM workspace_memberships WHERE workspace_id = ? AND telegram_user_id = ?",
            (workspace_id, telegram_user_id),
        )
        await db.commit()


def test_workspace_suspended_after_login_blocks_access(api) -> None:
    """resolve_workspace_context() already refuses a non-active workspace
    internally - this proves that refusal actually reaches
    get_current_principal() and denies the request, not just the read
    path inside PartnerRepository itself."""
    client, web_api, workspace_id = api
    assert client.get("/api/conversations").status_code == 200

    _run(_set_workspace_status(web_api, workspace_id, "inactive"))

    response = client.get("/api/conversations")
    assert response.status_code == 403


async def _set_workspace_status(web_api, workspace_id: int, status: str) -> None:
    import aiosqlite

    async with aiosqlite.connect(web_api.settings.journal_db_path) as db:
        await db.execute(
            "UPDATE partner_workspaces SET status = ? WHERE id = ?", (status, workspace_id),
        )
        await db.commit()


def test_broken_membership_check_denies_access_not_500(api, monkeypatch) -> None:
    client, web_api, workspace_id = api

    async def broken_resolve(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(web_api.partner_repository, "resolve_workspace_context", broken_resolve)

    response = client.get("/api/conversations")
    assert response.status_code == 403


# ── beta invite must bind only to a real, currently-valid identity ──────

def test_cli_invite_creation_rejects_pair_without_active_membership(tmp_path, monkeypatch) -> None:
    """scripts/create_beta_invite.py must refuse to issue an invite for a
    (workspace_id, telegram_user_id) pair PartnerRepository doesn't
    recognize as an active member - not just a nonexistent workspace_id."""
    db_path = tmp_path / "journal.sqlite3"
    monkeypatch.setenv("JOURNAL_DB_PATH", str(db_path))

    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.modules.pop("scripts.create_beta_invite", None)
    from scripts.create_beta_invite import _create_invite

    from app.repositories.partner_repository import PartnerRepository
    partner_repository = PartnerRepository(db_path)
    _run(partner_repository.init())
    ws, _ = _run(partner_repository.ensure_owner_workspace(OWNER_ID))
    # Deliberately NOT calling bootstrap_owner_membership() - no active
    # membership exists for OWNER_ID yet.

    exit_code = _run(_create_invite(
        ws.id, OWNER_ID, email=None, ttl_hours=72, base_url="http://localhost:8000",
    ))

    assert exit_code == 1

    import sqlite3
    con = sqlite3.connect(db_path)
    tables = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='web_auth_invites'"
    ).fetchall()
    if tables:
        count = con.execute("SELECT COUNT(*) FROM web_auth_invites").fetchone()[0]
        assert count == 0


def test_cli_invite_creation_rejects_arbitrary_unrelated_pair(tmp_path, monkeypatch) -> None:
    """A workspace that exists, paired with a telegram_user_id that has
    nothing to do with it, must also be refused - the pair as a whole has
    to match a real active membership, not just each half independently."""
    db_path = tmp_path / "journal.sqlite3"
    monkeypatch.setenv("JOURNAL_DB_PATH", str(db_path))

    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.modules.pop("scripts.create_beta_invite", None)
    from scripts.create_beta_invite import _create_invite

    from app.repositories.partner_repository import PartnerRepository
    partner_repository = PartnerRepository(db_path)
    _run(partner_repository.init())
    ws, _ = _run(partner_repository.ensure_owner_workspace(OWNER_ID))
    _run(partner_repository.bootstrap_owner_membership(OWNER_ID))

    unrelated_telegram_id = 999999999
    exit_code = _run(_create_invite(
        ws.id, unrelated_telegram_id, email=None, ttl_hours=72,
        base_url="http://localhost:8000",
    ))

    assert exit_code == 1


def test_register_rejects_invite_whose_membership_became_inactive_before_use(api) -> None:
    """Time-of-check/time-of-use: an invite created while the pair was
    valid must still be re-checked at the moment it's actually used -
    membership could have been revoked any time in between."""
    client, web_api, workspace_id = api

    other_telegram_id = OWNER_ID + 500
    _run(web_api.partner_repository.create_membership(
        workspace_id, other_telegram_id, role="member", status="active",
    ))

    from app.services.web_auth_tokens import generate_token, hash_token
    from datetime import datetime, timedelta, timezone

    raw_invite = generate_token()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=72)).isoformat()
    _run(web_api.web_auth_repository.create_invite(
        workspace_id, other_telegram_id, hash_token(raw_invite), expires_at,
    ))

    # Membership revoked AFTER the invite was issued but BEFORE anyone
    # used it.
    _run(web_api.partner_repository.set_partner_membership_status(other_telegram_id, "inactive"))

    with TestClient(web_api.app, base_url="https://testserver") as fresh_client:
        response = fresh_client.post("/api/auth/register", json={
            "invite_token": raw_invite, "email": "late-joiner@example.com",
            "password": STRONG_PASSWORD,
        })

    assert "error" in response.json()
    assert _run(web_api.web_auth_repository.get_user_by_email("late-joiner@example.com")) is None


# ── CSRF token survives a full page reload (cookie, never localStorage) ──

def test_csrf_state_changing_request_works_after_simulated_reload(api) -> None:
    """A reload doesn't call /api/auth/login again - it's a fresh JS
    context that only has the browser's persistent cookie jar to go on.
    Simulated here with a brand-new TestClient (= fresh page/JS context)
    that gets ONLY the two cookies copied over (no test-only default
    header carried along) - exactly what a real browser hands a reloaded
    page. The CSRF value is read back out of the cookie, exactly like
    chat.html's fetch wrapper does via document.cookie, and manually
    attached as the header a real page would send automatically."""
    client, web_api, workspace_id = api
    session_token = client.cookies.get("ta_session")
    csrf_token = client.cookies.get("ta_csrf")
    assert session_token and csrf_token

    with TestClient(web_api.app, base_url="https://testserver") as reloaded_client:
        reloaded_client.cookies.set("ta_session", session_token)
        reloaded_client.cookies.set("ta_csrf", csrf_token)

        response = reloaded_client.post(
            "/api/conversations", headers={"X-CSRF-Token": csrf_token},
        )

    assert response.status_code == 200
    assert "error" not in response.json()


def test_csrf_cookie_is_never_httponly(api) -> None:
    """It MUST be JS-readable (document.cookie) - that's how the frontend
    re-derives it after a reload without any localStorage/sessionStorage
    involved. The session cookie, by contrast, must stay HttpOnly."""
    client, web_api, workspace_id = api

    # httpx's cookie jar doesn't expose per-cookie flags (HttpOnly/Secure)
    # after the fact - inspect the raw Set-Cookie header from a fresh
    # registration instead.
    raw_headers = _run(_last_set_cookie_headers(web_api, workspace_id))
    session_header = next(h for h in raw_headers if h.startswith("ta_session="))
    csrf_header = next(h for h in raw_headers if h.startswith("ta_csrf="))

    assert "HttpOnly" in session_header
    assert "HttpOnly" not in csrf_header


async def _last_set_cookie_headers(web_api, workspace_id):
    from app.services.web_auth_tokens import generate_token, hash_token
    from datetime import datetime, timedelta, timezone

    other_telegram_id = OWNER_ID + 900
    await web_api.partner_repository.create_membership(
        workspace_id, other_telegram_id, role="member", status="active",
    )
    raw_invite = generate_token()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=72)).isoformat()
    await web_api.web_auth_repository.create_invite(
        workspace_id, other_telegram_id, hash_token(raw_invite), expires_at,
    )

    with TestClient(web_api.app, base_url="https://testserver") as fresh_client:
        response = fresh_client.post("/api/auth/register", json={
            "invite_token": raw_invite, "email": "cookie-flags-check@example.com",
            "password": STRONG_PASSWORD,
        })
        return [v for k, v in response.headers.multi_items() if k.lower() == "set-cookie"]


@pytest.mark.skipif(node is None, reason="node is not available on PATH")
def test_frontend_never_touches_localstorage_or_sessionstorage_for_csrf() -> None:
    """The whole fetch wrapper + CSRF-attach logic in chat.html must never
    read/write localStorage or sessionStorage for the token - only the
    cookie (document.cookie), which is what actually survives reload
    safely without a client-side auth-secret store."""
    source = CHAT_HTML.read_text(encoding="utf-8")
    match = re.search(r"<script>(.*)</script>", source, re.S)
    script = match.group(1)

    start = script.index("(function installAuthFetch()")
    depth = 0
    end = start
    for index in range(start, len(script)):
        if script[index] == "(":
            depth += 1
        elif script[index] == ")":
            depth -= 1
            if depth == 0 and script[index + 1:index + 3] == "()":
                end = index + 3
                break
    wrapper_source = script[start:end]

    assert "localStorage" not in wrapper_source
    assert "sessionStorage" not in wrapper_source
    assert "document.cookie" in wrapper_source


@pytest.mark.skipif(node is None, reason="node is not available on PATH")
def test_csrf_cookie_reader_reflects_document_cookie_freshly_each_call() -> None:
    """Proves the token isn't cached anywhere in JS memory at page-load
    time - each mutating fetch re-reads document.cookie fresh, so a
    reload (which repopulates document.cookie from the browser's
    persistent jar) is picked up correctly with no stale in-memory copy."""
    source = CHAT_HTML.read_text(encoding="utf-8")
    match = re.search(r"<script>(.*)</script>", source, re.S)
    script = match.group(1)

    start = script.index("(function installAuthFetch()")
    depth = 0
    end = start
    for index in range(start, len(script)):
        if script[index] == "(":
            depth += 1
        elif script[index] == ")":
            depth -= 1
            if depth == 0 and script[index + 1:index + 3] == "()":
                end = index + 3
                break
    wrapper_source = script[start:end]

    node_script = f"""
let cookieValue = "ta_csrf=first-token-value";
const document = {{ get cookie() {{ return cookieValue; }} }};
let capturedHeaders = [];
const realFetchCalls = [];
global.window = {{
    fetch: async (url, opts) => {{
        realFetchCalls.push(opts);
        return {{ status: 200, ok: true, json: async () => ({{}}) }};
    }},
}};
{wrapper_source};

(async () => {{
    await window.fetch("/api/conversations", {{ method: "POST" }});
    capturedHeaders.push(realFetchCalls[0].headers.get("X-CSRF-Token"));

    // simulate a reload changing the cookie (e.g. a fresh login elsewhere)
    cookieValue = "ta_csrf=second-token-value-after-reload";
    await window.fetch("/api/conversations", {{ method: "POST" }});
    capturedHeaders.push(realFetchCalls[1].headers.get("X-CSRF-Token"));

    console.log(JSON.stringify(capturedHeaders));
}})();
"""
    result = subprocess.run(
        [node, "-e", node_script], capture_output=True, text=True, timeout=30,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    headers = json.loads(result.stdout.strip())
    assert headers == ["first-token-value", "second-token-value-after-reload"]
