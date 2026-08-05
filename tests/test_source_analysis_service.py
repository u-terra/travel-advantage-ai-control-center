from __future__ import annotations

import json
import urllib.error
from unittest.mock import patch

from app.services.content_factory import ContentFactoryConfig, analyze_source_sync


VALID = {"ok": True, "analysis": {"summary": " Итог ", "key_facts": [" факт ", ""],
    "disputed_claims": [], "audience_value": " Польза ", "target_audiences": [],
    "content_angles": [], "recommended_formats": [], "warnings": []}}


class Response:
    def __init__(self, value=VALID): self.value = value
    def __enter__(self): return self
    def __exit__(self, *args): return None
    def read(self): return json.dumps(self.value, ensure_ascii=False).encode()


def config(url="http://factory/internal/generate", analysis_url=""):
    return ContentFactoryConfig(url, "secret-token", 7.5, analysis_url)


def test_analysis_contract_url_headers_payload_timeout_and_normalization():
    with patch("urllib.request.urlopen", return_value=Response()) as call:
        result = analyze_source_sync(config(analysis_url="  http://factory/internal/analyze-source  "), source_text="Новость")
    request = call.call_args.args[0]
    assert request.full_url == "http://factory/internal/analyze-source"
    assert request.get_header("X-internal-token") == "secret-token"
    assert json.loads(request.data) == {"source_text": "Новость", "source_type": "text"}
    assert call.call_args.kwargs["timeout"] == 7.5
    assert call.call_count == 1
    assert result.summary == "Итог" and result.key_facts == ("факт",)


def test_analysis_url_is_derived_from_generate_endpoint():
    with patch("urllib.request.urlopen", return_value=Response()) as call:
        assert analyze_source_sync(config(), source_text="x") is not None
    assert call.call_args.args[0].full_url == "http://factory/internal/analyze-source"


def test_derived_url_handles_trailing_slash_and_rejects_query_or_fragment():
    with patch("urllib.request.urlopen", return_value=Response()) as call:
        assert analyze_source_sync(config("http://factory/base/internal/generate/"), source_text="x") is not None
    assert call.call_args.args[0].full_url == "http://factory/base/internal/analyze-source"
    for url in (
        "http://factory/internal/generate?debug=1",
        "http://factory/internal/generate#fragment",
    ):
        with patch("urllib.request.urlopen") as call:
            assert analyze_source_sync(config(url), source_text="x") is None
        call.assert_not_called()


def test_unavailable_url_does_not_call_http():
    with patch("urllib.request.urlopen") as call:
        assert analyze_source_sync(config("http://factory/other"), source_text="x") is None
    call.assert_not_called()


def test_strict_invalid_responses_are_rejected():
    variants = [
        {"ok": False, "analysis": VALID["analysis"]}, [], 1, "json", None,
        {"ok": True, "analysis": None},
        {"ok": True, "analysis": {k: v for k, v in VALID["analysis"].items() if k != "warnings"}},
        {"ok": True, "analysis": {**VALID["analysis"], "warnings": "bad"}},
        {"ok": True, "analysis": {**VALID["analysis"], "key_facts": [1]}},
        {"ok": True, "analysis": {**VALID["analysis"], "summary": " "}},
        {"ok": 1, "analysis": VALID["analysis"]},
    ]
    for value in variants:
        with patch("urllib.request.urlopen", return_value=Response(value)):
            assert analyze_source_sync(config(), source_text="x") is None


def test_network_and_non_json_errors_are_hidden_and_single_attempt():
    with patch("urllib.request.urlopen", side_effect=OSError("secret-token")) as call:
        assert analyze_source_sync(config(), source_text="x") is None
    assert call.call_count == 1
    class Bad(Response):
        def read(self): return b"not-json"
    with patch("urllib.request.urlopen", return_value=Bad()):
        assert analyze_source_sync(config(), source_text="x") is None


def test_http_and_url_errors_are_safe_and_not_retried():
    errors = [
        urllib.error.HTTPError("http://factory", 503, "secret-token", {}, None),
        urllib.error.URLError("secret-token"),
        TimeoutError("secret-token"),
    ]
    for error in errors:
        with patch("urllib.request.urlopen", side_effect=error) as call:
            assert analyze_source_sync(config(), source_text="x") is None
        assert call.call_count == 1
