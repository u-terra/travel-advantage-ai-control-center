"""Unit tests for YandexSearchProvider (Yandex Web Search API v2).

No real network call anywhere - urllib.request.urlopen is never invoked;
every test monkeypatches the isolated seam
app.services.web_search.yandex_provider._open, same convention as
app.planner.fetch's own tests.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

import pytest

from app.services.web_search import yandex_provider as yp
from app.services.web_search.yandex_provider import YandexSearchConfig, YandexSearchProvider

FAKE_API_KEY = "test-only-fake-key-not-a-secret"
FAKE_FOLDER_ID = "b1gfake000folder"


def _config(**overrides) -> YandexSearchConfig:
    base = dict(api_key=FAKE_API_KEY, folder_id=FAKE_FOLDER_ID, timeout_seconds=5.0, max_results=5)
    base.update(overrides)
    return YandexSearchConfig(**base)


def _xml_with_docs(docs_xml: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<yandexsearch version="1.0"><response><results><grouping>'
        + docs_xml +
        "</grouping></results></response></yandexsearch>"
    )


def _doc_xml(*, url: str | None, title: str = "Title", passage: str | None = "Snippet text.") -> str:
    url_tag = f"<url>{url}</url>" if url is not None else ""
    passages = f"<passages><passage>{passage}</passage></passages>" if passage else ""
    return f"<group><doc>{url_tag}<title>{title}</title>{passages}</doc></group>"


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


def _raw_data_response(xml_text: str) -> _FakeResponse:
    payload = {"rawData": base64.b64encode(xml_text.encode("utf-8")).decode("ascii")}
    return _FakeResponse(json.dumps(payload).encode("utf-8"))


# ── Request shape ─────────────────────────────────────────────────────────


def test_search_sends_correct_request(monkeypatch):
    captured: dict = {}

    def fake_open(request: urllib.request.Request, *, timeout: float):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["headers"] = dict(request.headers)
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _raw_data_response(_xml_with_docs(_doc_xml(url="https://example.com/a")))

    monkeypatch.setattr(yp, "_open", fake_open)

    provider = YandexSearchProvider(_config(timeout_seconds=7.5, max_results=3))
    response = provider.search("Что нового у Travel Advantage?", limit=3)

    assert response is not None
    assert captured["method"] == "POST"
    assert captured["url"] == "https://searchapi.api.cloud.yandex.net/v2/web/search"
    assert captured["headers"]["Authorization"] == f"Api-Key {FAKE_API_KEY}"
    assert captured["timeout"] == 7.5
    body = captured["body"]
    assert body["folderId"] == FAKE_FOLDER_ID
    assert body["responseFormat"] == "FORMAT_XML"
    assert body["query"]["queryText"] == "Что нового у Travel Advantage?"
    assert body["query"]["searchType"]
    assert body["groupSpec"]["groupsOnPage"] == 3


def test_search_with_site_appends_host_operator(monkeypatch):
    captured: dict = {}

    def fake_open(request: urllib.request.Request, *, timeout: float):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _raw_data_response(_xml_with_docs(""))

    monkeypatch.setattr(yp, "_open", fake_open)

    provider = YandexSearchProvider(_config())
    provider.search("отзывы", site="https://www.example.com/path")

    assert "host:example.com" in captured["body"]["query"]["queryText"]


# ── Authorization must never leak into an exception ──────────────────────


def test_authorization_header_never_appears_in_raised_error(monkeypatch):
    def fake_open(request: urllib.request.Request, *, timeout: float):
        # Simulates a server that echoes request headers into its error body
        # - the provider must never read/propagate that regardless.
        raise urllib.error.HTTPError(
            "https://searchapi.api.cloud.yandex.net/v2/web/search",
            401,
            f"unauthorized, saw Authorization: Api-Key {FAKE_API_KEY}",
            hdrs=None,  # type: ignore[arg-type]
            fp=None,
        )

    monkeypatch.setattr(yp, "_open", fake_open)

    provider = YandexSearchProvider(_config())
    with pytest.raises(yp._YandexSearchCallError) as excinfo:
        provider._call({"query": {"searchType": "SEARCH_TYPE_RU", "queryText": "x"}})

    assert FAKE_API_KEY not in str(excinfo.value)
    assert FAKE_API_KEY not in excinfo.value.safe_reason
    assert excinfo.value.safe_reason == "http_401"


def test_search_returns_none_on_401_without_raising(monkeypatch):
    def fake_open(request: urllib.request.Request, *, timeout: float):
        raise urllib.error.HTTPError(
            "https://searchapi.api.cloud.yandex.net/v2/web/search", 401, "unauthorized",
            hdrs=None, fp=None,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(yp, "_open", fake_open)
    provider = YandexSearchProvider(_config())
    assert provider.search("test query") is None


# ── Parsing: 3 results, dedupe, missing URL dropped ──────────────────────


def test_parses_three_results():
    xml = _xml_with_docs(
        _doc_xml(url="https://a.example/1", title="A")
        + _doc_xml(url="https://b.example/2", title="B")
        + _doc_xml(url="https://c.example/3", title="C")
    )
    results = yp._parse_xml_results(xml, provider="yandex", limit=5)
    assert len(results) == 3
    assert [r.url for r in results] == [
        "https://a.example/1", "https://b.example/2", "https://c.example/3",
    ]
    assert [r.rank for r in results] == [1, 2, 3]
    assert all(r.provider == "yandex" for r in results)


def test_dedupes_identical_urls():
    xml = _xml_with_docs(
        _doc_xml(url="https://a.example/1", title="First")
        + _doc_xml(url="https://a.example/1", title="Duplicate")
        + _doc_xml(url="https://b.example/2", title="Second")
    )
    results = yp._parse_xml_results(xml, provider="yandex", limit=5)
    assert [r.url for r in results] == ["https://a.example/1", "https://b.example/2"]


def test_docs_without_url_are_skipped():
    xml = _xml_with_docs(
        _doc_xml(url=None, title="No URL")
        + _doc_xml(url="https://a.example/1", title="Has URL")
    )
    results = yp._parse_xml_results(xml, provider="yandex", limit=5)
    assert len(results) == 1
    assert results[0].url == "https://a.example/1"


def test_respects_limit_even_with_more_docs_in_xml():
    xml = _xml_with_docs("".join(
        _doc_xml(url=f"https://example.com/{i}", title=f"Doc {i}") for i in range(10)
    ))
    results = yp._parse_xml_results(xml, provider="yandex", limit=2)
    assert len(results) == 2


def test_snippet_prefers_passages_falls_back_to_headline():
    xml = _xml_with_docs(_doc_xml(url="https://a.example/1", passage="From passage"))
    results = yp._parse_xml_results(xml, provider="yandex", limit=5)
    assert results[0].snippet == "From passage"


def test_max_results_config_caps_effective_limit(monkeypatch):
    def fake_open(request: urllib.request.Request, *, timeout: float):
        return _raw_data_response(_xml_with_docs("".join(
            _doc_xml(url=f"https://example.com/{i}") for i in range(10)
        )))

    monkeypatch.setattr(yp, "_open", fake_open)
    provider = YandexSearchProvider(_config(max_results=2))
    response = provider.search("query", limit=10)
    assert response is not None
    assert len(response.results) == 2


# ── Fail-soft: timeout, 429, 5xx, malformed response, network error ──────


def test_timeout_returns_none(monkeypatch):
    def fake_open(request: urllib.request.Request, *, timeout: float):
        raise TimeoutError("timed out")

    monkeypatch.setattr(yp, "_open", fake_open)
    assert YandexSearchProvider(_config()).search("query") is None


def test_429_returns_none(monkeypatch):
    def fake_open(request: urllib.request.Request, *, timeout: float):
        raise urllib.error.HTTPError(
            "url", 429, "Too Many Requests", hdrs=None, fp=None,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(yp, "_open", fake_open)
    assert YandexSearchProvider(_config()).search("query") is None


def test_5xx_returns_none(monkeypatch):
    def fake_open(request: urllib.request.Request, *, timeout: float):
        raise urllib.error.HTTPError(
            "url", 500, "Internal Server Error", hdrs=None, fp=None,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(yp, "_open", fake_open)
    assert YandexSearchProvider(_config()).search("query") is None


def test_network_error_returns_none(monkeypatch):
    def fake_open(request: urllib.request.Request, *, timeout: float):
        raise urllib.error.URLError("network unreachable")

    monkeypatch.setattr(yp, "_open", fake_open)
    assert YandexSearchProvider(_config()).search("query") is None


def test_malformed_json_returns_none(monkeypatch):
    def fake_open(request: urllib.request.Request, *, timeout: float):
        return _FakeResponse(b"not-json{")

    monkeypatch.setattr(yp, "_open", fake_open)
    assert YandexSearchProvider(_config()).search("query") is None


def test_missing_raw_data_field_returns_none(monkeypatch):
    def fake_open(request: urllib.request.Request, *, timeout: float):
        return _FakeResponse(json.dumps({"unexpected": "shape"}).encode("utf-8"))

    monkeypatch.setattr(yp, "_open", fake_open)
    assert YandexSearchProvider(_config()).search("query") is None


def test_malformed_base64_returns_none(monkeypatch):
    def fake_open(request: urllib.request.Request, *, timeout: float):
        return _FakeResponse(json.dumps({"rawData": "not-valid-base64!!!"}).encode("utf-8"))

    monkeypatch.setattr(yp, "_open", fake_open)
    assert YandexSearchProvider(_config()).search("query") is None


def test_malformed_xml_returns_none(monkeypatch):
    def fake_open(request: urllib.request.Request, *, timeout: float):
        broken_xml = "<yandexsearch><response>not closed"
        return _raw_data_response(broken_xml)

    monkeypatch.setattr(yp, "_open", fake_open)
    assert YandexSearchProvider(_config()).search("query") is None


# ── Disabled / unconfigured ────────────────────────────────────────────────


def test_missing_credentials_returns_none_without_any_network_call(monkeypatch):
    def fake_open(request, *, timeout):
        raise AssertionError("must not attempt a network call when unconfigured")

    monkeypatch.setattr(yp, "_open", fake_open)
    provider = YandexSearchProvider(_config(api_key="", folder_id=""))
    assert provider.search("query") is None


def test_empty_query_returns_none_without_any_network_call(monkeypatch):
    def fake_open(request, *, timeout):
        raise AssertionError("must not attempt a network call for an empty query")

    monkeypatch.setattr(yp, "_open", fake_open)
    assert YandexSearchProvider(_config()).search("   ") is None
