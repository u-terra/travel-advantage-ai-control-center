"""scripts/create_beta_invite.py - the beta-bootstrap CLI: issues a
one-time registration invite for an existing (workspace_id,
telegram_user_id) pair. Verifies the printed output is the only place the
raw token ever appears - the database only ever gets its hash - and that
an unknown workspace_id fails cleanly instead of creating a dangling
invite.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import sys
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.create_beta_invite import _create_invite  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


OWNER_ID = 586249067


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "journal.sqlite3"
    monkeypatch.setenv("JOURNAL_DB_PATH", str(path))
    return path


def _bootstrap_workspace(path: Path) -> int:
    """ensure_owner_workspace() alone creates the workspace/profile but not
    a workspace_memberships row - the CLI now requires a real active
    membership to exist before it will issue an invite (fail-closed), so
    tests need bootstrap_owner_membership() too, exactly like the real bot
    process does at startup (app/main.py)."""
    from app.repositories.partner_repository import PartnerRepository

    repo = PartnerRepository(path)
    _run(repo.init())
    ws, _ = _run(repo.ensure_owner_workspace(OWNER_ID))
    _run(repo.bootstrap_owner_membership(OWNER_ID))
    return ws.id


def test_creates_invite_and_prints_url_once(db_path, capsys) -> None:
    workspace_id = _bootstrap_workspace(db_path)

    exit_code = _run(_create_invite(
        workspace_id, OWNER_ID, email=None, ttl_hours=72,
        base_url="http://localhost:8000",
    ))

    assert exit_code == 0
    printed = capsys.readouterr().out
    match = re.search(r"invite=([A-Za-z0-9_-]+)", printed)
    assert match is not None
    raw_token = match.group(1)
    assert f"http://localhost:8000/register?invite={raw_token}" in printed


def test_database_only_ever_stores_the_hash(db_path, capsys) -> None:
    workspace_id = _bootstrap_workspace(db_path)

    _run(_create_invite(
        workspace_id, OWNER_ID, email=None, ttl_hours=72,
        base_url="http://localhost:8000",
    ))
    printed = capsys.readouterr().out
    raw_token = re.search(r"invite=([A-Za-z0-9_-]+)", printed).group(1)

    con = sqlite3.connect(db_path)
    row = con.execute("SELECT token_hash FROM web_auth_invites").fetchone()
    assert row is not None
    assert row[0] != raw_token
    assert raw_token not in row[0]

    from app.services.web_auth_tokens import hash_token
    assert row[0] == hash_token(raw_token)


def test_email_restriction_is_stored_normalized(db_path, capsys) -> None:
    workspace_id = _bootstrap_workspace(db_path)

    _run(_create_invite(
        workspace_id, OWNER_ID, email="Owner@Example.com", ttl_hours=72,
        base_url="http://localhost:8000",
    ))

    con = sqlite3.connect(db_path)
    row = con.execute("SELECT email_restriction FROM web_auth_invites").fetchone()
    assert row[0] == "owner@example.com"


def test_unknown_workspace_id_fails_cleanly_without_creating_an_invite(
    db_path, capsys,
) -> None:
    from app.repositories.partner_repository import PartnerRepository

    _run(PartnerRepository(db_path).init())

    exit_code = _run(_create_invite(
        999999, OWNER_ID, email=None, ttl_hours=72, base_url="http://localhost:8000",
    ))

    assert exit_code == 1
    assert "не найден" in capsys.readouterr().out

    con = sqlite3.connect(db_path)
    tables = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='web_auth_invites'"
    ).fetchall()
    if tables:
        count = con.execute("SELECT COUNT(*) FROM web_auth_invites").fetchone()[0]
        assert count == 0


def test_generated_invite_can_actually_register(db_path, capsys, monkeypatch) -> None:
    """End-to-end: the URL this script prints must be a working invite
    against the real web_api registration endpoint."""
    workspace_id = _bootstrap_workspace(db_path)
    monkeypatch.setenv("PLANNER_OPENAI_API_KEY", "test-key")

    _run(_create_invite(
        workspace_id, OWNER_ID, email=None, ttl_hours=72,
        base_url="http://localhost:8000",
    ))
    printed = capsys.readouterr().out
    raw_token = re.search(r"invite=([A-Za-z0-9_-]+)", printed).group(1)

    import importlib
    sys.modules.pop("app.web_api", None)
    web_api = importlib.import_module("app.web_api")

    from fastapi.testclient import TestClient
    with TestClient(web_api.app, base_url="https://testserver") as client:
        response = client.post("/api/auth/register", json={
            "invite_token": raw_token, "email": "beta@example.com",
            "password": "correcthorsebattery",
        })
        body = response.json()
        assert response.status_code == 200
        assert "error" not in body
        assert body["workspace_id"] == workspace_id

    sys.modules.pop("app.web_api", None)
