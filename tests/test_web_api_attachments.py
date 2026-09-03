"""HTTP surface for web-Ассистент attachments: POST /api/attachments
(upload), DELETE /api/attachments/{id}, GET /api/attachments/{id}/content,
and their integration into POST /api/chat + GET
/api/conversations/{id}/messages.

No test hits the real OpenAI API - app.web_api.chat_provider.generate is
monkeypatched everywhere a chat turn is exercised, same convention as
tests/test_web_api_conversations.py.

Requires the web-only dependencies (fastapi, uvicorn, markdown,
python-multipart). Skips cleanly when they're not installed.
"""

from __future__ import annotations

import asyncio

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("markdown")

from fastapi.testclient import TestClient  # noqa: E402

from app.chat_provider import ChatResult  # noqa: E402

from tests._web_auth_test_helpers import login_as  # noqa: E402

OWNER_ID = 586249067

_JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 40
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
_PDF_BYTES = b"%PDF-1.7\n" + b"\x00" * 40
_TXT_BYTES = "Отчёт: цены выросли на 20%.".encode("utf-8")


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


def _fake_generate(text="Ответ ассистента", captured=None):
    def _generate(**kwargs):
        if captured is not None:
            captured.update(kwargs)
        return ChatResult(text=text, usage=None)
    return _generate


def _new_conversation(client) -> int:
    return client.post("/api/conversations").json()["conversation"]["id"]


def _upload(client, conversation_id, files):
    return client.post(
        "/api/attachments",
        data={"conversation_id": str(conversation_id)},
        files=files,
    )


# ── POST /api/attachments: supported formats ───────────────────────────

def test_upload_jpg_succeeds(api) -> None:
    client, _, _ = api
    conversation_id = _new_conversation(client)

    response = _upload(client, conversation_id, [("files", ("photo.jpg", _JPEG_BYTES, "image/jpeg"))])

    assert response.status_code == 200
    body = response.json()
    assert "error" not in body
    attachment = body["attachments"][0]
    assert attachment["filename"] == "photo.jpg"
    assert attachment["kind"] == "image"
    assert attachment["content_type"] == "image/jpeg"
    assert attachment["size_bytes"] == len(_JPEG_BYTES)
    assert attachment["id"]


def test_upload_png_succeeds(api) -> None:
    client, _, _ = api
    conversation_id = _new_conversation(client)

    response = _upload(client, conversation_id, [("files", ("screen.png", _PNG_BYTES, "image/png"))])

    assert response.status_code == 200
    assert response.json()["attachments"][0]["kind"] == "image"


def test_upload_pdf_succeeds(api) -> None:
    client, _, _ = api
    conversation_id = _new_conversation(client)

    response = _upload(client, conversation_id, [("files", ("hotel.pdf", _PDF_BYTES, "application/pdf"))])

    assert response.status_code == 200
    assert response.json()["attachments"][0]["kind"] == "pdf"


def test_upload_txt_succeeds(api) -> None:
    client, _, _ = api
    conversation_id = _new_conversation(client)

    response = _upload(client, conversation_id, [("files", ("notes.txt", _TXT_BYTES, "text/plain"))])

    assert response.status_code == 200
    assert response.json()["attachments"][0]["kind"] == "text"


def test_upload_multiple_files_at_once(api) -> None:
    client, _, _ = api
    conversation_id = _new_conversation(client)

    response = _upload(client, conversation_id, [
        ("files", ("a.jpg", _JPEG_BYTES, "image/jpeg")),
        ("files", ("b.pdf", _PDF_BYTES, "application/pdf")),
    ])

    assert response.status_code == 200
    assert len(response.json()["attachments"]) == 2


# ── rejections ───────────────────────────────────────────────────────

def test_upload_rejects_unsupported_type(api) -> None:
    client, _, _ = api
    conversation_id = _new_conversation(client)

    response = _upload(client, conversation_id, [
        ("files", ("evil.svg", b"<svg onload=alert(1)></svg>", "image/svg+xml")),
    ])

    assert response.status_code == 200
    body = response.json()
    assert body["attachments"] == []
    assert "error" in body


def test_upload_rejects_oversize_file(api, monkeypatch) -> None:
    client, web_api, _ = api
    monkeypatch.setattr(web_api, "MAX_FILE_SIZE_BYTES", 100)
    conversation_id = _new_conversation(client)

    response = _upload(client, conversation_id, [
        ("files", ("big.jpg", _JPEG_BYTES + b"\x00" * 500, "image/jpeg")),
    ])

    assert response.status_code == 200
    body = response.json()
    assert body["attachments"] == []
    assert "error" in body
    assert "МБ" in body["error"]


def test_upload_rejects_too_many_files(api) -> None:
    client, web_api, _ = api
    conversation_id = _new_conversation(client)
    too_many = web_api.MAX_FILES_PER_UPLOAD + 1

    response = _upload(client, conversation_id, [
        ("files", (f"f{i}.txt", _TXT_BYTES, "text/plain")) for i in range(too_many)
    ])

    assert response.status_code == 200
    body = response.json()
    assert body["attachments"] == []
    assert "error" in body


def test_upload_rejects_content_mismatched_with_extension(api) -> None:
    client, _, _ = api
    conversation_id = _new_conversation(client)

    response = _upload(client, conversation_id, [
        ("files", ("fake.png", _PDF_BYTES, "image/png")),
    ])

    body = response.json()
    assert body["attachments"] == []
    assert "error" in body


def test_upload_rejects_unknown_conversation(api) -> None:
    client, _, _ = api

    response = _upload(client, 999999, [("files", ("a.jpg", _JPEG_BYTES, "image/jpeg"))])

    body = response.json()
    assert body["attachments"] == []
    assert "error" in body


# ── filename / path traversal sanitization ─────────────────────────────

def test_upload_sanitizes_path_traversal_filename(api) -> None:
    client, web_api, workspace_id = api
    conversation_id = _new_conversation(client)

    response = _upload(client, conversation_id, [
        ("files", ("../../etc/passwd.jpg", _JPEG_BYTES, "image/jpeg")),
    ])

    body = response.json()
    assert "error" not in body
    assert body["attachments"][0]["filename"] == "passwd.jpg"

    stored = _run(web_api.web_attachment_repository.get_for_workspace(
        workspace_id, OWNER_ID, body["attachments"][0]["id"],
    ))
    assert "/" not in stored.stored_filename
    assert "\\" not in stored.stored_filename
    assert ".." not in stored.stored_filename


def test_uploaded_file_is_stored_outside_any_static_directory(api) -> None:
    client, web_api, workspace_id = api
    conversation_id = _new_conversation(client)

    response = _upload(client, conversation_id, [
        ("files", ("photo.jpg", _JPEG_BYTES, "image/jpeg")),
    ])
    public_id = response.json()["attachments"][0]["id"]
    stored = _run(web_api.web_attachment_repository.get_for_workspace(
        workspace_id, OWNER_ID, public_id,
    ))

    path = web_api.attachment_storage._path(
        stored.workspace_id, stored.conversation_id, stored.stored_filename,
    )
    assert path.is_file()
    assert "web_uploads" in path.parts
    assert path.name != "photo.jpg"


# ── CSRF ────────────────────────────────────────────────────────────

def test_upload_requires_csrf(api) -> None:
    client, web_api, workspace_id = api
    conversation_id = _new_conversation(client)

    token = client.headers.pop("X-CSRF-Token")
    try:
        response = _upload(client, conversation_id, [
            ("files", ("photo.jpg", _JPEG_BYTES, "image/jpeg")),
        ])
    finally:
        client.headers["X-CSRF-Token"] = token

    assert response.status_code == 403
    assert _run(web_api.web_attachment_repository.list_for_conversation_messages(
        workspace_id, OWNER_ID, conversation_id,
    )) == {}


def test_delete_attachment_requires_csrf(api) -> None:
    client, web_api, workspace_id = api
    conversation_id = _new_conversation(client)
    public_id = _upload(client, conversation_id, [
        ("files", ("photo.jpg", _JPEG_BYTES, "image/jpeg")),
    ]).json()["attachments"][0]["id"]

    token = client.headers.pop("X-CSRF-Token")
    try:
        response = client.delete(f"/api/attachments/{public_id}")
    finally:
        client.headers["X-CSRF-Token"] = token

    assert response.status_code == 403
    assert _run(web_api.web_attachment_repository.get_for_workspace(
        workspace_id, OWNER_ID, public_id,
    )) is not None


# ── DELETE /api/attachments/{id}: remove a pending chip ─────────────────

def test_delete_pending_attachment_removes_it(api) -> None:
    client, web_api, workspace_id = api
    conversation_id = _new_conversation(client)
    public_id = _upload(client, conversation_id, [
        ("files", ("photo.jpg", _JPEG_BYTES, "image/jpeg")),
    ]).json()["attachments"][0]["id"]

    response = client.delete(f"/api/attachments/{public_id}")

    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert _run(web_api.web_attachment_repository.get_for_workspace(
        workspace_id, OWNER_ID, public_id,
    )) is None


# ── workspace isolation ────────────────────────────────────────────────

def test_upload_cannot_target_another_workspaces_conversation(api) -> None:
    client, web_api, workspace_id = api
    conversation_id = _new_conversation(client)

    other = _run(web_api.partner_repository.provision_partner(
        222333777, "Other Agency", "other-agency-attachments",
        business_name="Other Agency", business_type="independent_agent",
        short_description="Другое рабочее пространство.", context={},
    ))
    with TestClient(web_api.app, base_url="https://testserver") as intruder:
        login_as(intruder, web_api, other.workspace.id, 222333777, email="intruder-ws@example.com")

        response = _upload(intruder, conversation_id, [
            ("files", ("photo.jpg", _JPEG_BYTES, "image/jpeg")),
        ])

        body = response.json()
        assert body["attachments"] == []
        assert "error" in body


def test_attachment_content_not_accessible_from_another_workspace(api) -> None:
    client, web_api, workspace_id = api
    conversation_id = _new_conversation(client)
    public_id = _upload(client, conversation_id, [
        ("files", ("photo.jpg", _JPEG_BYTES, "image/jpeg")),
    ]).json()["attachments"][0]["id"]

    other = _run(web_api.partner_repository.provision_partner(
        222333888, "Other Agency 2", "other-agency-attachments-2",
        business_name="Other Agency 2", business_type="independent_agent",
        short_description="Другое рабочее пространство.", context={},
    ))
    with TestClient(web_api.app, base_url="https://testserver") as intruder:
        login_as(intruder, web_api, other.workspace.id, 222333888, email="intruder-content@example.com")

        response = intruder.get(f"/api/attachments/{public_id}/content")
        assert response.status_code == 404


# ── user isolation (same workspace, different member) ──────────────────

def test_pending_attachment_not_visible_to_another_member_of_same_workspace(api) -> None:
    client, web_api, workspace_id = api
    conversation_id = _new_conversation(client)
    public_id = _upload(client, conversation_id, [
        ("files", ("photo.jpg", _JPEG_BYTES, "image/jpeg")),
    ]).json()["attachments"][0]["id"]

    with TestClient(web_api.app, base_url="https://testserver") as member_client:
        login_as(
            member_client, web_api, workspace_id, 700000111,
            role="member", email="member-attachments@example.com",
        )

        content_response = member_client.get(f"/api/attachments/{public_id}/content")
        assert content_response.status_code == 404

        # can't attach someone else's pending upload to their own message
        member_conversation_id = _new_conversation(member_client)
        chat_response = member_client.post("/api/chat", json={
            "message": "Разбери это", "conversation_id": member_conversation_id,
            "attachment_ids": [public_id],
        })
        assert "error" in chat_response.json()


# ── attachment <-> conversation/message linkage + provider integration ──

def test_attachment_reaches_provider_with_text_and_appears_in_history(api) -> None:
    client, web_api, workspace_id = api
    captured = {}
    web_api.chat_provider.generate = _fake_generate(text="Вижу вложение.", captured=captured)
    conversation_id = _new_conversation(client)

    public_id = _upload(client, conversation_id, [
        ("files", ("offer.jpg", _JPEG_BYTES, "image/jpeg")),
    ]).json()["attachments"][0]["id"]

    response = client.post("/api/chat", json={
        "message": "Разбери это предложение",
        "conversation_id": conversation_id,
        "attachment_ids": [public_id],
    })

    assert response.status_code == 200
    assert response.json()["answer"] == "Вижу вложение."

    provider_attachments = captured["attachments"]
    assert len(provider_attachments) == 1
    assert provider_attachments[0].kind == "image"
    assert provider_attachments[0].filename == "offer.jpg"
    assert provider_attachments[0].data_base64  # actual bytes, not just the filename

    history_response = client.get(f"/api/conversations/{conversation_id}/messages")
    messages = history_response.json()["messages"]
    user_message = next(m for m in messages if m["role"] == "user")
    assert user_message["content"] == "Разбери это предложение"
    assert len(user_message["attachments"]) == 1
    assert user_message["attachments"][0]["filename"] == "offer.jpg"
    assert user_message["attachments"][0]["id"] == public_id


def test_file_only_message_is_accepted_and_stored_with_placeholder_text(api) -> None:
    client, web_api, _ = api
    web_api.chat_provider.generate = _fake_generate(text="Вижу файл.")
    conversation_id = _new_conversation(client)
    public_id = _upload(client, conversation_id, [
        ("files", ("hotel.pdf", _PDF_BYTES, "application/pdf")),
    ]).json()["attachments"][0]["id"]

    response = client.post("/api/chat", json={
        "message": "", "conversation_id": conversation_id, "attachment_ids": [public_id],
    })

    assert response.status_code == 200
    assert response.json()["answer"] == "Вижу файл."

    messages = client.get(f"/api/conversations/{conversation_id}/messages").json()["messages"]
    user_message = next(m for m in messages if m["role"] == "user")
    assert user_message["content"]  # non-empty placeholder, not blank
    assert len(user_message["attachments"]) == 1


def test_chat_with_no_message_and_no_attachments_is_rejected(api) -> None:
    client, _, _ = api
    conversation_id = _new_conversation(client)

    response = client.post("/api/chat", json={"message": "", "conversation_id": conversation_id})

    assert response.status_code == 200
    assert "error" in response.json()


def test_chat_rejects_attachment_id_from_a_different_conversation(api) -> None:
    client, _, _ = api
    conversation_a = _new_conversation(client)
    conversation_b = _new_conversation(client)
    public_id = _upload(client, conversation_a, [
        ("files", ("photo.jpg", _JPEG_BYTES, "image/jpeg")),
    ]).json()["attachments"][0]["id"]

    response = client.post("/api/chat", json={
        "message": "Разбери", "conversation_id": conversation_b,
        "attachment_ids": [public_id],
    })

    assert "error" in response.json()


def test_chat_rejects_already_attached_attachment_id_reused(api) -> None:
    client, web_api, _ = api
    web_api.chat_provider.generate = _fake_generate()
    conversation_id = _new_conversation(client)
    public_id = _upload(client, conversation_id, [
        ("files", ("photo.jpg", _JPEG_BYTES, "image/jpeg")),
    ]).json()["attachments"][0]["id"]

    first = client.post("/api/chat", json={
        "message": "Раз", "conversation_id": conversation_id, "attachment_ids": [public_id],
    })
    assert "error" not in first.json()

    second = client.post("/api/chat", json={
        "message": "Два", "conversation_id": conversation_id, "attachment_ids": [public_id],
    })
    assert "error" in second.json()


def test_chat_rejects_too_many_attachment_ids(api) -> None:
    client, web_api, _ = api
    conversation_id = _new_conversation(client)

    response = client.post("/api/chat", json={
        "message": "Много файлов", "conversation_id": conversation_id,
        "attachment_ids": [f"fake-id-{i}" for i in range(web_api.MAX_FILES_PER_UPLOAD + 1)],
    })

    assert "error" in response.json()


# ── regression: plain chat without attachments still works ─────────────

def test_plain_chat_without_attachments_still_works(api) -> None:
    client, web_api, _ = api
    captured = {}
    web_api.chat_provider.generate = _fake_generate(text="Обычный ответ.", captured=captured)
    conversation_id = _new_conversation(client)

    response = client.post("/api/chat", json={
        "message": "Куда поехать в марте?", "conversation_id": conversation_id,
    })

    assert response.status_code == 200
    assert response.json()["answer"] == "Обычный ответ."
    assert captured["attachments"] == []

    messages = client.get(f"/api/conversations/{conversation_id}/messages").json()["messages"]
    user_message = next(m for m in messages if m["role"] == "user")
    assert user_message["attachments"] == []


def test_old_conversation_without_attachments_still_opens(api) -> None:
    """A conversation created before this feature existed has no
    web_attachments rows at all - history must still load cleanly."""
    client, web_api, _ = api
    web_api.chat_provider.generate = _fake_generate(text="Ок.")
    conversation_id = _new_conversation(client)
    client.post("/api/chat", json={"message": "Привет", "conversation_id": conversation_id})

    response = client.get(f"/api/conversations/{conversation_id}/messages")

    assert response.status_code == 200
    body = response.json()
    assert all("attachments" in m for m in body["messages"])


# ── independent (non-TA) workspace works the same way, no TA leakage ────

def test_independent_workspace_attachment_flow_has_no_ta_context(api) -> None:
    client, web_api, _ = api
    captured = {}
    independent = _run(web_api.partner_repository.provision_partner(
        700000222, "Independent Agent", "independent-attachments-test",
        business_name="Мария Турагент", business_type="independent_agent",
        short_description="", context={},
    ))

    with TestClient(web_api.app, base_url="https://testserver") as indep_client:
        login_as(
            indep_client, web_api, independent.workspace.id, 700000222,
            email="independent-attachments@example.com",
        )
        indep_client.headers  # ensure session/csrf cookie set
        web_api.chat_provider.generate = _fake_generate(text="Ок, вижу файл.", captured=captured)

        conversation_id = _new_conversation(indep_client)
        public_id = _upload(indep_client, conversation_id, [
            ("files", ("notes.txt", _TXT_BYTES, "text/plain")),
        ]).json()["attachments"][0]["id"]

        response = indep_client.post("/api/chat", json={
            "message": "Суммируй", "conversation_id": conversation_id,
            "attachment_ids": [public_id],
        })

        assert response.status_code == 200
        assert "error" not in response.json()
        # No TA/MWR knowledge base leaks into a non-affiliated workspace's
        # prompt - business_profile context (name/description) is still
        # allowed, only the gated "ПРОВЕРЕННАЯ БАЗА ЗНАНИЙ" block is not.
        assert "ПРОВЕРЕННАЯ БАЗА ЗНАНИЙ" not in captured["knowledge_context"]
        assert "MWR" not in captured["knowledge_context"]
        assert len(captured["attachments"]) == 1
        assert captured["attachments"][0].kind == "text"
        assert "20%" in captured["attachments"][0].text


# ── orphan reaper: abandoned pending uploads don't accumulate forever ────

def test_reap_orphan_attachments_deletes_old_pending_rows_and_files(api) -> None:
    from datetime import datetime, timedelta, timezone
    import aiosqlite

    client, web_api, workspace_id = api
    conversation_id = _new_conversation(client)
    public_id = _upload(client, conversation_id, [
        ("files", ("abandoned.jpg", _JPEG_BYTES, "image/jpeg")),
    ]).json()["attachments"][0]["id"]

    stored = _run(web_api.web_attachment_repository.get_for_workspace(
        workspace_id, OWNER_ID, public_id,
    ))
    path = web_api.attachment_storage._path(
        stored.workspace_id, stored.conversation_id, stored.stored_filename,
    )
    assert path.is_file()

    async def _backdate():
        async with aiosqlite.connect(web_api.web_attachment_repository.db_path) as db:
            await db.execute(
                "UPDATE web_attachments SET created_at = ? WHERE public_id = ?",
                ((datetime.now(timezone.utc) - timedelta(days=2)).isoformat(), public_id),
            )
            await db.commit()
    _run(_backdate())

    _run(web_api._reap_orphan_attachments())

    assert _run(web_api.web_attachment_repository.get_for_workspace(
        workspace_id, OWNER_ID, public_id,
    )) is None
    assert not path.exists()


def test_reap_orphan_attachments_keeps_recent_pending_and_attached_rows(api) -> None:
    client, web_api, workspace_id = api
    web_api.chat_provider.generate = _fake_generate()
    conversation_id = _new_conversation(client)

    recent_public_id = _upload(client, conversation_id, [
        ("files", ("recent.jpg", _JPEG_BYTES, "image/jpeg")),
    ]).json()["attachments"][0]["id"]

    attached_public_id = _upload(client, conversation_id, [
        ("files", ("sent.jpg", _JPEG_BYTES, "image/jpeg")),
    ]).json()["attachments"][0]["id"]
    client.post("/api/chat", json={
        "message": "Разбери", "conversation_id": conversation_id,
        "attachment_ids": [attached_public_id],
    })

    _run(web_api._reap_orphan_attachments())

    assert _run(web_api.web_attachment_repository.get_for_workspace(
        workspace_id, OWNER_ID, recent_public_id,
    )) is not None
    assert _run(web_api.web_attachment_repository.get_for_workspace(
        workspace_id, OWNER_ID, attached_public_id,
    )) is not None
