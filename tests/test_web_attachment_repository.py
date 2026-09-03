"""WebAttachmentRepository - workspace/user/conversation isolation, the
pending -> attached lifecycle, and orphan/conversation cleanup. Pure
repository-level coverage (no FastAPI); see tests/test_web_api_attachments.py
for the HTTP surface built on top of this.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.repositories.web_attachment_repository import WebAttachmentRepository
from app.repositories.web_conversation_repository import WebConversationRepository
from app.repositories.partner_repository import PartnerRepository

WORKSPACE_A_OWNER = 111
WORKSPACE_B_OWNER = 222


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def repos(tmp_path):
    db_path = tmp_path / "journal.sqlite3"
    partner_repository = PartnerRepository(db_path)
    conversation_repository = WebConversationRepository(db_path)
    attachment_repository = WebAttachmentRepository(db_path)

    _run(partner_repository.init())
    _run(conversation_repository.init())
    _run(attachment_repository.init())

    workspace_a, _ = _run(partner_repository.ensure_owner_workspace(WORKSPACE_A_OWNER))
    provisioned_b = _run(partner_repository.provision_partner(
        WORKSPACE_B_OWNER, "Workspace B", "workspace-b-attachments",
        business_name="Workspace B", business_type="independent_agent",
        short_description="Второе рабочее пространство для теста изоляции.",
        context={},
    ))

    return (
        partner_repository, conversation_repository, attachment_repository,
        workspace_a.id, provisioned_b.workspace.id,
    )


def _conversation(conversation_repository, workspace_id, user_id):
    return _run(conversation_repository.create_conversation(workspace_id, user_id)).id


def _pending(attachment_repository, workspace_id, user_id, conversation_id, name="photo.jpg"):
    return _run(attachment_repository.create_pending(
        workspace_id=workspace_id, telegram_user_id=user_id, conversation_id=conversation_id,
        original_filename=name, stored_filename=f"stored-{name}", content_type="image/jpeg",
        kind="image", size_bytes=1234,
    ))


# ── create_pending / get_pending_for_conversation ──────────────────────────

def test_create_pending_has_message_id_none(repos):
    _, conversation_repository, attachment_repository, ws_a, _ = repos
    conv = _conversation(conversation_repository, ws_a, WORKSPACE_A_OWNER)

    attachment = _pending(attachment_repository, ws_a, WORKSPACE_A_OWNER, conv)

    assert attachment.message_id is None
    assert attachment.public_id
    assert attachment.workspace_id == ws_a


def test_public_ids_are_unique_and_high_entropy(repos):
    _, conversation_repository, attachment_repository, ws_a, _ = repos
    conv = _conversation(conversation_repository, ws_a, WORKSPACE_A_OWNER)

    a = _pending(attachment_repository, ws_a, WORKSPACE_A_OWNER, conv)
    b = _pending(attachment_repository, ws_a, WORKSPACE_A_OWNER, conv)

    assert a.public_id != b.public_id
    assert len(a.public_id) >= 24


def test_get_pending_for_conversation_requires_exact_match(repos):
    _, conversation_repository, attachment_repository, ws_a, ws_b = repos
    conv_a = _conversation(conversation_repository, ws_a, WORKSPACE_A_OWNER)
    other_conv = _conversation(conversation_repository, ws_a, WORKSPACE_A_OWNER)
    attachment = _pending(attachment_repository, ws_a, WORKSPACE_A_OWNER, conv_a)

    # wrong conversation
    assert _run(attachment_repository.get_pending_for_conversation(
        ws_a, WORKSPACE_A_OWNER, other_conv, attachment.public_id,
    )) is None
    # wrong workspace
    assert _run(attachment_repository.get_pending_for_conversation(
        ws_b, WORKSPACE_A_OWNER, conv_a, attachment.public_id,
    )) is None
    # wrong user
    assert _run(attachment_repository.get_pending_for_conversation(
        ws_a, 999999, conv_a, attachment.public_id,
    )) is None
    # correct
    found = _run(attachment_repository.get_pending_for_conversation(
        ws_a, WORKSPACE_A_OWNER, conv_a, attachment.public_id,
    ))
    assert found is not None and found.id == attachment.id


# ── attach_to_message ───────────────────────────────────────────────────

def test_attach_to_message_binds_and_clears_pending_state(repos):
    _, conversation_repository, attachment_repository, ws_a, _ = repos
    conv = _conversation(conversation_repository, ws_a, WORKSPACE_A_OWNER)
    attachment = _pending(attachment_repository, ws_a, WORKSPACE_A_OWNER, conv)

    attached = _run(attachment_repository.attach_to_message(
        ws_a, WORKSPACE_A_OWNER, conv, [attachment.public_id], message_id=555,
    ))

    assert len(attached) == 1
    assert attached[0].message_id == 555
    assert _run(attachment_repository.get_pending_for_conversation(
        ws_a, WORKSPACE_A_OWNER, conv, attachment.public_id,
    )) is None


def test_attach_to_message_ignores_foreign_public_ids(repos):
    _, conversation_repository, attachment_repository, ws_a, ws_b = repos
    conv_a = _conversation(conversation_repository, ws_a, WORKSPACE_A_OWNER)
    conv_b = _conversation(conversation_repository, ws_b, WORKSPACE_B_OWNER)
    foreign = _pending(attachment_repository, ws_b, WORKSPACE_B_OWNER, conv_b)

    attached = _run(attachment_repository.attach_to_message(
        ws_a, WORKSPACE_A_OWNER, conv_a, [foreign.public_id], message_id=1,
    ))

    assert attached == []
    still_pending = _run(attachment_repository.get_pending_for_conversation(
        ws_b, WORKSPACE_B_OWNER, conv_b, foreign.public_id,
    ))
    assert still_pending is not None and still_pending.message_id is None


def test_attach_to_message_does_not_reattach_already_attached_row(repos):
    _, conversation_repository, attachment_repository, ws_a, _ = repos
    conv = _conversation(conversation_repository, ws_a, WORKSPACE_A_OWNER)
    attachment = _pending(attachment_repository, ws_a, WORKSPACE_A_OWNER, conv)
    _run(attachment_repository.attach_to_message(
        ws_a, WORKSPACE_A_OWNER, conv, [attachment.public_id], message_id=1,
    ))

    second_attempt = _run(attachment_repository.attach_to_message(
        ws_a, WORKSPACE_A_OWNER, conv, [attachment.public_id], message_id=2,
    ))

    assert second_attempt == []


# ── list_for_conversation_messages ─────────────────────────────────────

def test_list_for_conversation_messages_groups_by_message_and_excludes_pending(repos):
    _, conversation_repository, attachment_repository, ws_a, _ = repos
    conv = _conversation(conversation_repository, ws_a, WORKSPACE_A_OWNER)
    attached = _pending(attachment_repository, ws_a, WORKSPACE_A_OWNER, conv)
    _pending(attachment_repository, ws_a, WORKSPACE_A_OWNER, conv, name="still-pending.png")
    _run(attachment_repository.attach_to_message(
        ws_a, WORKSPACE_A_OWNER, conv, [attached.public_id], message_id=42,
    ))

    grouped = _run(attachment_repository.list_for_conversation_messages(
        ws_a, WORKSPACE_A_OWNER, conv,
    ))

    assert list(grouped.keys()) == [42]
    assert len(grouped[42]) == 1
    assert grouped[42][0].public_id == attached.public_id


# ── get_for_workspace (used by the content-serving endpoint) ───────────

def test_get_for_workspace_is_isolated_by_workspace_and_user(repos):
    _, conversation_repository, attachment_repository, ws_a, ws_b = repos
    conv_a = _conversation(conversation_repository, ws_a, WORKSPACE_A_OWNER)
    attachment = _pending(attachment_repository, ws_a, WORKSPACE_A_OWNER, conv_a)

    assert _run(attachment_repository.get_for_workspace(
        ws_b, WORKSPACE_B_OWNER, attachment.public_id,
    )) is None
    assert _run(attachment_repository.get_for_workspace(
        ws_a, 999999, attachment.public_id,
    )) is None
    found = _run(attachment_repository.get_for_workspace(
        ws_a, WORKSPACE_A_OWNER, attachment.public_id,
    ))
    assert found is not None


# ── delete_pending ───────────────────────────────────────────────────

def test_delete_pending_removes_a_pending_row(repos):
    _, conversation_repository, attachment_repository, ws_a, _ = repos
    conv = _conversation(conversation_repository, ws_a, WORKSPACE_A_OWNER)
    attachment = _pending(attachment_repository, ws_a, WORKSPACE_A_OWNER, conv)

    deleted = _run(attachment_repository.delete_pending(
        ws_a, WORKSPACE_A_OWNER, attachment.public_id,
    ))

    assert deleted is not None
    assert _run(attachment_repository.get_for_workspace(
        ws_a, WORKSPACE_A_OWNER, attachment.public_id,
    )) is None


def test_delete_pending_cannot_delete_an_already_attached_row(repos):
    _, conversation_repository, attachment_repository, ws_a, _ = repos
    conv = _conversation(conversation_repository, ws_a, WORKSPACE_A_OWNER)
    attachment = _pending(attachment_repository, ws_a, WORKSPACE_A_OWNER, conv)
    _run(attachment_repository.attach_to_message(
        ws_a, WORKSPACE_A_OWNER, conv, [attachment.public_id], message_id=7,
    ))

    deleted = _run(attachment_repository.delete_pending(
        ws_a, WORKSPACE_A_OWNER, attachment.public_id,
    ))

    assert deleted is None
    still_there = _run(attachment_repository.get_for_workspace(
        ws_a, WORKSPACE_A_OWNER, attachment.public_id,
    ))
    assert still_there is not None


# ── delete_orphans_older_than ───────────────────────────────────────

def test_delete_orphans_older_than_only_removes_old_pending_rows(repos):
    _, conversation_repository, attachment_repository, ws_a, _ = repos
    conv = _conversation(conversation_repository, ws_a, WORKSPACE_A_OWNER)
    old_orphan = _pending(attachment_repository, ws_a, WORKSPACE_A_OWNER, conv, name="old.jpg")
    recent_orphan = _pending(attachment_repository, ws_a, WORKSPACE_A_OWNER, conv, name="recent.jpg")
    attached = _pending(attachment_repository, ws_a, WORKSPACE_A_OWNER, conv, name="attached.jpg")
    _run(attachment_repository.attach_to_message(
        ws_a, WORKSPACE_A_OWNER, conv, [attached.public_id], message_id=9,
    ))

    # backdate only the "old" orphan directly in the DB
    async def _backdate():
        import aiosqlite
        async with aiosqlite.connect(attachment_repository.db_path) as db:
            await db.execute(
                "UPDATE web_attachments SET created_at = ? WHERE id = ?",
                ((datetime.now(timezone.utc) - timedelta(days=2)).isoformat(), old_orphan.id),
            )
            await db.commit()
    _run(_backdate())

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    reaped = _run(attachment_repository.delete_orphans_older_than(cutoff))

    assert [a.id for a in reaped] == [old_orphan.id]
    assert _run(attachment_repository.get_for_workspace(
        ws_a, WORKSPACE_A_OWNER, recent_orphan.public_id,
    )) is not None
    assert _run(attachment_repository.get_for_workspace(
        ws_a, WORKSPACE_A_OWNER, attached.public_id,
    )) is not None


# ── delete_for_conversation (future "delete conversation" hook) ────────

def test_delete_for_conversation_removes_pending_and_attached_rows(repos):
    _, conversation_repository, attachment_repository, ws_a, _ = repos
    conv = _conversation(conversation_repository, ws_a, WORKSPACE_A_OWNER)
    other_conv = _conversation(conversation_repository, ws_a, WORKSPACE_A_OWNER)
    pending = _pending(attachment_repository, ws_a, WORKSPACE_A_OWNER, conv)
    attached = _pending(attachment_repository, ws_a, WORKSPACE_A_OWNER, conv, name="a2.jpg")
    _run(attachment_repository.attach_to_message(
        ws_a, WORKSPACE_A_OWNER, conv, [attached.public_id], message_id=3,
    ))
    untouched = _pending(attachment_repository, ws_a, WORKSPACE_A_OWNER, other_conv)

    deleted = _run(attachment_repository.delete_for_conversation(ws_a, WORKSPACE_A_OWNER, conv))

    assert {a.id for a in deleted} == {pending.id, attached.id}
    assert _run(attachment_repository.get_for_workspace(
        ws_a, WORKSPACE_A_OWNER, untouched.public_id,
    )) is not None
