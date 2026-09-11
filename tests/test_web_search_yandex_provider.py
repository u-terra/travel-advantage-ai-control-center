"""Unit tests for YandexSearchProvider (Yandex Web Search API v2, Smart
Snippets mode).

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


def _doc(
    *,
    url: str | None = "https://example.com/a",
    title: str | None = "Title",
    description: str | None = "Description text.",
    info_context: str | None = "Smart snippet text.",
    num: int | None = 1,
) -> dict:
    doc: dict = {}
    if num is not None:
        doc["Num"] = num
    if title is not None:
        doc["DocumentTitle"] = title
    if url is not None:
        doc["FullUrl"] = url
    if description is not None:
        doc["Description"] = description
    if info_context is not None:
        doc["info_context"] = info_context
    return doc


def _smart_snippets_json(docs: list[dict]) -> str:
    return json.dumps({"docs": docs}, ensure_ascii=False)


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


def _raw_data_response(decoded_text: str) -> _FakeResponse:
    payload = {"rawData": base64.b64encode(decoded_text.encode("utf-8")).decode("ascii")}
    return _FakeResponse(json.dumps(payload).encode("utf-8"))


def _fake_open_returning(decoded_text: str):
    def fake_open(request: urllib.request.Request, *, timeout: float):
        return _raw_data_response(decoded_text)
    return fake_open


# ── Request shape (A, B) ─────────────────────────────────────────────────


def test_search_sends_smart_snippets_metadata_flag(monkeypatch):
    captured: dict = {}

    def fake_open(request: urllib.request.Request, *, timeout: float):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["headers"] = dict(request.headers)
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _raw_data_response(_smart_snippets_json([_doc(url="https://example.com/a")]))

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
    assert body["query"]["queryText"] == "Что нового у Travel Advantage?"
    # A: Smart Snippets flag must be present and set to "on".
    assert body["metadata"]["fields"]["x-genesis-info-context"] == "on"


def test_search_uses_ru_search_type(monkeypatch):
    captured: dict = {}

    def fake_open(request: urllib.request.Request, *, timeout: float):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _raw_data_response(_smart_snippets_json([]))

    monkeypatch.setattr(yp, "_open", fake_open)

    provider = YandexSearchProvider(_config())
    provider.search("query")

    # B: searchType must be SEARCH_TYPE_RU.
    assert captured["body"]["query"]["searchType"] == "SEARCH_TYPE_RU"


def test_search_with_site_appends_host_operator(monkeypatch):
    captured: dict = {}

    def fake_open(request: urllib.request.Request, *, timeout: float):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _raw_data_response(_smart_snippets_json([]))

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


# ── Parsing: Smart Snippets JSON docs[] (C, D, E, F, G) ───────────────────


def test_parses_three_results_with_correct_field_mapping():
    decoded = _smart_snippets_json([
        _doc(num=1, title="A", url="https://a.example/1", description="Desc A", info_context="Snippet A"),
        _doc(num=2, title="B", url="https://b.example/2", description="Desc B", info_context="Snippet B"),
        _doc(num=3, title="C", url="https://c.example/3", description="Desc C", info_context="Snippet C"),
    ])
    results = yp._parse_smart_snippet_results(decoded, provider="yandex", limit=5)

    assert len(results) == 3
    assert [r.title for r in results] == ["A", "B", "C"]
    assert [r.url for r in results] == [
        "https://a.example/1", "https://b.example/2", "https://c.example/3",
    ]
    assert [r.snippet for r in results] == ["Snippet A", "Snippet B", "Snippet C"]
    assert [r.domain for r in results] == ["a.example", "b.example", "c.example"]
    assert [r.rank for r in results] == [1, 2, 3]
    assert all(r.provider == "yandex" for r in results)
    assert all(r.published_at is None for r in results)


def test_snippet_comes_from_info_context():
    # D: snippet must come from info_context, not Description, when both present.
    decoded = _smart_snippets_json([
        _doc(url="https://a.example/1", description="Plain blurb", info_context="Smart snippet"),
    ])
    results = yp._parse_smart_snippet_results(decoded, provider="yandex", limit=5)
    assert results[0].snippet == "Smart snippet"


def test_empty_info_context_falls_back_to_description():
    # E: empty info_context -> fall back to Description.
    decoded = _smart_snippets_json([
        _doc(url="https://a.example/1", description="Plain blurb", info_context=""),
    ])
    results = yp._parse_smart_snippet_results(decoded, provider="yandex", limit=5)
    assert results[0].snippet == "Plain blurb"


def test_missing_full_url_is_skipped():
    # F: doc without FullUrl must be dropped, not crash / not become a result.
    decoded = _smart_snippets_json([
        _doc(url=None, title="No URL"),
        _doc(url="https://a.example/1", title="Has URL"),
    ])
    results = yp._parse_smart_snippet_results(decoded, provider="yandex", limit=5)
    assert len(results) == 1
    assert results[0].url == "https://a.example/1"


def test_dedupes_identical_full_url():
    # G: duplicate FullUrl -> dedupe, keep first occurrence.
    decoded = _smart_snippets_json([
        _doc(url="https://a.example/1", title="First"),
        _doc(url="https://a.example/1", title="Duplicate"),
        _doc(url="https://b.example/2", title="Second"),
    ])
    results = yp._parse_smart_snippet_results(decoded, provider="yandex", limit=5)
    assert [r.url for r in results] == ["https://a.example/1", "https://b.example/2"]
    assert results[0].title == "First"


def test_respects_limit_even_with_more_docs():
    decoded = _smart_snippets_json([
        _doc(url=f"https://example.com/{i}", num=i) for i in range(10)
    ])
    results = yp._parse_smart_snippet_results(decoded, provider="yandex", limit=2)
    assert len(results) == 2


def test_invalid_num_falls_back_to_sequential_rank():
    decoded = _smart_snippets_json([
        {"DocumentTitle": "No Num", "FullUrl": "https://a.example/1", "info_context": "x"},
        {"Num": "not-an-int", "DocumentTitle": "Bad Num", "FullUrl": "https://b.example/2", "info_context": "y"},
    ])
    results = yp._parse_smart_snippet_results(decoded, provider="yandex", limit=5)
    assert [r.rank for r in results] == [1, 2]


def test_max_results_config_caps_effective_limit(monkeypatch):
    monkeypatch.setattr(yp, "_open", _fake_open_returning(
        _smart_snippets_json([_doc(url=f"https://example.com/{i}", num=i) for i in range(10)])
    ))
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


def test_malformed_json_envelope_returns_none(monkeypatch):
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


def test_malformed_decoded_json_returns_none(monkeypatch):
    # H: rawData decodes fine as base64/UTF-8 but is not valid JSON.
    monkeypatch.setattr(yp, "_open", _fake_open_returning("not-json{"))
    assert YandexSearchProvider(_config()).search("query") is None


@pytest.mark.parametrize("decoded", [
    json.dumps({"docs": "not-a-list"}),
    json.dumps({"no_docs_key": []}),
    json.dumps(["not", "an", "object"]),
])
def test_unexpected_docs_shape_returns_none(monkeypatch, decoded):
    # I: docs missing / wrong type / payload not an object -> fail-soft.
    monkeypatch.setattr(yp, "_open", _fake_open_returning(decoded))
    assert YandexSearchProvider(_config()).search("query") is None


def test_docs_with_non_dict_entries_are_skipped_not_fatal():
    # A docs *list* is present and valid (top-level shape is fine) - entries
    # that aren't objects are skipped one-by-one rather than failing the
    # whole batch.
    decoded = _smart_snippets_json([
        {"FullUrl": "https://a.example/1", "DocumentTitle": "Real doc", "info_context": "x"},
    ])
    parsed = json.loads(decoded)
    parsed["docs"].insert(0, "not-a-doc-object")
    results = yp._parse_smart_snippet_results(json.dumps(parsed), provider="yandex", limit=5)
    assert len(results) == 1
    assert results[0].url == "https://a.example/1"


def test_docs_entirely_non_dict_returns_empty_results_not_none(monkeypatch):
    # I (edge case): every entry is the wrong type - each is skipped, giving
    # a valid empty-results response rather than a hard failure, since the
    # top-level {"docs": [...]} shape itself was fine.
    monkeypatch.setattr(yp, "_open", _fake_open_returning(json.dumps({"docs": [1, 2, 3]})))
    response = YandexSearchProvider(_config()).search("query")
    assert response is not None
    assert response.results == []


# ── Secrets never leak (J) ─────────────────────────────────────────────────


def test_api_key_never_appears_in_any_failure_path(monkeypatch, caplog):
    def fake_open(request: urllib.request.Request, *, timeout: float):
        raise urllib.error.HTTPError(
            "url", 403, f"forbidden key={FAKE_API_KEY}", hdrs=None, fp=None,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(yp, "_open", fake_open)
    with caplog.at_level("WARNING"):
        result = YandexSearchProvider(_config()).search("query")

    assert result is None
    assert FAKE_API_KEY not in caplog.text


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
