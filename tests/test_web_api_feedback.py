"""POST /api/feedback - 👍/👎 on a single assistant chat message. Requires
the web-only dependencies (requirements-web.txt); skips cleanly when
they're not installed.
"""

from __future__ import annotations

import asyncio

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("markdown")
pytest.importorskip("argon2")

from fastapi.testclient import TestClient  # noqa: E402

from app.domain.web_conversation import ROLE_ASSISTANT, ROLE_USER  # noqa: E402
from tests._web_auth_test_helpers import login_as  # noqa: E402

OWNER_ID = 586249067


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def api(tmp_path, monkeypatch):
    db_path = tmp_path / "journal.sqlite3"
    monkeypatch.setenv("JOURNAL_DB_PATH", str(db_path))
    monkeypatch.setenv("PLANNER_OPENAI_API_KEY", "test-key")

    import sys
    sys.modules.pop("app.web_api", None)
    import app.web_api as web_api

    with TestClient(web_api.app, base_url="https://testserver") as client:
        ws, _ = _run(web_api.partner_repository.ensure_owner_workspace(OWNER_ID))
        login_as(client, web_api, ws.id, OWNER_ID)
        yield client, web_api, ws.id

    sys.modules.pop("app.web_api", None)


def _seed_conversation_with_assistant_message(web_api, workspace_id):
    conversation = _run(web_api.web_conversation_repository.create_conversation(
        workspace_id, OWNER_ID,
    ))
    _run(web_api.web_conversation_repository.add_message(
        workspace_id, OWNER_ID, conversation.id, ROLE_USER, "Вопрос",
    ))
    message = _run(web_api.web_conversation_repository.add_message(
        workspace_id, OWNER_ID, conversation.id, ROLE_ASSISTANT, "Ответ ассистента",
    ))
    return conversation.id, message.id


def test_thumbs_up_submits_without_a_reason(api) -> None:
    client, web_api, workspace_id = api
    conversation_id, message_id = _seed_conversation_with_assistant_message(web_api, workspace_id)

    response = client.post("/api/feedback", json={
        "conversation_id": conversation_id, "message_id": message_id, "rating": "up",
    })

    assert response.status_code == 200
    body = response.json()
    assert body["feedback"]["rating"] == "up"


def test_thumbs_down_with_reason_and_comment(api) -> None:
    client, web_api, workspace_id = api
    conversation_id, message_id = _seed_conversation_with_assistant_message(web_api, workspace_id)

    response = client.post("/api/feedback", json={
        "conversation_id": conversation_id, "message_id": message_id, "rating": "down",
        "reason": "too_generic", "comment": "Мало конкретики",
    })

    assert response.status_code == 200
    body = response.json()
    assert body["feedback"]["rating"] == "down"
    assert body["feedback"]["reason"] == "too_generic"
    assert body["feedback"]["comment"] == "Мало конкретики"


def test_invalid_rating_is_rejected(api) -> None:
    client, web_api, workspace_id = api
    conversation_id, message_id = _seed_conversation_with_assistant_message(web_api, workspace_id)

    response = client.post("/api/feedback", json={
        "conversation_id": conversation_id, "message_id": message_id, "rating": "sideways",
    })
    assert "error" in response.json()


def test_invalid_reason_is_rejected(api) -> None:
    client, web_api, workspace_id = api
    conversation_id, message_id = _seed_conversation_with_assistant_message(web_api, workspace_id)

    response = client.post("/api/feedback", json={
        "conversation_id": conversation_id, "message_id": message_id, "rating": "down",
        "reason": "not-a-real-reason-code",
    })
    assert "error" in response.json()


def test_feedback_on_a_user_message_is_rejected(api) -> None:
    """Only assistant messages can receive feedback."""
    client, web_api, workspace_id = api
    conversation = _run(web_api.web_conversation_repository.create_conversation(workspace_id, OWNER_ID))
    user_message = _run(web_api.web_conversation_repository.add_message(
        workspace_id, OWNER_ID, conversation.id, ROLE_USER, "Вопрос",
    ))

    response = client.post("/api/feedback", json={
        "conversation_id": conversation.id, "message_id": user_message.id, "rating": "up",
    })
    assert "error" in response.json()


def test_feedback_on_a_foreign_conversation_is_rejected(api) -> None:
    """message_id/conversation_id must actually belong to the caller's own
    (workspace_id, telegram_user_id) - never trusted at face value."""
    client, web_api, workspace_id = api
    other = _run(web_api.partner_repository.provision_partner(
        222333444, "Other Agency", "other-agency-fb",
        business_name="Other Agency", business_type="independent_agent",
        short_description="Другое пространство.", context={},
    ))
    foreign_conversation_id, foreign_message_id = _seed_conversation_with_assistant_message(
        web_api, other.workspace.id,
    )
    # The foreign conversation was created under a different
    # telegram_user_id too - re-seed using this test's OWNER_ID identity
    # is not possible since _seed_conversation_with_assistant_message
    # always uses OWNER_ID; construct directly instead.
    foreign_conversation = _run(web_api.web_conversation_repository.create_conversation(
        other.workspace.id, 222333444,
    ))
    foreign_message = _run(web_api.web_conversation_repository.add_message(
        other.workspace.id, 222333444, foreign_conversation.id, ROLE_ASSISTANT, "Чужой ответ",
    ))

    response = client.post("/api/feedback", json={
        "conversation_id": foreign_conversation.id, "message_id": foreign_message.id, "rating": "up",
    })
    assert "error" in response.json()


def test_feedback_is_associated_with_the_correct_workspace_and_message(api) -> None:
    client, web_api, workspace_id = api
    conversation_id, message_id = _seed_conversation_with_assistant_message(web_api, workspace_id)

    client.post("/api/feedback", json={
        "conversation_id": conversation_id, "message_id": message_id, "rating": "up",
    })

    stored = _run(web_api.feedback_repository.list_recent(workspace_id=workspace_id))
    assert len(stored) == 1
    assert stored[0].conversation_id == conversation_id
    assert stored[0].message_id == message_id
    assert stored[0].workspace_id == workspace_id


def test_resubmitting_feedback_updates_not_duplicates(api) -> None:
    client, web_api, workspace_id = api
    conversation_id, message_id = _seed_conversation_with_assistant_message(web_api, workspace_id)

    client.post("/api/feedback", json={
        "conversation_id": conversation_id, "message_id": message_id, "rating": "up",
    })
    client.post("/api/feedback", json={
        "conversation_id": conversation_id, "message_id": message_id, "rating": "down",
        "reason": "wrong_answer",
    })

    stored = _run(web_api.feedback_repository.list_recent(workspace_id=workspace_id))
    assert len(stored) == 1
    assert stored[0].rating.value == "down"


def test_feedback_does_not_copy_the_conversation_content(api) -> None:
    """Only references (conversation_id/message_id) are stored - never the
    message text itself."""
    client, web_api, workspace_id = api
    conversation_id, message_id = _seed_conversation_with_assistant_message(web_api, workspace_id)

    client.post("/api/feedback", json={
        "conversation_id": conversation_id, "message_id": message_id, "rating": "down",
        "reason": "wrong_answer",
    })

    stored = _run(web_api.feedback_repository.list_recent(workspace_id=workspace_id))
    assert "Ответ ассистента" not in (stored[0].comment or "")


def test_feedback_survives_without_active_subscription(api) -> None:
    """Feedback is not itself a paid product feature - it stays reachable
    even for an expired workspace, unlike chat/materials/etc."""
    client, web_api, workspace_id = api
    conversation_id, message_id = _seed_conversation_with_assistant_message(web_api, workspace_id)
    _run(web_api.subscription_repository.mark_expired(workspace_id))

    response = client.post("/api/feedback", json={
        "conversation_id": conversation_id, "message_id": message_id, "rating": "up",
    })
    assert response.status_code == 200
    assert "error" not in response.json()
