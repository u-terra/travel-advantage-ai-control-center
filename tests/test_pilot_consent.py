from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from app.domain.partners import WorkspaceContext
from app.handlers.consent import accept_pilot_consent, show_pilot_consent
from app.handlers.start import cmd_start
from app.keyboards import PILOT_CONSENT_ACCEPT_PREFIX
from app.repositories.partner_repository import PartnerRepository
from app.services.pilot_consent import (
    PILOT_CONSENT_VERSION,
    PilotConsentRequiredError,
    PilotConsentService,
)


def run(value):
    return asyncio.run(value)


def provision(repository: PartnerRepository, user_id: int, slug: str):
    return run(repository.provision_partner(
        user_id, slug, slug,
        business_name=slug, business_type="club_partner",
        short_description="Pilot workspace.",
        context={"specializations": ["travel"]},
    ))


def context(user_id: int, workspace_id: int) -> WorkspaceContext:
    return WorkspaceContext(user_id, workspace_id, "owner", "active")


class Message:
    def __init__(self) -> None:
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


class State:
    async def clear(self):
        pass


class Callback:
    def __init__(self, version=PILOT_CONSENT_VERSION) -> None:
        self.data = f"{PILOT_CONSENT_ACCEPT_PREFIX}{version}"
        self.message = Message()
        self.answers = []

    async def answer(self, text="", **kwargs):
        self.answers.append((text, kwargs))


def test_consent_is_tenant_scoped_idempotent_and_versioned(tmp_path: Path) -> None:
    db_path = tmp_path / "consent.sqlite3"
    repository = PartnerRepository(db_path)
    run(repository.init())
    first = provision(repository, 100, "first")
    second = provision(repository, 200, "second")

    accepted = run(repository.accept_user_consent(
        first.workspace.id, 100, PILOT_CONSENT_VERSION
    ))
    repeated = run(repository.accept_user_consent(
        first.workspace.id, 100, PILOT_CONSENT_VERSION
    ))
    assert repeated == accepted
    assert run(repository.has_user_consent(
        first.workspace.id, 100, PILOT_CONSENT_VERSION
    )) is True
    assert run(repository.has_user_consent(
        second.workspace.id, 200, PILOT_CONSENT_VERSION
    )) is False
    assert run(repository.has_user_consent(
        first.workspace.id, 100, "pilot-v2"
    )) is False
    newer = run(repository.accept_user_consent(
        first.workspace.id, 100, "pilot-v2"
    ))
    assert newer.id != accepted.id
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM user_consents").fetchone()[0] == 2
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_consent_cannot_be_written_for_foreign_membership(tmp_path: Path) -> None:
    repository = PartnerRepository(tmp_path / "consent.sqlite3")
    run(repository.init())
    first = provision(repository, 100, "first")
    provision(repository, 200, "second")
    with pytest.raises(PermissionError):
        run(repository.accept_user_consent(
            first.workspace.id, 200, PILOT_CONSENT_VERSION
        ))


def test_paid_access_check_is_fail_closed_and_then_allows(tmp_path: Path) -> None:
    repository = PartnerRepository(tmp_path / "consent.sqlite3")
    run(repository.init())
    partner = provision(repository, 100, "paid")
    workspace_context = context(100, partner.workspace.id)
    service = PilotConsentService(repository)
    with pytest.raises(PilotConsentRequiredError):
        run(service.require_for_paid_access(workspace_context))
    run(service.accept_current(workspace_context))
    assert run(service.require_for_paid_access(workspace_context)) is None


def test_telegram_consent_is_separate_action_and_stale_version_fails(
    tmp_path: Path,
) -> None:
    repository = PartnerRepository(tmp_path / "consent.sqlite3")
    run(repository.init())
    partner = provision(repository, 100, "telegram")
    workspace_context = context(100, partner.workspace.id)
    message = Message()
    run(show_pilot_consent(message))
    button = message.answers[0][1]["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Я согласен на обработку персональных данных"
    assert "оплат" not in button.text.lower() and "оферт" not in button.text.lower()

    stale = Callback("old-version")
    run(accept_pilot_consent(stale, workspace_context, repository))
    assert stale.answers[0][1]["show_alert"] is True
    assert run(repository.has_user_consent(
        partner.workspace.id, 100, PILOT_CONSENT_VERSION
    )) is False

    callback = Callback()
    run(accept_pilot_consent(callback, workspace_context, repository))
    assert "Согласие сохранено" in callback.answers[0][0]
    assert run(repository.has_user_consent(
        partner.workspace.id, 100, PILOT_CONSENT_VERSION
    )) is True


def test_legacy_start_flow_does_not_require_consent() -> None:
    message = Message()
    run(cmd_start(message, State(), v2_menu_enabled=True))
    text = message.answers[0][0]
    assert text
    assert "согласи" not in text.lower()
    assert "consent" not in text.lower()
