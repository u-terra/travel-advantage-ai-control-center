from __future__ import annotations

import json
import urllib.error
from unittest.mock import patch

from app.services.content_factory import ContentFactoryConfig, propose_topics_sync


VALID = {"ok": True, "topics": [
    {"id": " 1 ", "title": " Тема раз ", "angle": " История ", "reason": " Актуально "},
    {"id": "2", "title": "Тема два", "angle": "Еда", "reason": "Сезонно"},
    {"id": "3", "title": "Тема три", "angle": "Природа", "reason": "Визуально"},
]}


class Response:
    def __init__(self, value=VALID): self.value = value
    def __enter__(self): return self
    def __exit__(self, *args): return None
    def read(self): return json.dumps(self.value, ensure_ascii=False).encode()


def config(url="http://factory/internal/generate", topics_url=""):
    return ContentFactoryConfig(url, "secret-token", 7.5, "", topics_url)


# ── A: request contract ─────────────────────────────────────────────────


def test_topics_contract_url_headers_payload_timeout_and_normalization():
    with patch("urllib.request.urlopen", return_value=Response()) as call:
        result = propose_topics_sync(
            config(topics_url="  http://factory/internal/propose-topics  "),
            source_text="Предложи три темы для поста", count=3,
        )
    request = call.call_args.args[0]
    assert request.full_url == "http://factory/internal/propose-topics"
    assert request.get_header("X-internal-token") == "secret-token"
    body = json.loads(request.data)
    assert body == {"source_text": "Предложи три темы для поста", "count": 3}
    assert call.call_args.kwargs["timeout"] == 7.5
    assert call.call_count == 1  # F: exactly one HTTP call
    assert result is not None
    assert [topic.id for topic in result.topics] == ["1", "2", "3"]
    assert result.topics[0].title == "Тема раз"
    assert result.topics[0].angle == "История"
    assert result.topics[0].reason == "Актуально"


def test_topics_url_is_derived_from_generate_endpoint():
    with patch("urllib.request.urlopen", return_value=Response()) as call:
        assert propose_topics_sync(config(), source_text="x", count=3) is not None
    assert call.call_args.args[0].full_url == "http://factory/internal/propose-topics"


def test_derived_topics_url_handles_trailing_slash_and_rejects_query_or_fragment():
    with patch("urllib.request.urlopen", return_value=Response()) as call:
        assert propose_topics_sync(
            config("http://factory/base/internal/generate/"), source_text="x", count=3,
        ) is not None
    assert call.call_args.args[0].full_url == "http://factory/base/internal/propose-topics"
    for url in (
        "http://factory/internal/generate?debug=1",
        "http://factory/internal/generate#fragment",
    ):
        with patch("urllib.request.urlopen") as call:
            assert propose_topics_sync(config(url), source_text="x", count=3) is None
        call.assert_not_called()


def test_unavailable_url_does_not_call_http():
    with patch("urllib.request.urlopen") as call:
        assert propose_topics_sync(config("http://factory/other"), source_text="x", count=3) is None
    call.assert_not_called()


def test_non_positive_count_does_not_call_http():
    with patch("urllib.request.urlopen") as call:
        assert propose_topics_sync(config(), source_text="x", count=0) is None
        assert propose_topics_sync(config(), source_text="x", count=-1) is None
    call.assert_not_called()


# ── B/C/D/E: response validation ────────────────────────────────────────


def test_valid_response_becomes_content_topics_result():
    with patch("urllib.request.urlopen", return_value=Response()):
        result = propose_topics_sync(config(), source_text="x", count=3)
    assert result is not None
    assert len(result.topics) == 3


def test_strict_invalid_responses_are_rejected():
    variants = [
        {"ok": False, "topics": VALID["topics"]},
        [], 1, "json", None,
        {"ok": True, "topics": None},
        {"ok": True, "topics": "not-a-list"},
        {"ok": True, "topics": [{"id": "1", "title": "T", "angle": "A"}]},  # missing reason
        {"ok": True, "topics": [{"id": "1", "title": "T", "angle": "A", "reason": " "}]},  # blank
        {"ok": True, "topics": [{"id": 1, "title": "T", "angle": "A", "reason": "R"}]},  # id not str
        {"ok": True, "topics": ["not-a-dict", "not-a-dict", "not-a-dict"]},
        {"ok": 1, "topics": VALID["topics"]},
    ]
    for value in variants:
        with patch("urllib.request.urlopen", return_value=Response(value)):
            assert propose_topics_sync(config(), source_text="x", count=3) is None


def test_count_mismatch_is_rejected():
    with patch("urllib.request.urlopen", return_value=Response()):
        assert propose_topics_sync(config(), source_text="x", count=5) is None
    two_topics = {"ok": True, "topics": VALID["topics"][:2]}
    with patch("urllib.request.urlopen", return_value=Response(two_topics)):
        assert propose_topics_sync(config(), source_text="x", count=3) is None


def test_duplicate_topic_ids_are_rejected():
    duplicated = {"ok": True, "topics": [
        {"id": "1", "title": "Тема раз", "angle": "A", "reason": "R"},
        {"id": "1", "title": "Тема два (дубль id)", "angle": "B", "reason": "S"},
        {"id": "3", "title": "Тема три", "angle": "C", "reason": "T"},
    ]}
    with patch("urllib.request.urlopen", return_value=Response(duplicated)):
        assert propose_topics_sync(config(), source_text="x", count=3) is None


# ── network/transport errors ─────────────────────────────────────────────


def test_network_and_non_json_errors_are_hidden_and_single_attempt():
    with patch("urllib.request.urlopen", side_effect=OSError("secret-token")) as call:
        assert propose_topics_sync(config(), source_text="x", count=3) is None
    assert call.call_count == 1

    class Bad(Response):
        def read(self): return b"not-json"
    with patch("urllib.request.urlopen", return_value=Bad()):
        assert propose_topics_sync(config(), source_text="x", count=3) is None


def test_http_and_url_errors_are_safe_and_not_retried():
    errors = [
        urllib.error.HTTPError("http://factory", 503, "secret-token", {}, None),
        urllib.error.URLError("secret-token"),
        TimeoutError("secret-token"),
    ]
    for error in errors:
        with patch("urllib.request.urlopen", side_effect=error) as call:
            assert propose_topics_sync(config(), source_text="x", count=3) is None
        assert call.call_count == 1
