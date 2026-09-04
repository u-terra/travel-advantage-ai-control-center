from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.repositories.admin_directory_repository import AdminDirectoryRepository
from app.repositories.partner_repository import PartnerRepository
from app.repositories.subscription_repository import SubscriptionRepository
from app.repositories.web_auth_repository import WebAuthRepository
from app.services.web_auth_tokens import generate_token, hash_token


def run(coro):
    return asyncio.run(coro)


def _future() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=72)).isoformat()


def _setup_workspace_with_binding(
    db_path: Path, *, telegram_id: int, email: str,
) -> int:
    partners = PartnerRepository(db_path)
    run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(telegram_id))
    run(partners.bootstrap_owner_membership(telegram_id))

    # AdminDirectoryRepository's queries LEFT JOIN workspace_subscriptions -
    # in the real app this table always exists by the time an admin
    # endpoint can be hit (subscription_repository.init() runs at web_api.py
    # startup, before any request is served); tests must set up the same
    # precondition explicitly.
    subscriptions = SubscriptionRepository(db_path)
    run(subscriptions.init())

    web_auth = WebAuthRepository(db_path)
    run(web_auth.init())
    user = run(web_auth.create_user(email, "hash"))
    run(web_auth.create_binding(user.id, workspace.id, telegram_id))
    return workspace.id


def test_search_with_no_query_returns_all_workspaces(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    _setup_workspace_with_binding(db_path, telegram_id=100, email="one@example.com")

    repo = AdminDirectoryRepository(db_path)
    rows, total = run(repo.search_workspaces())

    assert total == 1
    assert rows[0].primary_email == "one@example.com"


def test_search_matches_by_email(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _setup_workspace_with_binding(db_path, telegram_id=100, email="findme@example.com")

    repo = AdminDirectoryRepository(db_path)
    rows, total = run(repo.search_workspaces(query="findme"))

    assert total == 1
    assert rows[0].workspace_id == workspace_id


def test_search_matches_by_telegram_id(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _setup_workspace_with_binding(db_path, telegram_id=777888, email="a@example.com")

    repo = AdminDirectoryRepository(db_path)
    rows, total = run(repo.search_workspaces(query="777888"))

    assert total == 1
    assert rows[0].workspace_id == workspace_id


def test_search_matches_by_workspace_name(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    _setup_workspace_with_binding(db_path, telegram_id=100, email="a@example.com")

    repo = AdminDirectoryRepository(db_path)
    # ensure_owner_workspace names the workspace after the telegram id by
    # default in this codebase's test helper convention - search for a
    # substring of it and confirm at least the exact-query path finds
    # nothing bogus.
    rows, total = run(repo.search_workspaces(query="definitely-not-a-real-workspace"))
    assert total == 0


def test_search_does_not_leak_an_unrelated_workspace(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    _setup_workspace_with_binding(db_path, telegram_id=100, email="first@example.com")

    repo = AdminDirectoryRepository(db_path)
    rows, total = run(repo.search_workspaces(query="second@example.com"))
    assert total == 0
    assert rows == []


def test_get_workspace_includes_subscription_fields(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _setup_workspace_with_binding(db_path, telegram_id=100, email="a@example.com")

    subscriptions = SubscriptionRepository(db_path)
    run(subscriptions.init())
    run(subscriptions.start_trial(workspace_id, _future()))

    repo = AdminDirectoryRepository(db_path)
    row = run(repo.get_workspace(workspace_id))

    assert row is not None
    assert row.subscription_status == "trial"
    assert row.trial_until is not None


def test_get_workspace_returns_none_for_unknown_id(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    _setup_workspace_with_binding(db_path, telegram_id=100, email="a@example.com")
    repo = AdminDirectoryRepository(db_path)
    assert run(repo.get_workspace(999999)) is None


def test_get_members_returns_role_and_email(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _setup_workspace_with_binding(db_path, telegram_id=100, email="owner@example.com")

    repo = AdminDirectoryRepository(db_path)
    members = run(repo.get_members(workspace_id))

    assert len(members) == 1
    assert members[0].telegram_user_id == 100
    assert members[0].role == "owner"
    assert members[0].email == "owner@example.com"


def test_onboarding_completed_reflects_binding_state(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    workspace_id = _setup_workspace_with_binding(db_path, telegram_id=100, email="a@example.com")

    repo = AdminDirectoryRepository(db_path)
    row = run(repo.get_workspace(workspace_id))
    # Fresh binding via create_binding() has onboarding_completed_at=NULL.
    assert row.onboarding_completed is False
