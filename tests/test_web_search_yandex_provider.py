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
    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self._body = body
        self.status = status  # exercised by the parse-failure diagnostics below

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


def _raw_data_response(decoded_text: str, *, status: int = 200) -> _FakeResponse:
    payload = {"rawData": base64.b64encode(decoded_text.encode("utf-8")).decode("ascii")}
    return _FakeResponse(json.dumps(payload).encode("utf-8"), status=status)


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


# ── XML fallback parser (classic Yandex search XML shape) ─────────────────
#
# Production diagnosis: a live SEARCH_TYPE_COM request returned HTTP 200,
# base64-decoded fine, but the decoded body was the classic Yandex XML
# search shape ("<?xml version=\"1.0\" ...><yandexsearch ...>") instead of
# Smart Snippets JSON - and the provider tried to json.loads() it, hit
# "bad smart snippets json", and returned None. These tests cover the fix:
# _parse_search_results() tries JSON first (unchanged - see the JSON tests
# above, all still green) and only falls back to _parse_xml_search_results()
# when the body is not JSON at all.


def _xml_doc(
    *,
    url: str | None = "https://example.com/a",
    domain: str | None = "example.com",
    title: str | None = "Title",
    passages: list[str] | None = ("Snippet passage.",),
) -> str:
    parts = []
    if url is not None:
        parts.append(f"<url>{url}</url>")
    if domain is not None:
        parts.append(f"<domain>{domain}</domain>")
    if title is not None:
        parts.append(f"<title>{title}</title>")
    if passages is not None:
        inner = "".join(f"<passage>{p}</passage>" for p in passages)
        parts.append(f"<passages>{inner}</passages>")
    return f"<doc>{''.join(parts)}</doc>"


def _xml_response(docs: list[str]) -> str:
    # Realistic nesting (yandexsearch/response/results/grouping/group/doc) -
    # matches the classic Yandex search XML shape confirmed in production.
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<yandexsearch version="1.0">\n'
        "<request><query>test query</query></request>\n"
        '<response date="20260101T000000">\n'
        f'<found priority="phrase">{len(docs)}</found>\n'
        "<results><grouping>"
        f'<found priority="phrase">{len(docs)}</found>'
        f"<group>{''.join(docs)}</group>"
        "</grouping></results>\n"
        "</response>\n"
        "</yandexsearch>"
    )


def test_parses_single_xml_doc(monkeypatch):
    monkeypatch.setattr(yp, "_open", _fake_open_returning(_xml_response([_xml_doc()])))
    response = YandexSearchProvider(_config()).search("query", search_type="SEARCH_TYPE_COM")
    assert response is not None
    assert len(response.results) == 1
    result = response.results[0]
    assert result.url == "https://example.com/a"
    assert result.domain == "example.com"
    assert result.title == "Title"
    assert result.snippet == "Snippet passage."
    assert result.rank == 1
    assert result.provider == "yandex"
    assert result.published_at is None


def test_parses_multiple_xml_docs_preserving_order(monkeypatch):
    docs = [
        _xml_doc(url=f"https://example.com/{i}", title=f"Title {i}", passages=[f"Snippet {i}"])
        for i in range(3)
    ]
    monkeypatch.setattr(yp, "_open", _fake_open_returning(_xml_response(docs)))
    response = YandexSearchProvider(_config()).search("query")
    assert response is not None
    assert [r.url for r in response.results] == [
        "https://example.com/0", "https://example.com/1", "https://example.com/2",
    ]
    assert [r.rank for r in response.results] == [1, 2, 3]


def test_xml_doc_missing_snippet_defaults_to_empty(monkeypatch):
    monkeypatch.setattr(
        yp, "_open", _fake_open_returning(_xml_response([_xml_doc(passages=None)])),
    )
    response = YandexSearchProvider(_config()).search("query")
    assert response is not None
    assert response.results[0].snippet == ""


def test_xml_doc_missing_title_falls_back_to_url(monkeypatch):
    monkeypatch.setattr(
        yp, "_open", _fake_open_returning(_xml_response([_xml_doc(title=None)])),
    )
    response = YandexSearchProvider(_config()).search("query")
    assert response is not None
    assert response.results[0].title == "https://example.com/a"


def test_xml_doc_without_url_is_skipped(monkeypatch):
    docs = [_xml_doc(url=None), _xml_doc(url="https://example.com/kept")]
    monkeypatch.setattr(yp, "_open", _fake_open_returning(_xml_response(docs)))
    response = YandexSearchProvider(_config()).search("query")
    assert response is not None
    assert len(response.results) == 1
    assert response.results[0].url == "https://example.com/kept"


def test_xml_domain_derived_from_url_when_domain_tag_missing(monkeypatch):
    monkeypatch.setattr(
        yp, "_open", _fake_open_returning(
            _xml_response([_xml_doc(url="https://sub.example.org/page", domain=None)]),
        ),
    )
    response = YandexSearchProvider(_config()).search("query")
    assert response is not None
    assert response.results[0].domain == "sub.example.org"


def test_xml_respects_limit_config(monkeypatch):
    docs = [_xml_doc(url=f"https://example.com/{i}") for i in range(5)]
    monkeypatch.setattr(yp, "_open", _fake_open_returning(_xml_response(docs)))
    provider = YandexSearchProvider(_config(max_results=2))
    response = provider.search("query", limit=10)
    assert response is not None
    assert len(response.results) == 2


def test_xml_dedupes_identical_url(monkeypatch):
    docs = [
        _xml_doc(url="https://example.com/a", title="First"),
        _xml_doc(url="https://example.com/a", title="Duplicate"),
        _xml_doc(url="https://example.com/b", title="Second"),
    ]
    monkeypatch.setattr(yp, "_open", _fake_open_returning(_xml_response(docs)))
    response = YandexSearchProvider(_config()).search("query")
    assert response is not None
    assert [r.url for r in response.results] == ["https://example.com/a", "https://example.com/b"]
    assert response.results[0].title == "First"


def test_xml_nested_highlight_markup_does_not_crash_and_is_joined(monkeypatch):
    # Yandex highlights matched query terms with a nested <hlword> tag inside
    # <title>/<passage> - a naive .text read would stop before it.
    doc = (
        "<doc><url>https://example.com/a</url>"
        "<title>Правила <hlword>въезда</hlword> в Индонезию</title>"
        "<passages><passage>Актуальные <hlword>правила</hlword> на 2026 год.</passage></passages>"
        "</doc>"
    )
    monkeypatch.setattr(yp, "_open", _fake_open_returning(_xml_response([doc])))
    response = YandexSearchProvider(_config()).search("query")
    assert response is not None
    assert response.results[0].title == "Правила въезда в Индонезию"
    assert response.results[0].snippet == "Актуальные правила на 2026 год."


def test_xml_entities_are_decoded(monkeypatch):
    doc = (
        "<doc><url>https://example.com/a?x=1&amp;y=2</url>"
        "<title>Tom &amp; Jerry &lt;official&gt;</title></doc>"
    )
    monkeypatch.setattr(yp, "_open", _fake_open_returning(_xml_response([doc])))
    response = YandexSearchProvider(_config()).search("query")
    assert response is not None
    assert response.results[0].url == "https://example.com/a?x=1&y=2"
    assert response.results[0].title == "Tom & Jerry <official>"


def test_malformed_xml_returns_none(monkeypatch):
    monkeypatch.setattr(yp, "_open", _fake_open_returning("<doc><url>unclosed"))
    assert YandexSearchProvider(_config()).search("query") is None


def test_xml_without_any_doc_elements_returns_none(monkeypatch):
    monkeypatch.setattr(yp, "_open", _fake_open_returning(_xml_response([])))
    assert YandexSearchProvider(_config()).search("query") is None


def test_parse_xml_search_results_raises_shape_error_without_doc_elements():
    with pytest.raises(yp._XmlSearchShapeError):
        yp._parse_xml_search_results("<yandexsearch></yandexsearch>", provider="yandex", limit=5)


def test_parse_search_results_dispatches_to_xml_only_when_json_fails_and_looks_like_xml():
    xml_text = _xml_response([_xml_doc()])
    results = yp._parse_search_results(xml_text, provider="yandex", limit=5)
    assert len(results) == 1
    assert results[0].url == "https://example.com/a"


def test_parse_search_results_prefers_json_when_both_would_be_ambiguous():
    # Sanity check that the JSON path is tried FIRST and unconditionally -
    # valid Smart Snippets JSON is never redirected into the XML parser.
    decoded = _smart_snippets_json([_doc(url="https://example.com/json-wins")])
    results = yp._parse_search_results(decoded, provider="yandex", limit=5)
    assert len(results) == 1
    assert results[0].url == "https://example.com/json-wins"


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


# ══════════════════════════════════════════════════════════════════════════
# TEMPORARY PRODUCTION DIAGNOSTIC LOGGING - Smart Snippets parse failure.
#
# Delete alongside app/services/web_search/yandex_provider.py's matching
# banner (_log_parse_failure_diagnostics and the two _DIAG_PARSE_* constants)
# once the live investigation into the SEARCH_TYPE_COM parse failure is done.
# ══════════════════════════════════════════════════════════════════════════

def test_parse_failure_logs_http_status_and_search_type(monkeypatch, caplog):
    monkeypatch.setattr(yp, "_open", _fake_open_returning('{"no_docs_key": []}'))
    with caplog.at_level("WARNING"):
        result = YandexSearchProvider(_config()).search("query", search_type="SEARCH_TYPE_COM")
    assert result is None
    assert "web_search_parse_diag: search_type='SEARCH_TYPE_COM' http_status=200" in caplog.text


def test_parse_failure_logs_top_level_keys_and_docs_shape_for_wrong_shape_dict(monkeypatch, caplog):
    monkeypatch.setattr(
        yp, "_open", _fake_open_returning(json.dumps({"error": "quota_exceeded", "code": 7})),
    )
    with caplog.at_level("WARNING"):
        YandexSearchProvider(_config()).search("query")
    text = caplog.text
    assert "top_level_type=dict" in text
    assert "'code'" in text and "'error'" in text
    assert "docs_key_present=False" in text


def test_parse_failure_logs_first_doc_keys_when_docs_is_wrong_type(monkeypatch, caplog):
    monkeypatch.setattr(yp, "_open", _fake_open_returning(json.dumps({"docs": "not-a-list"})))
    with caplog.at_level("WARNING"):
        YandexSearchProvider(_config()).search("query")
    assert "docs_key_present=True docs_type=str" in caplog.text


def test_parse_failure_logs_not_valid_json_and_a_capped_snippet(monkeypatch, caplog):
    # Hypothesis under investigation: SEARCH_TYPE_COM might not honor the
    # Smart Snippets flag and fall back to the plain (XML) Web Search shape.
    xml_like_body = "<html><body>not json at all" + ("x" * 1000) + "</body></html>"
    monkeypatch.setattr(yp, "_open", _fake_open_returning(xml_like_body))
    with caplog.at_level("WARNING"):
        result = YandexSearchProvider(_config()).search("query")
    assert result is None
    text = caplog.text
    assert "decoded_text_is_valid_json=False" in text
    assert f"length={len(xml_like_body)}" in text
    # Body must be truncated, not dumped in full.
    assert len(xml_like_body) > yp._DIAG_BODY_SNIPPET_MAX_CHARS
    assert xml_like_body[:yp._DIAG_BODY_SNIPPET_MAX_CHARS] in text
    assert "x" * 1000 not in text


def test_parse_failure_diagnostics_never_log_api_key(monkeypatch, caplog):
    monkeypatch.setattr(yp, "_open", _fake_open_returning('{"no_docs_key": []}'))
    with caplog.at_level("WARNING"):
        YandexSearchProvider(_config()).search("query")
    assert FAKE_API_KEY not in caplog.text
    assert "Authorization" not in caplog.text


def test_successful_parse_logs_no_diagnostics(monkeypatch, caplog):
    monkeypatch.setattr(
        yp, "_open", _fake_open_returning(_smart_snippets_json([_doc(url="https://example.com/a")])),
    )
    with caplog.at_level("WARNING"):
        result = YandexSearchProvider(_config()).search("query")
    assert result is not None
    assert "web_search_parse_diag" not in caplog.text
