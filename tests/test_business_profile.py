from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from app.domain.business_profiles import (
    BusinessProfileValidationError,
    StaleBusinessProfileError,
)
from app.domain.partners import WorkspaceContext
from app.repositories import partner_repository as partner_module
from app.repositories.partner_repository import (
    BusinessProfileSchemaError,
    PartnerRepository,
    empty_business_context,
)
from app.services.business_profile_context import (
    BusinessProfileAccessError,
    BusinessProfileService,
    build_assistant_context,
    build_competitor_context,
    build_content_context,
)


def run(coro):
    return asyncio.run(coro)


def workspace(db_path: Path, slug: str) -> int:
    with sqlite3.connect(db_path) as db:
        cursor = db.execute(
            "INSERT INTO partner_workspaces(name, slug, status, created_at, updated_at) "
            "VALUES (?, ?, 'active', 'x', 'x')", (slug, slug),
        )
        return int(cursor.lastrowid)


def profile_values(**overrides):
    context = empty_business_context()
    context["specializations"] = ["package_tours"]
    context["destinations"] = ["Россия"]
    context["audiences"] = ["семьи"]
    context["markets"] = ["Самара"]
    context["positioning"] = {
        "statement": "Путешествия без лишней суеты",
        "value_proposition": "Подбор под запрос",
        "differentiators": ["Личная консультация"],
    }
    context["communication"]["preferred_terms"] = ["путешествие"]
    context["goals"] = ["leads"]
    context["content_preferences"]["formats"] = ["telegram"]
    context["public_contacts"]["website"] = "https://example.com"
    context["claims"] = [
        {"text": "Работаем 15 лет"},
        {
            "text": "Официальный партнёр X", "verification_status": "verified",
            "evidence_reference": "https://example.com/evidence", "updated_at": "2026-01-01",
            "verified_at": "2026-01-01",
        },
    ]
    values = dict(
        business_name="Путешествия с Анной", business_type="independent_agent",
        short_description="Подбираем путешествия для семей.", context=context,
    )
    values.update(overrides)
    return values


def test_legacy_migration_preserves_exact_row_and_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.executescript("""
        CREATE TABLE partner_workspaces (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            slug TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE partner_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL UNIQUE,
            telegram_user_id INTEGER NOT NULL UNIQUE, partner_name TEXT NOT NULL,
            project_name TEXT NOT NULL, business_description TEXT NOT NULL,
            communication_style TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY(workspace_id) REFERENCES partner_workspaces(id)
        );
        INSERT INTO partner_workspaces VALUES (7, 'Other', 'other', 'active', 'w1', 'w2');
        INSERT INTO partner_workspaces VALUES (42, 'Exact', 'exact', 'active', 'w1', 'w2');
        INSERT INTO partner_profiles VALUES (
            18, 7, 100, 'Owner', 'Travel Advantage AI Ecosystem',
            'Внутреннее партнёрское рабочее пространство.',
            'Спокойный и профессиональный', 'created-bootstrap', 'updated-bootstrap'
        );
        INSERT INTO partner_profiles VALUES (
            19, 42, 586249067, 'Анна', 'Generic Agency', 'Описание',
            'Дружелюбный', 'created', 'updated'
        );
        """)
    repo = PartnerRepository(db_path)
    run(repo.init())
    run(repo.init())
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM partner_profiles ORDER BY id").fetchall()
        columns = {item[1]: item for item in db.execute("PRAGMA table_info(partner_profiles)")}
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    bootstrap, row = rows
    assert bootstrap["id"] == 18 and bootstrap["workspace_id"] == 7
    assert bootstrap["business_type"] == "club_partner"
    assert row["id"] == 19 and row["workspace_id"] == 42
    assert row["telegram_user_id"] == 586249067
    assert row["partner_name"] == "Анна"
    assert row["project_name"] == row["business_name"] == "Generic Agency"
    assert row["business_description"] == row["short_description"] == "Описание"
    assert row["communication_style"] == "Дружелюбный"
    assert row["created_at"] == "created" and row["updated_at"] == "updated"
    assert columns["telegram_user_id"][3] == 0
    assert row["business_type"] == "other"
    assert json.loads(row["context_json"])["communication"]["tone"] == "Дружелюбный"


def test_valid_stage5_schema_init_is_noop_and_preserves_data(tmp_path: Path) -> None:
    repo = PartnerRepository(tmp_path / "db.sqlite3")
    run(repo.init())
    ws = workspace(repo.db_path, "one")
    original = run(repo.create_business_profile(ws, **profile_values()))
    run(repo.init())
    assert run(repo.get_business_profile(ws)) == original


@pytest.mark.parametrize("defect", ["missing_workspace_unique", "missing_revision_check"])
def test_partial_canonical_schema_fails_closed(tmp_path: Path, defect: str) -> None:
    db_path = tmp_path / "db.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute("""CREATE TABLE partner_workspaces (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            slug TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        ddl = partner_module._PROFILE_SCHEMA.format(table_name="partner_profiles")
        if defect == "missing_workspace_unique":
            ddl = ddl.replace("workspace_id INTEGER NOT NULL UNIQUE", "workspace_id INTEGER NOT NULL")
        else:
            ddl = ddl.replace(" CHECK (revision >= 1)", "")
        db.execute(ddl)
    with pytest.raises(BusinessProfileSchemaError):
        run(PartnerRepository(db_path).init())


def test_legacy_migration_error_rolls_back_original_profile_table(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.executescript("""
        CREATE TABLE partner_workspaces (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            slug TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE partner_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL UNIQUE,
            telegram_user_id INTEGER NOT NULL UNIQUE, partner_name TEXT NOT NULL,
            project_name TEXT NOT NULL, business_description TEXT NOT NULL,
            communication_style TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY(workspace_id) REFERENCES partner_workspaces(id)
        );
        INSERT INTO partner_profiles VALUES (
            5, 999, 100, 'Owner', 'Legacy', 'Description', 'Tone', 'created', 'updated'
        );
        """)
    with pytest.raises(sqlite3.IntegrityError):
        run(PartnerRepository(db_path).init())
    with sqlite3.connect(db_path) as db:
        columns = [row[1] for row in db.execute("PRAGMA table_info(partner_profiles)")]
        row = db.execute("SELECT id, workspace_id, project_name FROM partner_profiles").fetchone()
        temp_exists = db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='partner_profiles_new'"
        ).fetchone()[0]
    assert "business_name" not in columns
    assert row == (5, 999, "Legacy")
    assert temp_exists == 0


def test_minimal_incomplete_optional_and_multi_workspace_profiles(tmp_path: Path) -> None:
    repo = PartnerRepository(tmp_path / "db.sqlite3")
    run(repo.init())
    first = workspace(repo.db_path, "first")
    second = workspace(repo.db_path, "second")
    incomplete = run(repo.create_business_profile(
        first, business_name="A", business_type="agency", short_description="", context={}
    ))
    usable = run(repo.create_business_profile(second, **profile_values()))
    assert incomplete.profile_status == "incomplete"
    assert usable.profile_status == "usable" and usable.revision == 1
    assert usable.context.public_contacts["phone"] == ""
    with sqlite3.connect(repo.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM partner_profiles WHERE telegram_user_id IS NULL"
        ).fetchone()[0] == 2


def test_tenant_reads_updates_and_revision_fail_closed(tmp_path: Path) -> None:
    repo = PartnerRepository(tmp_path / "db.sqlite3")
    run(repo.init())
    first = workspace(repo.db_path, "first")
    second = workspace(repo.db_path, "second")
    a = run(repo.create_business_profile(first, **profile_values(business_name="A")))
    b = run(repo.create_business_profile(second, **profile_values(business_name="B")))
    updated = run(repo.update_business_profile(
        first, a.revision, **profile_values(business_name="A2")
    ))
    assert updated.revision == 2
    assert run(repo.get_business_profile(second)) == b
    with pytest.raises(StaleBusinessProfileError):
        run(repo.update_business_profile(
            first, a.revision, **profile_values(business_name="stale")
        ))
    assert run(repo.get_business_profile(first)).business_name == "A2"
    with pytest.raises(StaleBusinessProfileError):
        run(repo.update_business_profile(
            second + 999, b.revision, **profile_values(business_name="foreign")
        ))
    assert run(repo.get_business_profile(second)).business_name == "B"


@pytest.mark.parametrize("business_type", ["mwr_only", "", "unknown"])
def test_invalid_business_type_and_schema_are_rejected(tmp_path: Path, business_type: str) -> None:
    repo = PartnerRepository(tmp_path / "db.sqlite3")
    run(repo.init())
    ws = workspace(repo.db_path, "one")
    with pytest.raises(BusinessProfileValidationError):
        run(repo.create_business_profile(ws, **profile_values(business_type=business_type)))
    with pytest.raises(BusinessProfileValidationError):
        run(repo.create_business_profile(ws, **profile_values(schema_version=2)))


@pytest.mark.parametrize(
    "bad_context",
    [
        {"specializations": "tours"},
        {"competitors": []},
        {"crm_token": "secret"},
        {"source_subscriptions": []},
        {"journal": []},
        {"communication": {"api_key": "secret"}},
        {"claims": [{"text": "X", "verification_status": "proved"}]},
    ],
)
def test_malformed_or_forbidden_context_is_rejected(
    tmp_path: Path, bad_context: dict
) -> None:
    repo = PartnerRepository(tmp_path / "db.sqlite3")
    run(repo.init())
    ws = workspace(repo.db_path, "one")
    with pytest.raises(BusinessProfileValidationError):
        run(repo.create_business_profile(ws, **profile_values(context=bad_context)))


def test_malformed_stored_json_is_rejected(tmp_path: Path) -> None:
    repo = PartnerRepository(tmp_path / "db.sqlite3")
    run(repo.init())
    ws = workspace(repo.db_path, "one")
    run(repo.create_business_profile(ws, **profile_values()))
    with sqlite3.connect(repo.db_path) as db:
        db.execute("UPDATE partner_profiles SET context_json='{' WHERE workspace_id=?", (ws,))
    with pytest.raises(BusinessProfileValidationError):
        run(repo.get_business_profile(ws))


def context(workspace_id: int, role: str, status: str = "active") -> WorkspaceContext:
    return WorkspaceContext(100, workspace_id, role, status)


def test_permission_boundary_owner_admin_member_and_inactive(tmp_path: Path) -> None:
    repo = PartnerRepository(tmp_path / "db.sqlite3")
    run(repo.init())
    ws = workspace(repo.db_path, "one")
    profile = run(repo.create_business_profile(ws, **profile_values()))
    service = BusinessProfileService(repo)
    assert run(service.get(context(ws, "owner"))) == profile
    assert run(service.get(context(ws, "admin"))) == profile
    assert run(service.get(context(ws, "member"))) == profile
    run(service.update(context(ws, "admin"), profile.revision, **profile_values()))
    with pytest.raises(BusinessProfileAccessError):
        run(service.update(context(ws, "member"), 2, **profile_values()))
    with pytest.raises(BusinessProfileAccessError):
        run(service.get(context(ws, "owner", "inactive")))
    inactive_id = 999
    run(repo.create_membership(ws, inactive_id, role="member", status="inactive"))
    assert run(repo.resolve_workspace_context(inactive_id)) is None
    with pytest.raises(BusinessProfileAccessError):
        run(service.get(None))


def test_claims_and_purpose_specific_safe_projections(tmp_path: Path) -> None:
    repo = PartnerRepository(tmp_path / "db.sqlite3")
    run(repo.init())
    ws = workspace(repo.db_path, "one")
    profile = run(repo.create_business_profile(ws, **profile_values()))
    assert profile.context.claims[0].verification_status == "unverified"
    assert profile.context.claims[0].verified_at is None
    assert profile.context.claims[1].verification_status == "verified"
    assert profile.context.claims[1].evidence_reference == "https://example.com/evidence"
    with pytest.raises(TypeError):
        profile.context.communication["tone"] = "mutated"

    content = build_content_context(profile)
    competitor = build_competitor_context(profile)
    assistant = build_assistant_context(profile)
    assert content["goals"] == ["leads"]
    assert {claim["verification_status"] for claim in content["claims"]} == {
        "unverified", "verified"
    }
    assert "positioning" in competitor and "competitors" not in competitor
    assert [claim["verification_status"] for claim in assistant["verified_claims"]] == [
        "verified"
    ]
    forbidden = {
        "goals", "content_preferences", "positioning", "telegram_user_id", "revision",
        "workspace_id", "competitors", "credentials", "crm_token", "source_subscriptions",
    }
    assert forbidden.isdisjoint(assistant)
