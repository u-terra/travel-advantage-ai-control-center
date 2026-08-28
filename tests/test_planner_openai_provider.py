"""Tests for the first live PlannerLLMProvider (OpenAI, direct HTTPS call).

No test hits the network: transport is patched at the urllib.request.urlopen
level, same pattern as tests/test_orchestration_openai_provider.py.
"""

from __future__ import annotations

import json
import re
from unittest.mock import patch

import pytest

from app.planner.openai_provider import (
    OpenAIPlannerProvider,
    PlannerOpenAIConfig,
    _build_messages,
    _response_schema,
    plan_sync,
)
from app.planner.plan import MAX_STEPS, validate_task_plan
from app.planner.provider import PlannerLLMProvider
from app.planner.request import build_planner_request

CONFIG = PlannerOpenAIConfig(api_key="secret-key", model="gpt-4o-mini", timeout_seconds=5.0)

REQUEST = build_planner_request("Проанализируй конкурента ТурКлуб")

_PLAN = {
    "goal": "Проанализировать конкурента и предложить действия",
    "reason": "Пользователь явно попросил анализ конкурента",
    "steps": [
        {
            "id": "step_1", "action": "Собрать список конкурентов",
            "executor": "list_competitors", "input": {}, "depends_on": [],
        },
    ],
    "final_output": "Итоговые рекомендации",
}


class Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return json.dumps(self.value, ensure_ascii=False).encode()


def _chat_completion(content: str, *, usage: dict | None = None) -> dict:
    payload = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        payload["usage"] = usage
    return payload


# --- is_configured -----------------------------------------------------------


def test_is_configured_requires_api_key_and_model():
    assert PlannerOpenAIConfig("key", "model", 5.0).is_configured is True
    assert PlannerOpenAIConfig("", "model", 5.0).is_configured is False
    assert PlannerOpenAIConfig("key", "", 5.0).is_configured is False


def test_unconfigured_provider_never_calls_network():
    provider = OpenAIPlannerProvider(PlannerOpenAIConfig("", "", 0.0))
    assert provider.is_configured is False
    with patch("urllib.request.urlopen") as mock_urlopen:
        assert provider.plan(request=REQUEST) is None
    mock_urlopen.assert_not_called()


# --- contract ------------------------------------------------------------------


def test_provider_implements_the_shared_interface():
    provider = OpenAIPlannerProvider(CONFIG)
    assert isinstance(provider, PlannerLLMProvider)
    assert provider.name == "openai"


# --- successful call -----------------------------------------------------------


def test_successful_call_returns_decoded_plan_and_it_validates():
    raw = _chat_completion(json.dumps(_PLAN, ensure_ascii=False))
    with patch("urllib.request.urlopen", return_value=Response(raw)):
        result = OpenAIPlannerProvider(CONFIG).plan(request=REQUEST)
    assert result == _PLAN
    validate_task_plan(result)  # must be a genuinely valid TaskPlan


def test_request_makes_exactly_one_call_sends_model_and_json_schema():
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        return Response(_chat_completion(json.dumps(_PLAN)))

    with patch("urllib.request.urlopen", side_effect=fake_urlopen) as mock_urlopen:
        OpenAIPlannerProvider(CONFIG).plan(request=REQUEST)

    assert len(calls) == 1  # cost control: at most one LLM call per plan()
    mock_urlopen.assert_called_once()


def test_request_sends_expected_payload_shape():
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return Response(_chat_completion(json.dumps(_PLAN)))

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        OpenAIPlannerProvider(CONFIG).plan(request=REQUEST)

    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer secret-key"
    assert captured["body"]["model"] == "gpt-4o-mini"
    assert captured["body"]["response_format"]["type"] == "json_schema"
    assert captured["timeout"] == 5.0


def test_request_sets_temperature_to_zero():
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return Response(_chat_completion(json.dumps(_PLAN)))

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        OpenAIPlannerProvider(CONFIG).plan(request=REQUEST)

    assert captured["body"]["temperature"] == 0


def test_request_caps_output_tokens_cost_control():
    """A TaskPlan is a short structured document, not free-form reasoning -
    max_tokens must be capped, not left unbounded."""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return Response(_chat_completion(json.dumps(_PLAN)))

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        OpenAIPlannerProvider(CONFIG).plan(request=REQUEST)

    assert isinstance(captured["body"]["max_tokens"], int)
    assert 0 < captured["body"]["max_tokens"] <= 2000


def test_usage_is_logged_but_not_prompt_or_response_content(caplog):
    raw = _chat_completion(
        json.dumps(_PLAN), usage={"prompt_tokens": 123, "completion_tokens": 45},
    )
    with caplog.at_level("INFO"):
        with patch("urllib.request.urlopen", return_value=Response(raw)):
            OpenAIPlannerProvider(CONFIG).plan(request=REQUEST)
    assert "123" in caplog.text
    assert "45" in caplog.text
    assert "ТурКлуб" not in caplog.text
    assert "secret-key" not in caplog.text


def test_response_schema_caps_steps_at_max_steps():
    schema = _response_schema(REQUEST)["schema"]
    assert schema["properties"]["steps"]["maxItems"] == MAX_STEPS
    assert schema["properties"]["steps"]["minItems"] == 1


def test_response_schema_restricts_executor_enum_to_catalog():
    schema = _response_schema(REQUEST)["schema"]
    step_schema = schema["properties"]["steps"]["items"]
    assert set(step_schema["properties"]["executor"]["enum"]) == set(REQUEST.executor_catalog)


def test_response_schema_step_input_is_a_closed_nullable_field_set():
    """Strict schema, not a free-form object - the model cannot invent an
    input field no executor reads (see app.planner.executors)."""
    schema = _response_schema(REQUEST)["schema"]
    step_schema = schema["properties"]["steps"]["items"]
    input_schema = step_schema["properties"]["input"]
    assert input_schema["additionalProperties"] is False
    assert set(input_schema["properties"]) == {
        "url", "competitor_label", "text", "task_text", "limit",
    }


def test_response_schema_structurally_forbids_competitor_id():
    """Stage 3.2 hotfix: a live run showed the model inventing
    competitor_id=1 despite the system prompt explicitly forbidding it -
    prompt text alone was not a reliable guard. The OpenAI request schema
    must make this structurally impossible: competitor_id must not appear in
    the input schema's properties, in its required list, or anywhere in the
    serialized schema at all (additionalProperties: false already rejects
    any property the model tries to emit outside "properties", so a
    model-generated plan can never contain this field, regardless of what
    the model 'wants' to output)."""
    schema = _response_schema(REQUEST)["schema"]
    step_schema = schema["properties"]["steps"]["items"]
    input_schema = step_schema["properties"]["input"]
    assert "competitor_id" not in input_schema["properties"]
    assert "competitor_id" not in input_schema["required"]
    assert "competitor_id" not in json.dumps(schema)


def test_natural_language_competitor_request_schema_has_no_competitor_id_path():
    """End-to-end structural proof for the natural-language scenario
    ('Проанализируй конкурента ТурКлуб...'): whatever plan the model returns
    for THIS exact request, competitor_id cannot be part of it - the schema
    built for this specific request has no such field anywhere."""
    request = build_planner_request("Проанализируй конкурента ТурКлуб и скажи, что мне делать лучше него")
    schema = _response_schema(request)["schema"]
    assert "competitor_id" not in json.dumps(schema)
    assert "competitor_label" in json.dumps(schema)


def test_step_id_schema_pattern_matches_plan_validation_pattern():
    schema = _response_schema(REQUEST)["schema"]
    step_schema = schema["properties"]["steps"]["items"]
    pattern = re.compile(step_schema["properties"]["id"]["pattern"])
    assert pattern.match("step_1")
    assert not pattern.match("step 1")


# --- fail-closed: always None, never an exception -------------------------------


def test_transport_failure_returns_none_not_raises():
    with patch("urllib.request.urlopen", side_effect=TimeoutError("secret-key")):
        assert OpenAIPlannerProvider(CONFIG).plan(request=REQUEST) is None


def test_non_json_body_returns_none():
    class BadResponse(Response):
        def read(self):
            return b"not json"

    with patch("urllib.request.urlopen", return_value=BadResponse(None)):
        assert OpenAIPlannerProvider(CONFIG).plan(request=REQUEST) is None


def test_missing_choices_returns_none():
    with patch("urllib.request.urlopen", return_value=Response({"choices": []})):
        assert OpenAIPlannerProvider(CONFIG).plan(request=REQUEST) is None


def test_non_json_model_content_returns_none():
    raw = _chat_completion("this is not json")
    with patch("urllib.request.urlopen", return_value=Response(raw)):
        assert OpenAIPlannerProvider(CONFIG).plan(request=REQUEST) is None


def test_unexpected_exception_is_swallowed():
    with patch("urllib.request.urlopen", side_effect=RuntimeError("boom")):
        assert OpenAIPlannerProvider(CONFIG).plan(request=REQUEST) is None


def test_error_never_leaks_the_api_key(caplog):
    with caplog.at_level("WARNING"):
        with patch("urllib.request.urlopen", side_effect=TimeoutError("secret-key")):
            result = plan_sync(CONFIG, request=REQUEST)
    assert result is None
    assert "secret-key" not in caplog.text


# --- message building (no network) ----------------------------------------------


def test_build_messages_include_rules_and_catalog_and_task():
    messages = _build_messages(REQUEST)
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    system_content = messages[0]["content"]
    user_content = messages[1]["content"]
    assert "list_competitors" in system_content
    assert "fetch_public_source" in system_content
    assert "ТурКлуб" in user_content


def test_build_messages_handles_empty_context_without_crashing():
    request = build_planner_request("Проанализируй конкурента X")
    messages = _build_messages(request)
    assert len(messages) == 2
