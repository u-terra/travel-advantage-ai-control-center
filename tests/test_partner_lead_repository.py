"""Partner Discovery 2.0 lead pipeline: app.repositories.partner_lead_repository
+ app.domain.partner_leads status transitions. Pure repository-level tests,
no Telegram/Web wiring - see tests/test_partner_discovery_handlers.py for
the button-level wiring tests.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.domain.partner_leads import PartnerLeadStatus, compute_lead_identity_key
from app.repositories.partner_lead_repository import PartnerLeadRepository


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def repo(tmp_path) -> PartnerLeadRepository:
    repository = PartnerLeadRepository(tmp_path / "journal.sqlite3")
    _run(repository.init())
    return repository


def _candidate(**overrides) -> dict:
    base = {
        "display_name": "Onvisor", "name": "Onvisor", "url": "https://onvisor.ru/",
        "total_score": 74, "confidence": 0.9, "why_fit": ["a", "b"], "hook": "h",
        "contact_type": "telegram", "contact_value": "@onvisor_travel",
    }
    base.update(overrides)
    return base


# A new candidate is saved exactly once.
def test_new_candidate_is_saved_once(repo) -> None:
    candidate = _candidate()
    key = compute_lead_identity_key(candidate["url"])
    lead = _run(repo.upsert_from_candidate(1, key, candidate))

    assert lead.status == PartnerLeadStatus.NEW
    leads = _run(repo.list_for_workspace(1))
    assert len(leads) == 1


# Rediscovering the same candidate never creates a duplicate row.
def test_repeated_discovery_does_not_duplicate() -> None:
    import tempfile
    repository = PartnerLeadRepository(Path(tempfile.mkdtemp()) / "j.sqlite3")
    _run(repository.init())
    candidate = _candidate()
    key = compute_lead_identity_key(candidate["url"])

    first = _run(repository.upsert_from_candidate(1, key, candidate))
    second = _run(repository.upsert_from_candidate(1, key, candidate))

    assert first.lead_id == second.lead_id
    leads = _run(repository.list_for_workspace(1))
    assert len(leads) == 1


# Updating score/confidence on rediscovery never resets an advanced status.
def test_score_update_does_not_reset_status(repo) -> None:
    candidate = _candidate()
    key = compute_lead_identity_key(candidate["url"])
    lead = _run(repo.upsert_from_candidate(1, key, candidate))
    _run(repo.set_status(lead.lead_id, 1, PartnerLeadStatus.REVIEWED))
    _run(repo.set_status(lead.lead_id, 1, PartnerLeadStatus.APPROVED))

    updated_candidate = _candidate(total_score=90, confidence=0.5)
    refreshed = _run(repo.upsert_from_candidate(1, key, updated_candidate))

    assert refreshed.status == PartnerLeadStatus.APPROVED
    assert refreshed.score == 90


# approve -> status approved.
def test_approve_sets_status_approved(repo) -> None:
    candidate = _candidate()
    key = compute_lead_identity_key(candidate["url"])
    lead = _run(repo.upsert_from_candidate(1, key, candidate))

    result = _run(repo.set_status(lead.lead_id, 1, PartnerLeadStatus.APPROVED))

    assert result.ok
    assert result.lead.status == PartnerLeadStatus.APPROVED


# skip -> declined, history (row) kept.
def test_skip_sets_declined_and_keeps_row(repo) -> None:
    candidate = _candidate()
    key = compute_lead_identity_key(candidate["url"])
    lead = _run(repo.upsert_from_candidate(1, key, candidate))

    result = _run(repo.set_status(lead.lead_id, 1, PartnerLeadStatus.DECLINED))

    assert result.ok
    assert result.lead.status == PartnerLeadStatus.DECLINED
    assert _run(repo.get(lead.lead_id, 1)) is not None


# approved lead can transition to contacted.
def test_approved_lead_can_become_contacted(repo) -> None:
    candidate = _candidate()
    key = compute_lead_identity_key(candidate["url"])
    lead = _run(repo.upsert_from_candidate(1, key, candidate))
    _run(repo.set_status(lead.lead_id, 1, PartnerLeadStatus.APPROVED))

    result = _run(repo.set_status(lead.lead_id, 1, PartnerLeadStatus.CONTACTED))

    assert result.ok
    assert result.lead.status == PartnerLeadStatus.CONTACTED


# interested never rolls back to new.
def test_interested_cannot_be_demoted_to_new(repo) -> None:
    candidate = _candidate()
    key = compute_lead_identity_key(candidate["url"])
    lead = _run(repo.upsert_from_candidate(1, key, candidate))
    for status in (
        PartnerLeadStatus.APPROVED, PartnerLeadStatus.CONTACTED,
        PartnerLeadStatus.REPLIED, PartnerLeadStatus.INTERESTED,
    ):
        result = _run(repo.set_status(lead.lead_id, 1, status))
        assert result.ok

    blocked = _run(repo.set_status(lead.lead_id, 1, PartnerLeadStatus.NEW))

    assert not blocked.ok
    refreshed = _run(repo.get(lead.lead_id, 1))
    assert refreshed.status == PartnerLeadStatus.INTERESTED


# A send/action without approval is blocked (no "contacted" from "new").
def test_action_without_approval_is_blocked(repo) -> None:
    candidate = _candidate()
    key = compute_lead_identity_key(candidate["url"])
    lead = _run(repo.upsert_from_candidate(1, key, candidate))

    result = _run(repo.set_status(lead.lead_id, 1, PartnerLeadStatus.CONTACTED))

    assert not result.ok
    assert result.reason == "transition_not_allowed:new->contacted"


# Review queue filters new/approved/follow_up (not declined/interested).
def test_review_queue_filters_by_status(repo) -> None:
    new_lead = _run(repo.upsert_from_candidate(
        1, compute_lead_identity_key("https://a.ru/"), _candidate(url="https://a.ru/"),
    ))
    declined_lead = _run(repo.upsert_from_candidate(
        1, compute_lead_identity_key("https://b.ru/"), _candidate(url="https://b.ru/"),
    ))
    _run(repo.set_status(declined_lead.lead_id, 1, PartnerLeadStatus.DECLINED))
    approved_lead = _run(repo.upsert_from_candidate(
        1, compute_lead_identity_key("https://c.ru/"), _candidate(url="https://c.ru/"),
    ))
    _run(repo.set_status(approved_lead.lead_id, 1, PartnerLeadStatus.APPROVED))

    queue = _run(repo.list_for_workspace(
        1, statuses=[PartnerLeadStatus.NEW, PartnerLeadStatus.APPROVED, PartnerLeadStatus.FOLLOW_UP],
    ))

    queue_ids = {lead.lead_id for lead in queue}
    assert new_lead.lead_id in queue_ids
    assert approved_lead.lead_id in queue_ids
    assert declined_lead.lead_id not in queue_ids


# Tenant/workspace isolation: a lead saved for workspace 1 is invisible to
# workspace 2, even with the same identity_key.
def test_workspace_isolation(repo) -> None:
    candidate = _candidate()
    key = compute_lead_identity_key(candidate["url"])
    lead = _run(repo.upsert_from_candidate(1, key, candidate))

    assert _run(repo.get(lead.lead_id, 2)) is None
    assert _run(repo.list_for_workspace(2)) == []

    # Same identity, different workspace - independent row, not shared state.
    other_lead = _run(repo.upsert_from_candidate(2, key, candidate))
    assert other_lead.lead_id != lead.lead_id
