from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from app.repositories.partner_repository import PartnerRepository
from app.repositories.web_conversation_repository import (
    WebConversationRepository,
    derive_conversation_title,
)

OWNER_ID = 586249067
OTHER_USER_ID = 700000001


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _setup(tmp_path: Path) -> tuple[WebConversationRepository, int, int]:
    """Real workspace(s) via PartnerRepository, matching
    tests/test_artifact_repository.py's convention exactly."""
    db_path = tmp_path / "journal.sqlite3"
    partners = PartnerRepository(db_path)
    _run(partners.init())
    owner_workspace, _ = _run(partners.ensure_owner_workspace(OWNER_ID))
    other_workspace_id = _insert_workspace(db_path, "other-workspace")
    conversations = WebConversationRepository(db_path)
    _run(conversations.init())
    return conversations, owner_workspace.id, other_workspace_id


def _insert_workspace(db_path: Path, slug: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as db:
        cursor = db.execute(
            "INSERT INTO partner_workspaces "
            "(name, slug, status, created_at, updated_at) "
            "VALUES (?, ?, 'active', ?, ?)",
            (slug, slug, now, now),
        )
        return cursor.lastrowid or 0


# ── schema ────────────────────────────────────────────────────────────────

def test_schema_creates_tables_in_empty_database(tmp_path: Path) -> None:
    repository = WebConversationRepository(tmp_path / "empty.sqlite3")

    _run(repository.init())

    with sqlite3.connect(repository.db_path) as db:
        tables = {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert {"web_conversations", "web_conversation_messages"} <= tables


def test_init_is_idempotent(tmp_path: Path) -> None:
    repository = WebConversationRepository(tmp_path / "journal.sqlite3")
    _run(repository.init())
    _run(repository.init())  # must not raise


# ── create / list / get ──────────────────────────────────────────────────

def test_create_conversation_has_a_default_title(tmp_path: Path) -> None:
    repository, workspace_id, _ = _setup(tmp_path)

    conversation = _run(repository.create_conversation(workspace_id, OWNER_ID))

    assert conversation.title == "Новый диалог"
    assert conversation.workspace_id == workspace_id
    assert conversation.telegram_user_id == OWNER_ID
    assert conversation.created_at == conversation.updated_at


def test_get_conversation_returns_none_for_unknown_id(tmp_path: Path) -> None:
    repository, workspace_id, _ = _setup(tmp_path)

    assert _run(repository.get_conversation(workspace_id, OWNER_ID, 999999)) is None


def test_list_conversations_orders_by_updated_at_desc(tmp_path: Path) -> None:
    repository, workspace_id, _ = _setup(tmp_path)
    first = _run(repository.create_conversation(workspace_id, OWNER_ID))
    second = _run(repository.create_conversation(workspace_id, OWNER_ID))
    third = _run(repository.create_conversation(workspace_id, OWNER_ID))

    # touch `first` last, via a real message - it must jump to the top.
    _run(repository.add_message(workspace_id, OWNER_ID, first.id, "user", "Привет"))

    listed = _run(repository.list_conversations(workspace_id, OWNER_ID))

    assert [c.id for c in listed] == [first.id, third.id, second.id]


# ── messages: add / list, ownership enforced ─────────────────────────────

def test_add_message_and_list_messages_round_trip_in_order(tmp_path: Path) -> None:
    repository, workspace_id, _ = _setup(tmp_path)
    conversation = _run(repository.create_conversation(workspace_id, OWNER_ID))

    user_message = _run(repository.add_message(
        workspace_id, OWNER_ID, conversation.id, "user", "Привет, как дела?",
    ))
    assistant_message = _run(repository.add_message(
        workspace_id, OWNER_ID, conversation.id, "assistant", "Привет! Всё отлично.",
    ))

    assert user_message is not None and user_message.role == "user"
    assert assistant_message is not None and assistant_message.role == "assistant"

    messages = _run(repository.list_messages(workspace_id, OWNER_ID, conversation.id))

    assert [m.content for m in messages] == ["Привет, как дела?", "Привет! Всё отлично."]
    assert [m.role for m in messages] == ["user", "assistant"]


def test_add_message_bumps_conversation_updated_at(tmp_path: Path) -> None:
    repository, workspace_id, _ = _setup(tmp_path)
    conversation = _run(repository.create_conversation(workspace_id, OWNER_ID))
    original_updated_at = conversation.updated_at

    _run(repository.add_message(workspace_id, OWNER_ID, conversation.id, "user", "Привет"))

    refreshed = _run(repository.get_conversation(workspace_id, OWNER_ID, conversation.id))
    assert refreshed.updated_at >= original_updated_at


def test_add_message_rejects_invalid_role(tmp_path: Path) -> None:
    repository, workspace_id, _ = _setup(tmp_path)
    conversation = _run(repository.create_conversation(workspace_id, OWNER_ID))

    result = _run(repository.add_message(
        workspace_id, OWNER_ID, conversation.id, "system", "Скрытый системный промпт",
    ))

    assert result is None
    assert _run(repository.list_messages(workspace_id, OWNER_ID, conversation.id)) == []


def test_add_message_rejects_blank_content(tmp_path: Path) -> None:
    repository, workspace_id, _ = _setup(tmp_path)
    conversation = _run(repository.create_conversation(workspace_id, OWNER_ID))

    result = _run(repository.add_message(workspace_id, OWNER_ID, conversation.id, "user", "   "))

    assert result is None


def test_add_message_to_unknown_conversation_returns_none_and_persists_nothing(
    tmp_path: Path,
) -> None:
    repository, workspace_id, _ = _setup(tmp_path)

    result = _run(repository.add_message(workspace_id, OWNER_ID, 999999, "user", "Привет"))

    assert result is None
    with sqlite3.connect(repository.db_path) as db:
        count = db.execute("SELECT COUNT(*) FROM web_conversation_messages").fetchone()[0]
    assert count == 0


def test_list_messages_for_unknown_conversation_is_empty_not_an_error(tmp_path: Path) -> None:
    repository, workspace_id, _ = _setup(tmp_path)

    assert _run(repository.list_messages(workspace_id, OWNER_ID, 999999)) == []


# ── workspace isolation ──────────────────────────────────────────────────

def test_conversations_are_isolated_by_workspace(tmp_path: Path) -> None:
    repository, workspace_id, other_workspace_id = _setup(tmp_path)
    mine = _run(repository.create_conversation(workspace_id, OWNER_ID))
    _run(repository.create_conversation(other_workspace_id, OWNER_ID))

    listed = _run(repository.list_conversations(workspace_id, OWNER_ID))
    assert [c.id for c in listed] == [mine.id]

    assert _run(repository.get_conversation(other_workspace_id, OWNER_ID, mine.id)) is None


def test_cannot_read_or_write_messages_across_workspaces(tmp_path: Path) -> None:
    repository, workspace_id, other_workspace_id = _setup(tmp_path)
    conversation = _run(repository.create_conversation(workspace_id, OWNER_ID))
    _run(repository.add_message(workspace_id, OWNER_ID, conversation.id, "user", "Моё сообщение"))

    foreign_read = _run(repository.list_messages(other_workspace_id, OWNER_ID, conversation.id))
    assert foreign_read == []

    foreign_write = _run(repository.add_message(
        other_workspace_id, OWNER_ID, conversation.id, "user", "Чужая попытка дописать",
    ))
    assert foreign_write is None
    # original conversation is unaffected
    assert [m.content for m in _run(
        repository.list_messages(workspace_id, OWNER_ID, conversation.id)
    )] == ["Моё сообщение"]


# ── user isolation (within the SAME workspace) ───────────────────────────

def test_conversations_are_isolated_by_user_within_same_workspace(tmp_path: Path) -> None:
    repository, workspace_id, _ = _setup(tmp_path)
    mine = _run(repository.create_conversation(workspace_id, OWNER_ID))
    _run(repository.create_conversation(workspace_id, OTHER_USER_ID))

    listed = _run(repository.list_conversations(workspace_id, OWNER_ID))
    assert [c.id for c in listed] == [mine.id]

    assert _run(repository.get_conversation(workspace_id, OTHER_USER_ID, mine.id)) is None


def test_cannot_read_or_write_messages_across_users(tmp_path: Path) -> None:
    repository, workspace_id, _ = _setup(tmp_path)
    conversation = _run(repository.create_conversation(workspace_id, OWNER_ID))
    _run(repository.add_message(workspace_id, OWNER_ID, conversation.id, "user", "Моё сообщение"))

    foreign_read = _run(repository.list_messages(workspace_id, OTHER_USER_ID, conversation.id))
    assert foreign_read == []

    foreign_write = _run(repository.add_message(
        workspace_id, OTHER_USER_ID, conversation.id, "user", "Чужая попытка дописать",
    ))
    assert foreign_write is None
    assert [m.content for m in _run(
        repository.list_messages(workspace_id, OWNER_ID, conversation.id)
    )] == ["Моё сообщение"]


# ── title update (internal, used by web_api.py after the first message) ──

def test_set_conversation_title_updates_it(tmp_path: Path) -> None:
    repository, workspace_id, _ = _setup(tmp_path)
    conversation = _run(repository.create_conversation(workspace_id, OWNER_ID))

    updated = _run(repository.set_conversation_title(
        workspace_id, OWNER_ID, conversation.id, "Вопрос про Silver",
    ))

    assert updated is not None
    assert updated.title == "Вопрос про Silver"


def test_set_conversation_title_isolated_by_workspace_and_user(tmp_path: Path) -> None:
    repository, workspace_id, other_workspace_id = _setup(tmp_path)
    conversation = _run(repository.create_conversation(workspace_id, OWNER_ID))

    result = _run(repository.set_conversation_title(
        other_workspace_id, OWNER_ID, conversation.id, "Захват",
    ))
    assert result is None
    unchanged = _run(repository.get_conversation(workspace_id, OWNER_ID, conversation.id))
    assert unchanged.title == "Новый диалог"


# ── persistence across a fresh repository instance ───────────────────────

def test_data_survives_a_new_repository_instance_against_the_same_file(tmp_path: Path) -> None:
    """The whole point of this feature: closing the browser (or the process)
    must not lose anything - a brand new WebConversationRepository object
    pointed at the same db file must see exactly what was saved before."""
    db_path = tmp_path / "journal.sqlite3"
    partners = PartnerRepository(db_path)
    _run(partners.init())
    owner_workspace, _ = _run(partners.ensure_owner_workspace(OWNER_ID))

    first_instance = WebConversationRepository(db_path)
    _run(first_instance.init())
    conversation = _run(first_instance.create_conversation(owner_workspace.id, OWNER_ID))
    _run(first_instance.add_message(
        owner_workspace.id, OWNER_ID, conversation.id, "user", "Привет",
    ))
    _run(first_instance.add_message(
        owner_workspace.id, OWNER_ID, conversation.id, "assistant", "Здравствуйте!",
    ))
    _run(first_instance.set_conversation_title(
        owner_workspace.id, OWNER_ID, conversation.id, "Приветствие",
    ))

    second_instance = WebConversationRepository(db_path)
    # deliberately NOT calling init() again - proves this is real file
    # persistence, not an in-process cache.
    reloaded_conversations = _run(
        second_instance.list_conversations(owner_workspace.id, OWNER_ID)
    )
    reloaded_messages = _run(
        second_instance.list_messages(owner_workspace.id, OWNER_ID, conversation.id)
    )

    assert len(reloaded_conversations) == 1
    assert reloaded_conversations[0].title == "Приветствие"
    assert [m.content for m in reloaded_messages] == ["Привет", "Здравствуйте!"]


# ── derive_conversation_title(): no LLM call, clean truncation ───────────

def test_derive_conversation_title_uses_the_message_verbatim_when_short() -> None:
    assert derive_conversation_title("Расскажи про Elite Turbo") == "Расскажи про Elite Turbo"


def test_derive_conversation_title_collapses_whitespace() -> None:
    assert derive_conversation_title("  Привет   \n\n мир  ") == "Привет мир"


def test_derive_conversation_title_falls_back_to_default_when_blank() -> None:
    assert derive_conversation_title("   ") == "Новый диалог"
    assert derive_conversation_title("") == "Новый диалог"


def test_derive_conversation_title_truncates_long_messages_cleanly() -> None:
    long_message = "Расскажи подробно " * 20  # well past 80 chars

    title = derive_conversation_title(long_message)

    assert len(title) <= 80
    assert title.endswith("…")
    # never cuts mid-word: strip the ellipsis and the remainder must be a
    # prefix of the original, whitespace-normalized text ending on a word
    # boundary (no dangling partial token stuck to the ellipsis).
    normalized = " ".join(long_message.split())
    body = title[:-1].rstrip()
    assert normalized.startswith(body)
    assert normalized[len(body):len(body) + 1] in (" ", "")
