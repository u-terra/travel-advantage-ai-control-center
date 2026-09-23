"""PartnerRepository.provision_self_service_workspace /
rebind_workspace_telegram_id / delete_freshly_provisioned_workspace - the
self-service signup path (see app.web_api's POST /api/auth/signup),
distinct from the CLI/invite-only provision_partner().
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.repositories.partner_repository import (
    PartnerMembershipNotFoundError,
    PartnerProvisioningConflictError,
    PartnerRepository,
    is_telegram_linked,
    slugify,
)


def run(coro):
    return asyncio.run(coro)


def _repo(tmp_path: Path) -> PartnerRepository:
    repo = PartnerRepository(tmp_path / "journal.sqlite3")
    run(repo.init())
    return repo


# ── slugify ──────────────────────────────────────────────────────────────

def test_slugify_transliterates_cyrillic_and_normalizes():
    assert slugify("Морские Приключения") == "morskie-priklyucheniya"


def test_slugify_never_returns_empty_string():
    assert slugify("!!!") != ""
    assert slugify("") != ""


# ── is_telegram_linked ───────────────────────────────────────────────────

def test_is_telegram_linked_true_for_positive_false_for_placeholder():
    assert is_telegram_linked(586249067) is True
    assert is_telegram_linked(-5) is False
    assert is_telegram_linked(0) is False


# ── provision_self_service_workspace: atomicity / shape ─────────────────

def test_provision_self_service_workspace_creates_full_tenant(tmp_path: Path):
    repo = _repo(tmp_path)

    result = run(repo.provision_self_service_workspace("Моё агентство"))

    assert result.workspace.name == "Моё агентство"
    assert result.workspace.slug == "moe-agentstvo"
    assert result.membership.role == "owner"
    assert result.membership.status == "active"
    # Placeholder identity: negative, unique per workspace, never a real
    # Telegram id.
    assert result.membership.telegram_user_id == -result.workspace.id
    assert is_telegram_linked(result.membership.telegram_user_id) is False
    assert result.profile.profile_status == "incomplete"

    # Persisted, not just returned in-memory.
    fetched = run(repo.get_membership(result.workspace.id, -result.workspace.id))
    assert fetched is not None
    assert fetched.status == "active"


def test_provision_self_service_workspace_resolves_slug_collisions(tmp_path: Path):
    repo = _repo(tmp_path)

    first = run(repo.provision_self_service_workspace("Vassian Travel"))
    second = run(repo.provision_self_service_workspace("Vassian Travel"))
    third = run(repo.provision_self_service_workspace("Vassian Travel"))

    slugs = {first.workspace.slug, second.workspace.slug, third.workspace.slug}
    assert len(slugs) == 3
    assert first.workspace.slug == "vassian-travel"
    assert second.workspace.slug == "vassian-travel-2"
    assert third.workspace.slug == "vassian-travel-3"


def test_provision_self_service_workspace_two_tenants_are_isolated(tmp_path: Path):
    repo = _repo(tmp_path)

    a = run(repo.provision_self_service_workspace("Agency A"))
    b = run(repo.provision_self_service_workspace("Agency B"))

    assert a.workspace.id != b.workspace.id
    assert a.membership.telegram_user_id != b.membership.telegram_user_id
    memberships_a = run(repo.list_memberships_by_telegram_id(a.membership.telegram_user_id))
    assert [m.workspace_id for m in memberships_a] == [a.workspace.id]


def test_provision_self_service_workspace_rejects_empty_business_name(tmp_path: Path):
    repo = _repo(tmp_path)
    with pytest.raises(ValueError):
        run(repo.provision_self_service_workspace("   "))


# ── delete_freshly_provisioned_workspace: compensating rollback ─────────

def test_delete_freshly_provisioned_workspace_removes_everything(tmp_path: Path):
    repo = _repo(tmp_path)
    provisioned = run(repo.provision_self_service_workspace("Rollback Test"))
    workspace_id = provisioned.workspace.id

    run(repo.delete_freshly_provisioned_workspace(workspace_id))

    assert run(repo.get_workspace(workspace_id)) is None
    assert run(repo.get_membership(workspace_id, -workspace_id)) is None


def test_delete_freshly_provisioned_workspace_leaves_other_workspaces_alone(tmp_path: Path):
    repo = _repo(tmp_path)
    keep = run(repo.provision_self_service_workspace("Keep Me"))
    doomed = run(repo.provision_self_service_workspace("Delete Me"))

    run(repo.delete_freshly_provisioned_workspace(doomed.workspace.id))

    assert run(repo.get_workspace(keep.workspace.id)) is not None


# ── rebind_workspace_telegram_id: Telegram-connect flow ─────────────────

def test_rebind_workspace_telegram_id_renames_the_membership(tmp_path: Path):
    repo = _repo(tmp_path)
    provisioned = run(repo.provision_self_service_workspace("Bind Test"))
    workspace_id = provisioned.workspace.id
    placeholder = -workspace_id
    real_telegram_id = 555000111

    updated = run(repo.rebind_workspace_telegram_id(workspace_id, placeholder, real_telegram_id))

    assert updated.telegram_user_id == real_telegram_id
    assert run(repo.get_membership(workspace_id, placeholder)) is None
    context = run(repo.resolve_workspace_context(real_telegram_id))
    assert context is not None
    assert context.workspace_id == workspace_id


def test_rebind_workspace_telegram_id_rejects_positive_new_id_already_bound(tmp_path: Path):
    """The one-Telegram-identity-per-workspace invariant provision_partner()
    already enforces must hold here too - a real Telegram account can't
    end up owning two workspaces via the bind-token path."""
    repo = _repo(tmp_path)
    already_owns = run(repo.provision_partner(
        700000001, "Existing Owner", "existing-owner",
        business_name="Existing Owner", business_type="other",
        short_description="", context={},
    ))
    new_signup = run(repo.provision_self_service_workspace("New Tenant"))
    placeholder = -new_signup.workspace.id

    with pytest.raises(PartnerProvisioningConflictError):
        run(repo.rebind_workspace_telegram_id(
            new_signup.workspace.id, placeholder, 700000001,
        ))

    # Nothing changed - the placeholder membership is exactly as it was.
    still_placeholder = run(repo.get_membership(new_signup.workspace.id, placeholder))
    assert still_placeholder is not None


def test_rebind_workspace_telegram_id_rejects_non_positive_new_id(tmp_path: Path):
    repo = _repo(tmp_path)
    provisioned = run(repo.provision_self_service_workspace("Bad Id Test"))
    workspace_id = provisioned.workspace.id

    with pytest.raises(ValueError):
        run(repo.rebind_workspace_telegram_id(workspace_id, -workspace_id, -1))


def test_rebind_workspace_telegram_id_missing_membership_raises(tmp_path: Path):
    repo = _repo(tmp_path)
    provisioned = run(repo.provision_self_service_workspace("Missing Membership"))
    workspace_id = provisioned.workspace.id

    with pytest.raises(PartnerMembershipNotFoundError):
        run(repo.rebind_workspace_telegram_id(workspace_id, -999999, 123456))


def test_rebind_cascades_workspace_user_preferences(tmp_path: Path):
    """A "Мой стиль" edit made via the web BEFORE Telegram is connected
    (written under the placeholder id) must not become orphaned once the
    real Telegram id takes over."""
    repo = _repo(tmp_path)
    provisioned = run(repo.provision_self_service_workspace("Preferences Test"))
    workspace_id = provisioned.workspace.id
    placeholder = -workspace_id
    real_telegram_id = 999111222

    run(repo.set_user_style_description(workspace_id, placeholder, "Дружелюбный тон"))

    run(repo.rebind_workspace_telegram_id(workspace_id, placeholder, real_telegram_id))

    preferences = run(repo.get_user_preferences(workspace_id, real_telegram_id))
    assert preferences is not None
    assert preferences.style_description == "Дружелюбный тон"
