from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.partner_repository import (
    AmbiguousWorkspaceError,
    OwnerMembershipConflictError,
    PartnerMembershipNotFoundError,
    PartnerProvisioningConflictError,
    PartnerRepository,
    empty_business_context,
)
from app.storage import Journal


OWNER_ID = 586249067
OTHER_ID = 111222333
THIRD_ID = 444555666


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _repository(tmp_path: Path) -> PartnerRepository:
    return PartnerRepository(tmp_path / "workspace.sqlite3")


def test_init_creates_tables_in_empty_database(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())

    with sqlite3.connect(repository.db_path) as db:
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {"partner_workspaces", "partner_profiles", "workspace_memberships"} <= tables


def test_existing_journal_data_is_preserved(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    workspace, _ = _run(repository.ensure_owner_workspace(OWNER_ID))
    journal = Journal(repository.db_path)
    _run(journal.init(workspace.id))
    entry_id = _run(journal.add(workspace.id, "Задача", "content", (), "low"))

    _run(repository.init())

    entry = _run(journal.last(workspace.id))
    assert entry is not None
    assert entry.id == entry_id
    assert entry.task_text == "Задача"


def test_owner_bootstrap_is_idempotent_and_linked(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())

    workspace, profile = _run(repository.ensure_owner_workspace(OWNER_ID))
    repeated_workspace, repeated_profile = _run(
        repository.ensure_owner_workspace(OWNER_ID)
    )

    assert repeated_workspace == workspace
    assert repeated_profile == profile
    assert profile.workspace_id == workspace.id
    assert profile.telegram_user_id == OWNER_ID
    assert _run(repository.get_workspace(workspace.id)) == workspace
    assert _run(repository.get_profile(workspace.id)) == profile
    assert _run(repository.find_workspace_by_telegram_id(OWNER_ID)) == workspace

    with sqlite3.connect(repository.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM partner_workspaces").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM partner_profiles").fetchone()[0] == 1


def _insert_workspace(db_path: Path, slug: str, status: str = "active") -> int:
    with sqlite3.connect(db_path) as db:
        cursor = db.execute(
            "INSERT INTO partner_workspaces "
            "(name, slug, status, created_at, updated_at) VALUES (?, ?, ?, 'now', 'now')",
            (slug, slug, status),
        )
        return cursor.lastrowid


def test_membership_constraints_and_one_user_in_two_workspaces(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    first = _insert_workspace(repository.db_path, "first")
    second = _insert_workspace(repository.db_path, "second")

    one = _run(repository.create_membership(first, OWNER_ID, role="owner"))
    two = _run(repository.create_membership(second, OWNER_ID, role="member"))
    assert [one.workspace_id, two.workspace_id] == [first, second]

    with pytest.raises(sqlite3.IntegrityError):
        _run(repository.create_membership(first, OWNER_ID, role="admin"))
    with pytest.raises(sqlite3.IntegrityError):
        _run(repository.create_membership(999, OTHER_ID, role="member"))
    with pytest.raises(ValueError):
        _run(repository.create_membership(first, OTHER_ID, role="superuser"))
    with pytest.raises(ValueError):
        _run(repository.create_membership(first, OTHER_ID, role="member", status="pending"))

    with sqlite3.connect(repository.db_path) as db:
        for column, value in (("role", "superuser"), ("status", "pending")):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(
                    f"INSERT INTO workspace_memberships "
                    f"(workspace_id, telegram_user_id, role, status, created_at, updated_at) "
                    f"VALUES (?, ?, ?, ?, 'now', 'now')",
                    (first, OTHER_ID, value if column == "role" else "member",
                     value if column == "status" else "active"),
                )


def test_owner_backfill_is_idempotent_and_uses_profile_workspace(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    workspace, _ = _run(repository.ensure_owner_workspace(OWNER_ID))
    first = _run(repository.bootstrap_owner_membership(OWNER_ID))
    repeated = _run(repository.bootstrap_owner_membership(OWNER_ID))
    assert first == repeated
    assert first is not None
    assert first.workspace_id == workspace.id
    assert first.role == "owner"
    assert first.status == "active"


@pytest.mark.parametrize(
    ("role", "status"),
    (("member", "active"), ("admin", "active"), ("owner", "inactive")),
)
def test_owner_backfill_rejects_conflicting_existing_membership(
    tmp_path: Path, role: str, status: str
) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    workspace, _ = _run(repository.ensure_owner_workspace(OWNER_ID))
    existing = _run(
        repository.create_membership(
            workspace.id, OWNER_ID, role=role, status=status
        )
    )

    with pytest.raises(OwnerMembershipConflictError):
        _run(repository.bootstrap_owner_membership(OWNER_ID))

    unchanged = _run(repository.get_membership(workspace.id, OWNER_ID))
    assert unchanged == existing


def test_owner_backfill_never_uses_first_workspace_without_profile(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    _insert_workspace(repository.db_path, "unrelated")
    assert _run(repository.bootstrap_owner_membership(OWNER_ID)) is None
    assert _run(repository.list_memberships_by_telegram_id(OWNER_ID)) == []


def test_empty_database_bootstrap_creates_initial_owner_boundary(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    membership = _run(repository.bootstrap_owner_membership(OWNER_ID))
    assert membership is not None
    assert membership.role == "owner"
    assert _run(repository.find_workspace_by_telegram_id(OWNER_ID)) is not None


def test_workspace_context_resolution_states(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    active = _insert_workspace(repository.db_path, "active")
    inactive_workspace = _insert_workspace(repository.db_path, "inactive", "inactive")

    assert _run(repository.resolve_workspace_context(OWNER_ID)) is None
    _run(repository.create_membership(active, OWNER_ID, role="member", status="inactive"))
    assert _run(repository.resolve_workspace_context(OWNER_ID)) is None
    _run(repository.create_membership(inactive_workspace, THIRD_ID, role="admin"))
    assert _run(repository.resolve_workspace_context(THIRD_ID)) is None

    second_active = _insert_workspace(repository.db_path, "second-active")
    third_active = _insert_workspace(repository.db_path, "third-active")
    _run(repository.create_membership(second_active, OTHER_ID, role="admin"))
    context = _run(repository.resolve_workspace_context(OTHER_ID))
    assert context is not None
    assert context.workspace_id == second_active
    assert context.workspace_status == "active"
    _run(repository.create_membership(third_active, OTHER_ID, role="member"))
    with pytest.raises(AmbiguousWorkspaceError):
        _run(repository.resolve_workspace_context(OTHER_ID))


def test_different_telegram_id_cannot_read_owner_workspace(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    workspace, _ = _run(repository.ensure_owner_workspace(OWNER_ID))

    assert _run(repository.find_workspace_by_telegram_id(OTHER_ID)) is None
    assert _run(repository.find_workspace_by_telegram_id(OWNER_ID)) == workspace


def test_second_telegram_id_conflicts_without_changing_owner_data(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    workspace, profile = _run(repository.ensure_owner_workspace(OWNER_ID))

    with pytest.raises(sqlite3.IntegrityError):
        _run(repository.ensure_owner_workspace(OTHER_ID))

    assert _run(repository.get_profile(workspace.id)) == profile
    assert _run(repository.find_workspace_by_telegram_id(OTHER_ID)) is None
    with sqlite3.connect(repository.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM partner_workspaces").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM partner_profiles").fetchone()[0] == 1


def test_foreign_key_rejects_profile_without_workspace(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())

    with sqlite3.connect(repository.db_path) as db:
        db.execute("PRAGMA foreign_keys = ON")
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO partner_profiles "
                "(workspace_id, telegram_user_id, partner_name, project_name, "
                "business_description, communication_style, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (999, OWNER_ID, "Владелец", "Проект", "Описание", "Стиль", "now", "now"),
            )


def test_profile_can_be_updated_for_future_stages(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    workspace, _ = _run(repository.ensure_owner_workspace(OWNER_ID))

    profile = _run(
        repository.update_profile(
            workspace.id,
            partner_name="Анна",
            project_name="Путешествия с Анной",
            business_description="Помощь путешественникам.",
            communication_style="Дружелюбный",
        )
    )

    assert profile is not None
    assert profile.workspace_id == workspace.id
    assert profile.partner_name == "Анна"
    assert profile.communication_style == "Дружелюбный"

    repeated_workspace, repeated_profile = _run(
        repository.ensure_owner_workspace(OWNER_ID)
    )
    assert repeated_workspace == workspace
    assert repeated_profile == profile


def _pilot_profile(name: str = "TA Partner") -> dict[str, Any]:
    context = empty_business_context()
    context["specializations"] = ["travel_club"]
    context["audiences"] = ["travelers"]
    context["communication"]["tone"] = "calm"
    return {
        "business_name": name,
        "business_type": "club_partner",
        "short_description": "Travel Advantage partner.",
        "context": context,
    }


def _provision(
    repository: PartnerRepository, telegram_user_id: int = OTHER_ID,
    slug: str = "ta-partner", name: str = "TA Partner",
):
    return _run(repository.provision_partner(
        telegram_user_id, name, slug, **_pilot_profile(name),
    ))


def test_atomic_partner_provision_and_second_tenant(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    first = _provision(repository)
    second = _provision(repository, THIRD_ID, "ta-partner-2", "TA Partner 2")
    assert first.created and second.created
    assert first.workspace.id != second.workspace.id
    assert first.membership.workspace_id == first.profile.workspace_id == first.workspace.id
    assert first.membership.status == "active" and first.membership.role == "owner"
    assert first.profile.profile_status == "usable"


def test_exact_duplicate_provision_is_idempotent(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    first = _provision(repository)
    repeated = _provision(repository)
    assert first.workspace == repeated.workspace
    assert first.membership == repeated.membership
    assert first.profile == repeated.profile
    assert repeated.created is False
    with sqlite3.connect(repository.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM partner_workspaces").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM workspace_memberships").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM partner_profiles").fetchone()[0] == 1


def test_duplicate_with_claim_default_timestamp_is_idempotent(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    values = _pilot_profile()
    values["context"]["claims"] = [{"text": "TA affiliation"}]
    first = _run(repository.provision_partner(
        OTHER_ID, "TA Partner", "ta-partner", **values,
    ))
    repeated = _run(repository.provision_partner(
        OTHER_ID, "TA Partner", "ta-partner", **values,
    ))
    assert first.workspace == repeated.workspace
    assert repeated.created is False


@pytest.mark.parametrize("updated_at", [None, ""])
def test_duplicate_with_empty_claim_timestamp_is_idempotent(
    tmp_path: Path, updated_at,
) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    values = _pilot_profile()
    values["context"]["claims"] = [{
        "text": "TA affiliation", "updated_at": updated_at,
    }]
    first = _run(repository.provision_partner(
        OTHER_ID, "TA Partner", "ta-partner", **values,
    ))
    stored_timestamp = first.profile.context.claims[0].updated_at
    assert stored_timestamp
    repeated = _run(repository.provision_partner(
        OTHER_ID, "TA Partner", "ta-partner", **values,
    ))
    assert repeated.created is False
    assert repeated.profile.context.claims[0].updated_at == stored_timestamp
    with sqlite3.connect(repository.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM partner_workspaces").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM workspace_memberships").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM partner_profiles").fetchone()[0] == 1


def test_changed_meaningful_claim_timestamp_remains_conflict(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    values = _pilot_profile()
    values["context"]["claims"] = [{
        "text": "TA affiliation", "updated_at": "2026-01-01T00:00:00+00:00",
    }]
    _run(repository.provision_partner(
        OTHER_ID, "TA Partner", "ta-partner", **values,
    ))
    values["context"]["claims"][0]["updated_at"] = "2026-02-01T00:00:00+00:00"
    with pytest.raises(PartnerProvisioningConflictError):
        _run(repository.provision_partner(
            OTHER_ID, "TA Partner", "ta-partner", **values,
        ))


def test_provision_conflicts_fail_without_partial_tenant(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    _provision(repository)
    with pytest.raises(PartnerProvisioningConflictError):
        _provision(repository, THIRD_ID, "ta-partner", "Other")
    with pytest.raises(PartnerProvisioningConflictError):
        _provision(repository, OTHER_ID, "different", "Different")
    with sqlite3.connect(repository.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM partner_workspaces").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM workspace_memberships").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM partner_profiles").fetchone()[0] == 1


def test_invalid_profile_leaves_no_partial_tenant(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    invalid = _pilot_profile()
    invalid["business_type"] = "invalid"
    with pytest.raises(ValueError):
        _run(repository.provision_partner(
            OTHER_ID, "TA Partner", "ta-partner", **invalid,
        ))
    with sqlite3.connect(repository.db_path) as db:
        for table in ("partner_workspaces", "workspace_memberships", "partner_profiles"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_late_profile_insert_failure_rolls_back_workspace_and_membership(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    with sqlite3.connect(repository.db_path) as db:
        db.execute(
            "CREATE TRIGGER reject_pilot_profile BEFORE INSERT ON partner_profiles "
            "BEGIN SELECT RAISE(ABORT, 'rejected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        _provision(repository)
    with sqlite3.connect(repository.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM partner_workspaces").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM workspace_memberships").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM partner_profiles").fetchone()[0] == 0


def test_unexpected_existing_membership_status_fails_closed(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    _provision(repository)
    _run(repository.set_partner_membership_status(OTHER_ID, "inactive"))
    with pytest.raises(PartnerProvisioningConflictError):
        _provision(repository)


def test_unexpected_existing_membership_role_fails_closed(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    provisioned = _provision(repository)
    with sqlite3.connect(repository.db_path) as db:
        db.execute(
            "UPDATE workspace_memberships SET role='admin' WHERE id=?",
            (provisioned.membership.id,),
        )
    with pytest.raises(PartnerProvisioningConflictError):
        _provision(repository)


def test_deactivate_reactivate_are_idempotent_and_keep_profile(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    provisioned = _provision(repository)
    artifacts = ArtifactRepository(repository.db_path)
    _run(artifacts.init())
    source = _run(artifacts.create_source(
        provisioned.workspace.id,
        source_type="text",
        title="Pilot source",
        original_text="Source content",
    ))
    artifact, _ = _run(artifacts.create_artifact_with_initial_version(
        provisioned.workspace.id,
        artifact_type="post",
        title="Pilot artifact",
        content="Artifact content",
        source_id=source.id,
    ))
    journal = Journal(repository.db_path)
    _run(journal.init(None))
    journal_id = _run(journal.add(
        provisioned.workspace.id, "Pilot task", "content", (), "low",
    ))
    inactive = _run(repository.set_partner_membership_status(OTHER_ID, "inactive"))
    repeated = _run(repository.set_partner_membership_status(OTHER_ID, "inactive"))
    assert inactive.status == repeated.status == "inactive"
    assert _run(repository.resolve_workspace_context(OTHER_ID)) is None
    assert _run(repository.get_business_profile(provisioned.workspace.id)) == provisioned.profile
    assert _run(artifacts.get_source(provisioned.workspace.id, source.id)) == source
    assert _run(artifacts.get_artifact(provisioned.workspace.id, artifact.id)) == artifact
    assert _run(journal.last(provisioned.workspace.id)).id == journal_id
    active = _run(repository.set_partner_membership_status(OTHER_ID, "active"))
    assert active.status == "active"
    assert _run(repository.resolve_workspace_context(OTHER_ID)).workspace_id == provisioned.workspace.id
    assert _run(artifacts.get_source(provisioned.workspace.id, source.id)) == source
    assert _run(artifacts.get_artifact(provisioned.workspace.id, artifact.id)) == artifact
    assert _run(journal.last(provisioned.workspace.id)).id == journal_id


def test_status_change_never_selects_ambiguous_membership(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    _provision(repository)
    other_workspace = _insert_workspace(repository.db_path, "other")
    _run(repository.create_membership(other_workspace, OTHER_ID, role="member"))
    with pytest.raises(PartnerMembershipNotFoundError):
        _run(repository.set_partner_membership_status(OTHER_ID, "inactive"))
    with pytest.raises(AmbiguousWorkspaceError):
        _run(repository.resolve_workspace_context(OTHER_ID))


def test_reactivate_fails_with_another_active_membership(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _run(repository.init())
    _provision(repository)
    _run(repository.set_partner_membership_status(OTHER_ID, "inactive"))
    other_workspace = _insert_workspace(repository.db_path, "other-active")
    _run(repository.create_membership(other_workspace, OTHER_ID, role="member"))
    with pytest.raises(PartnerMembershipNotFoundError):
        _run(repository.set_partner_membership_status(OTHER_ID, "active"))
    assert _run(repository.get_membership(other_workspace, OTHER_ID)).status == "active"
