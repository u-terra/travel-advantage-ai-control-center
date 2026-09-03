"""Tests for app.chat_provider.OpenAIChatProvider - guards the web
Assistant's self-identification (ORCHESTRAVEL) against regressions. A
production end-to-end test once got "Я - Travel AI Orchestrator" back
even though this file's instructions string was already rebranded -
these tests pin the instructions actually sent to the model, including
that it's told to prefer its own identity over whatever an older
knowledge_context/workspace_memory blob (persisted before the rebrand)
might still say.

No test hits the network: transport is patched at the
urllib.request.urlopen level, same pattern as
tests/test_planner_openai_provider.py.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from app.chat_provider import AttachmentInput, ChatConfig, OpenAIChatProvider


class _Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return json.dumps(self.value, ensure_ascii=False).encode()


def _output(text: str) -> dict:
    return {
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": text}]},
        ],
    }


def _capture_payload(message="Представься", **generate_kwargs) -> dict:
    provider = OpenAIChatProvider(ChatConfig(api_key="test-key"))
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return _Response(_output("Ответ"))

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        provider.generate(message=message, **generate_kwargs)

    return captured["payload"]


def _capture_instructions(**generate_kwargs) -> str:
    return _capture_payload(**generate_kwargs)["instructions"]


def test_instructions_identify_the_assistant_as_orchestravel():
    instructions = _capture_instructions()

    assert "ORCHESTRAVEL" in instructions
    assert "Travel AI Orchestrator" not in instructions


def test_instructions_tell_the_model_its_identity_overrides_dynamic_context():
    """The exact production defect being guarded against: a workspace's
    own knowledge_context/workspace_memory (persisted before the rebrand)
    can still mention the old product name - the instructions must state
    that the ORCHESTRAVEL identity always wins over that, and the
    identity line must appear before any such dynamic context in the
    prompt."""
    instructions = _capture_instructions(
        knowledge_context="=== ПРОФИЛЬ БИЗНЕСА ===\nНазвание: Travel AI Orchestrator",
        workspace_memory="Мы работаем с Travel AI Orchestrator с прошлого года.",
    )

    identity_index = instructions.index("ORCHESTRAVEL")
    knowledge_index = instructions.index("ПРОФИЛЬ БИЗНЕСА")
    assert identity_index < knowledge_index
    assert "устаревш" in instructions.lower()


# ── attachments: current-turn multimodal input, no-attachment path unchanged ──

def test_no_attachments_sends_plain_string_content_unchanged():
    """Byte-identical to the payload shape sent before attachments
    existed - the whole point of _user_turn_content()'s early return."""
    payload = _capture_payload(message="Привет")

    user_item = payload["input"][-1]
    assert user_item == {"role": "user", "content": "Привет"}


def test_empty_attachments_list_also_sends_plain_string_content():
    payload = _capture_payload(message="Привет", attachments=[])

    assert payload["input"][-1]["content"] == "Привет"


def test_image_attachment_sent_as_input_image_data_uri():
    attachment = AttachmentInput(
        kind="image", content_type="image/png", filename="screen.png",
        data_base64="QUJD",
    )
    payload = _capture_payload(message="Разбери это предложение", attachments=[attachment])

    content = payload["input"][-1]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "input_text", "text": "Разбери это предложение"}
    image_part = next(p for p in content if p["type"] == "input_image")
    assert image_part["image_url"] == "data:image/png;base64,QUJD"


def test_pdf_attachment_sent_as_input_file_data_uri():
    attachment = AttachmentInput(
        kind="pdf", content_type="application/pdf", filename="hotel.pdf",
        data_base64="UERGREFUQQ==",
    )
    payload = _capture_payload(message="Сделай предложение клиенту", attachments=[attachment])

    content = payload["input"][-1]["content"]
    file_part = next(p for p in content if p["type"] == "input_file")
    assert file_part["filename"] == "hotel.pdf"
    assert file_part["file_data"] == "data:application/pdf;base64,UERGREFUQQ=="


def test_text_attachment_is_inlined_into_the_text_part_not_sent_as_media():
    attachment = AttachmentInput(
        kind="text", content_type="text/plain", filename="report.txt",
        text="Ключевой риск: цены выросли на 20%.",
    )
    payload = _capture_payload(message="Суммируй и выдели риски", attachments=[attachment])

    content = payload["input"][-1]["content"]
    # text-only attachment -> no media items -> stays a plain string, not a list
    assert isinstance(content, str)
    assert "Суммируй и выдели риски" in content
    assert "report.txt" in content
    assert "Ключевой риск: цены выросли на 20%." in content


def test_image_only_message_with_no_typed_text_still_has_a_text_part():
    attachment = AttachmentInput(
        kind="image", content_type="image/jpeg", filename="photo.jpg",
        data_base64="Zm9v",
    )
    payload = _capture_payload(message="", attachments=[attachment])

    content = payload["input"][-1]["content"]
    text_part = next(p for p in content if p["type"] == "input_text")
    assert text_part["text"]  # non-empty fallback prompt, never blank


def test_history_turns_are_never_expanded_with_attachments():
    """Prior turns stay plain strings even when the current turn has
    attachments - see generate()'s docstring on the v1 no-resend rule."""
    attachment = AttachmentInput(
        kind="image", content_type="image/png", filename="a.png", data_base64="QQ==",
    )
    payload = _capture_payload(
        message="А теперь сравни это с предыдущим",
        history=[
            {"role": "user", "content": "Вот первое сообщение"},
            {"role": "assistant", "content": "Хорошо, вижу"},
        ],
        attachments=[attachment],
    )

    history_items = payload["input"][:-1]
    assert all(isinstance(item["content"], str) for item in history_items)
