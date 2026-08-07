from __future__ import annotations

import json
import urllib.error
from unittest.mock import patch

from app.services.content_factory import ContentFactoryConfig, generate_draft_sync


class Response:
    def __init__(self, value):
        self.value = value
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return None
    def read(self):
        return json.dumps(self.value).encode()


def config():
    return ContentFactoryConfig(
        "http://factory/internal/generate", "secret-token", 7.5
    )


def test_generate_contract_endpoint_payload_headers_timeout_and_single_call():
    with patch("urllib.request.urlopen", return_value=Response({
        "ok": True, "text": " Черновик ", "warnings": [" Проверить "]
    })) as call:
        result = generate_draft_sync(
            config(), source_text="Контекст", material_type="market_offer",
            output_format="vk", mode="ai",
        )
    request = call.call_args.args[0]
    assert request.full_url == "http://factory/internal/generate"
    assert request.get_header("X-internal-token") == "secret-token"
    assert json.loads(request.data) == {
        "mode": "ai", "source_text": "Контекст",
        "material_type": "market_offer", "output_format": "vk",
    }
    assert call.call_args.kwargs["timeout"] == 7.5
    assert call.call_count == 1
    assert result.text == "Черновик" and result.warnings == ("Проверить",)


def test_generate_http_and_invalid_responses_fail_safely_without_retry():
    variants = [
        urllib.error.HTTPError("http://factory", 503, "secret-token", {}, None),
        urllib.error.URLError("secret-token"), TimeoutError("secret-token"),
    ]
    for error in variants:
        with patch("urllib.request.urlopen", side_effect=error) as call:
            assert generate_draft_sync(
                config(), source_text="x", material_type="market_offer",
                output_format="telegram", mode="ai",
            ) is None
        assert call.call_count == 1
    for value in (
        {"ok": False, "text": "x"}, {"ok": True, "text": " "}, [], "bad",
    ):
        with patch("urllib.request.urlopen", return_value=Response(value)):
            assert generate_draft_sync(
                config(), source_text="x", material_type="market_offer",
                output_format="telegram", mode="ai",
            ) is None
