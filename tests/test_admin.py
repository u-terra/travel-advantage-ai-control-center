from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

from app import admin
from app.repositories.partner_repository import PartnerRepository, empty_business_context


USER_ID = 123456789


def run(value):
    return asyncio.run(value)


def database(tmp_path: Path) -> Path:
    path = tmp_path / "pilot.sqlite3"
    run(PartnerRepository(path).init())
    return path


def profile_file(tmp_path: Path) -> Path:
    context = empty_business_context()
    context["specializations"] = ["travel_club"]
    context["audiences"] = ["families"]
    context["communication"]["tone"] = "calm"
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({
        "business_name": "Pilot Partner",
        "business_type": "club_partner",
        "short_description": "Travel Advantage partner.",
        "context": context,
    }), encoding="utf-8")
    return path


def provision_args(db_path: Path, profile_path: Path) -> list[str]:
    return [
        "--db-path", str(db_path), "provision-partner",
        "--telegram-user-id", str(USER_ID),
        "--workspace-name", "Pilot Workspace",
        "--workspace-slug", "pilot-workspace",
        "--profile-file", str(profile_path),
    ]


def test_cli_provision_show_deactivate_reactivate(tmp_path: Path, capsys) -> None:
    db_path = database(tmp_path)
    profile_path = profile_file(tmp_path)
    assert admin.main(provision_args(db_path, profile_path)) == 0
    provisioned = capsys.readouterr().out
    assert "Partner provisioned" in provisioned
    assert "TELEGRAM_ALLOWED_USER_IDS" in provisioned
    assert "restart" in provisioned

    show = ["--db-path", str(db_path), "show-partner", "--telegram-user-id", str(USER_ID)]
    assert admin.main(show) == 0
    output = capsys.readouterr().out
    for expected in (
        "workspace_slug: pilot-workspace", "membership_status: active",
        "profile_status: usable", "profile_revision: 1",
        "business_name: Pilot Partner", "business_type: club_partner",
    ):
        assert expected in output
    for forbidden in ("BOT_TOKEN", "api_key", "credentials", "provider"):
        assert forbidden not in output

    deactivate = [
        "--db-path", str(db_path), "deactivate-partner",
        "--telegram-user-id", str(USER_ID),
    ]
    reactivate = [
        "--db-path", str(db_path), "reactivate-partner",
        "--telegram-user-id", str(USER_ID),
    ]
    assert admin.main(deactivate) == 0
    assert "membership_status: inactive" in capsys.readouterr().out
    assert admin.main(deactivate) == 0
    capsys.readouterr()
    assert admin.main(reactivate) == 0
    assert "membership_status: active" in capsys.readouterr().out


def test_cli_exact_duplicate_is_idempotent(tmp_path: Path, capsys) -> None:
    db_path = database(tmp_path)
    args = provision_args(db_path, profile_file(tmp_path))
    assert admin.main(args) == 0
    capsys.readouterr()
    assert admin.main(args) == 0
    assert "already provisioned" in capsys.readouterr().out
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM partner_workspaces").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM workspace_memberships").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM partner_profiles").fetchone()[0] == 1


def test_malformed_profile_is_clean_error_without_db_changes(tmp_path: Path, capsys) -> None:
    db_path = database(tmp_path)
    malformed = tmp_path / "bad.json"
    malformed.write_text("{", encoding="utf-8")
    assert admin.main(provision_args(db_path, malformed)) == 2
    error = capsys.readouterr().err
    assert error.startswith("ERROR:") and "profile JSON" in error
    assert "Traceback" not in error
    with sqlite3.connect(db_path) as db:
        for table in ("partner_workspaces", "workspace_memberships", "partner_profiles"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_show_ambiguous_partner_does_not_choose_workspace(tmp_path: Path, capsys) -> None:
    db_path = database(tmp_path)
    repository = PartnerRepository(db_path)
    first = run(repository.provision_partner(
        USER_ID, "First", "first",
        business_name="First", business_type="club_partner",
        short_description="First partner.", context={"specializations": ["travel"]},
    ))
    with sqlite3.connect(db_path) as db:
        cursor = db.execute(
            "INSERT INTO partner_workspaces(name, slug, status, created_at, updated_at) "
            "VALUES ('Second', 'second', 'active', 'now', 'now')"
        )
        db.execute(
            "INSERT INTO workspace_memberships(workspace_id, telegram_user_id, role, status, "
            "created_at, updated_at) VALUES (?, ?, 'member', 'active', 'now', 'now')",
            (cursor.lastrowid, USER_ID),
        )
    show = ["--db-path", str(db_path), "show-partner", "--telegram-user-id", str(USER_ID)]
    assert admin.main(show) == 2
    output = capsys.readouterr().out
    assert "state: AMBIGUOUS" in output
    assert f"workspace_id: {first.workspace.id}" in output


def test_cli_uses_journal_db_env_without_runtime_secrets(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    db_path = database(tmp_path)
    monkeypatch.setenv("JOURNAL_DB_PATH", str(db_path))
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    assert admin.main(["show-partner", "--telegram-user-id", str(USER_ID)]) == 2
    assert "membership не найдена" in capsys.readouterr().err
