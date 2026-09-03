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

from app.chat_provider import ChatConfig, OpenAIChatProvider


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


def _capture_instructions(**generate_kwargs) -> str:
    provider = OpenAIChatProvider(ChatConfig(api_key="test-key"))
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return _Response(_output("Ответ"))

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        provider.generate(message="Представься", **generate_kwargs)

    return captured["payload"]["instructions"]


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
