"""Yandex Web Search API v2 adapter - Smart Snippets mode, NOT the deprecated
XML/v1 API and NOT plain (title/url/headline) Web Search.

Endpoint and request/response schema re-confirmed against the current
official documentation
(https://aistudio.yandex.ru/docs/en/search-api/operations/smart-snippets -
"Getting smart snippets") before this rewrite, per the ORCHESTRAVEL
web-search task's explicit instruction not to guess. The docs page's own
example request body, quoted verbatim:

    {
      "query": {
        "searchType": "SEARCH_TYPE_RU",
        "queryText": "Yandex Cloud"
      },
      "folderId": "<folder_ID>",
      "metadata": {
        "fields": {
          "x-genesis-info-context": "on"
        }
      }
    }

The ``metadata.fields`` key is documented as: "Object containing search
flags in the key:value format. To enable getting smart snippets, provide
the x-genesis-info-context key with on as its value." Without this flag the
same endpoint instead returns plain Web Search results (a different,
XML-shaped payload) - the flag is what selects Smart Snippets, not
``searchType`` (which only selects the RU/regional index).

Two fields present in the *plain* Web Search request are deliberately
ABSENT here, confirmed absent from the Smart Snippets example above:
``responseFormat`` (Smart Snippets responses are always JSON; there is no
XML/HTML choice to make) and ``groupSpec`` (no grouping applies to Smart
Snippets docs). Adding them back would be guessing at an undocumented
combination, and there is nothing they'd buy: the result count is already
capped client-side in ``_parse_smart_snippet_results`` below via ``limit``.

Response envelope is unchanged from plain Web Search: {"rawData": "<base64>"}.
With the Smart Snippets flag set, the base64-decoded payload is UTF-8 JSON
(not XML) shaped as:

    {"docs": [{"Num": 1, "DocumentTitle": "...", "FullUrl": "https://...",
                "Description": "...", "info_context": "..."}, ...]}

``info_context`` is the actual smart-snippet text (what this integration
exists to fetch); ``Description`` is the plain search-result blurb and is
only used as a fallback when a doc has no ``info_context``.

UPDATE (production diagnosis): the Smart Snippets metadata flag is always
sent, but a live SEARCH_TYPE_COM request (the official-source fallback's
worldwide search, see app.services.web_search.service) still came back as
the classic Yandex XML search shape (``<?xml version="1.0" ...>
<yandexsearch>...``) instead of Smart Snippets JSON - apparently the flag
is not honored for every search_type. Rather than guess at request-side
fixes, ``_parse_search_results`` below tries the JSON path first (unchanged
behavior for every case that already worked) and only falls back to
``_parse_xml_search_results`` when the body is not JSON at all. An unknown
third shape still fails soft exactly as before.

No page is fetched here - only the snippet/title/url Yandex itself returns
is used (see the task's explicit "one search call instead of Search + N
fetch").
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from time import monotonic
from typing import Any
from urllib.parse import urlsplit

from app.services.web_search.base import SearchResponse, SearchResult, WebSearchProvider

log = logging.getLogger(__name__)

PROVIDER_NAME = "yandex"

_ENDPOINT = "https://searchapi.api.cloud.yandex.net/v2/web/search"
_MAX_QUERY_TEXT_CHARS = 400

# RU-only for this MVP (the default/normal search): the product and its
# audience are Russian-language travel content (see the rest of this
# codebase's instructions/copy). Not exposed as a config knob - there is no
# real scenario for another default value yet, and adding one is a one-line
# change here plus one new env var when needed.
_SEARCH_TYPE = "SEARCH_TYPE_RU"

# Full documented searchType enum, confirmed against the current official
# docs (both https://aistudio.yandex.ru/docs/en/search-api/concepts/
# web-search.html and https://aistudio.yandex.ru/docs/en/ai-studio/sdk-ref/
# types/search_api.html list the same six values): SEARCH_TYPE_RU (Russian),
# SEARCH_TYPE_TR (Turkish), SEARCH_TYPE_COM (international/worldwide -
# yandex.com), SEARCH_TYPE_KK (Kazakh), SEARCH_TYPE_BE (Belarusian),
# SEARCH_TYPE_UZ (Uzbek). SEARCH_TYPE_COM is the one public, non-guessed
# value for "worldwide" search - used only by the official-source fallback
# in app.services.web_search.service (see SEARCH_TYPE_INTERNATIONAL below),
# never by the default/normal search above.
SEARCH_TYPE_INTERNATIONAL = "SEARCH_TYPE_COM"

# Selects Smart Snippets instead of plain Web Search - see module docstring
# for the verbatim official example this is copied from.
_SMART_SNIPPET_METADATA_FIELD_KEY = "x-genesis-info-context"
_SMART_SNIPPET_METADATA_FIELD_VALUE = "on"


@dataclass(frozen=True)
class YandexSearchConfig:
    api_key: str
    folder_id: str
    timeout_seconds: float = 5.0
    max_results: int = 5

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key.strip()) and bool(self.folder_id.strip())


class _YandexSearchCallError(RuntimeError):
    """Internal, control-flow only - never lets a raw urllib/json exception
    (which could embed request internals) escape this module. Carries a
    short FIXED reason string, never request/response content."""

    def __init__(self, safe_reason: str) -> None:
        super().__init__(safe_reason)
        self.safe_reason = safe_reason


class _SmartSnippetShapeError(ValueError):
    """Decoded payload was valid UTF-8 but not the expected Smart Snippets
    JSON shape ({"docs": [...]})."""


class _XmlSearchShapeError(ValueError):
    """Decoded payload was well-formed XML but not the expected Yandex
    search-results shape (no <doc> elements found anywhere in the tree)."""


class YandexSearchProvider(WebSearchProvider):
    name = PROVIDER_NAME

    def __init__(self, config: YandexSearchConfig) -> None:
        self.config = config

    def search(
        self,
        query: str,
        *,
        site: str | None = None,
        limit: int = 5,
        search_type: str | None = None,
    ) -> SearchResponse | None:
        """``search_type`` optionally overrides the default SEARCH_TYPE_RU
        request scope (e.g. SEARCH_TYPE_INTERNATIONAL for a worldwide
        search) - every existing caller that omits it keeps today's exact
        RU-only behavior unchanged."""
        if not self.config.is_configured:
            return None
        query_text = (query or "").strip()
        if not query_text:
            return None

        effective_limit = max(1, min(int(limit), max(1, self.config.max_results)))
        query_text_for_api = query_text
        if site:
            cleaned_site = _clean_site(site)
            if cleaned_site:
                query_text_for_api = f"{query_text} host:{cleaned_site}"

        payload: dict[str, Any] = {
            "query": {
                "searchType": search_type or _SEARCH_TYPE,
                "queryText": query_text_for_api[:_MAX_QUERY_TEXT_CHARS],
            },
            "folderId": self.config.folder_id,
            "metadata": {
                "fields": {
                    _SMART_SNIPPET_METADATA_FIELD_KEY: _SMART_SNIPPET_METADATA_FIELD_VALUE,
                },
            },
        }

        started = monotonic()
        try:
            decoded_text, http_status = self._call(payload)
        except _YandexSearchCallError as exc:
            # Fixed, secret-free reason only - see _YandexSearchCallError.
            log.warning("web_search: yandex request failed (%s)", exc.safe_reason)
            return None
        elapsed_ms = int((monotonic() - started) * 1000)

        try:
            results = _parse_search_results(
                decoded_text, provider=self.name, limit=effective_limit
            )
        except ValueError:
            # Covers malformed JSON, an unexpected-shape JSON payload,
            # malformed XML, and an unexpected-shape XML payload - see
            # _parse_search_results for which of those actually happened.
            log.warning("web_search: yandex response could not be parsed (bad smart snippets json)")
            # DIAG: TEMPORARY parse-failure-only diagnostics - see banner
            # above _log_parse_failure_diagnostics. Never runs on the
            # success path, so this adds zero log volume when parsing works.
            _log_parse_failure_diagnostics(
                decoded_text, http_status=http_status, search_type=search_type or _SEARCH_TYPE,
            )
            return None

        return SearchResponse(
            query=query_text,
            results=results,
            provider=self.name,
            elapsed_ms=elapsed_ms,
        )

    def _call(self, payload: dict[str, Any]) -> tuple[str, int | None]:
        """Returns ``(decoded_text, http_status)``. ``http_status`` is
        best-effort (``None`` if the response object exposes no ``.status``,
        e.g. in older test doubles) and is ONLY ever used for the temporary
        parse-failure diagnostics in ``search()`` - never for control flow,
        and never derived from request headers, so it carries no secret."""
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            _ENDPOINT,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                # Never logged, never included in any exception below.
                "Authorization": f"Api-Key {self.config.api_key}",
            },
        )
        try:
            with _open(request, timeout=self.config.timeout_seconds) as response:
                # DIAG: best-effort HTTP status, diagnostic-only - see docstring.
                http_status = getattr(response, "status", None)
                raw = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Deliberately never call exc.read(): some APIs echo request
            # headers into error bodies for diagnostics, and this request
            # carries the Authorization header. 401/403 (bad key), 429 (rate
            # limit) and any 5xx all land here as a plain "http_<code>".
            raise _YandexSearchCallError(f"http_{exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise _YandexSearchCallError("network_error") from None
        except (ValueError, UnicodeDecodeError):
            # json.JSONDecodeError is a ValueError subclass.
            raise _YandexSearchCallError("malformed_response") from None

        raw_data = raw.get("rawData") if isinstance(raw, dict) else None
        if not isinstance(raw_data, str) or not raw_data:
            raise _YandexSearchCallError("missing_raw_data")
        try:
            return base64.b64decode(raw_data).decode("utf-8", errors="replace"), http_status
        except (binascii.Error, ValueError):
            raise _YandexSearchCallError("malformed_base64") from None


def _open(request: urllib.request.Request, *, timeout: float):
    """Isolated network seam - tests monkeypatch this function directly
    instead of touching real sockets (same convention as
    app.planner.fetch._open)."""
    return urllib.request.urlopen(request, timeout=timeout)


def _clean_site(site: str) -> str:
    value = site.strip()
    if "://" in value:
        value = urlsplit(value).hostname or ""
    value = value.strip().strip("/")
    if value.lower().startswith("www."):
        value = value[4:]
    return value


# ══════════════════════════════════════════════════════════════════════════
# TEMPORARY PRODUCTION DIAGNOSTIC LOGGING - Smart Snippets parse failure.
#
# Prod log showed "web_search: yandex response could not be parsed (bad
# smart snippets json)" specifically for the official-source fallback call
# (search_type=SEARCH_TYPE_COM), while the default SEARCH_TYPE_RU call
# parses fine. Purpose is to see the ACTUAL shape of the SEARCH_TYPE_COM
# response body - previously invisible, since a parse failure just logged
# one fixed string and returned None. Only ever runs on the parse-failure
# path (never on a successful parse, so zero added log volume in the
# working case), and never touches the parser itself - no algorithm change,
# see search()'s call site above.
#
# No secrets: only HTTP status, the JSON shape/keys of the ALREADY-DECODED
# response body, and a short truncated fragment of that same body are
# logged. The request (which carries Authorization/the API key) is never
# read here - only `decoded_text`, which is Yandex's own response content
# after base64-decoding, and `http_status`, a plain int off the response
# object. Fragment is capped at _DIAG_BODY_SNIPPET_MAX_CHARS to avoid
# dumping a large body.
#
# DELETE EASILY: everything below lives in _log_parse_failure_diagnostics
# (name starts with "_log_parse_failure_" - grep for it, plus its one call
# site in search()) or is one of the two _DIAG_PARSE_* constants.
_DIAG_PARSE_PREFIX = "web_search_parse_diag"
_DIAG_BODY_SNIPPET_MAX_CHARS = 500


def _log_parse_failure_diagnostics(
    decoded_text: str, *, http_status: int | None, search_type: str,
) -> None:
    log.warning(
        "%s: search_type=%r http_status=%s", _DIAG_PARSE_PREFIX, search_type, http_status,
    )
    snippet = decoded_text[:_DIAG_BODY_SNIPPET_MAX_CHARS]

    try:
        parsed = json.loads(decoded_text)
    except ValueError:
        log.warning(
            "%s: decoded_text_is_valid_json=False length=%d snippet=%r",
            _DIAG_PARSE_PREFIX, len(decoded_text), snippet,
        )
        return

    if isinstance(parsed, dict):
        keys = sorted(str(key) for key in parsed.keys())
        log.warning("%s: top_level_type=dict top_level_keys=%s", _DIAG_PARSE_PREFIX, keys)
        docs = parsed.get("docs")
        docs_len = len(docs) if isinstance(docs, (list, str, dict)) else None
        log.warning(
            "%s: docs_key_present=%s docs_type=%s docs_len=%s",
            _DIAG_PARSE_PREFIX, "docs" in parsed, type(docs).__name__, docs_len,
        )
        if isinstance(docs, list) and docs and isinstance(docs[0], dict):
            log.warning(
                "%s: first_doc_keys=%s", _DIAG_PARSE_PREFIX,
                sorted(str(key) for key in docs[0].keys()),
            )
    elif isinstance(parsed, list):
        log.warning(
            "%s: top_level_type=list top_level_len=%d first_item_type=%s",
            _DIAG_PARSE_PREFIX, len(parsed),
            type(parsed[0]).__name__ if parsed else "n/a",
        )
    else:
        log.warning("%s: top_level_type=%s", _DIAG_PARSE_PREFIX, type(parsed).__name__)

    log.warning("%s: decoded_text_snippet=%r", _DIAG_PARSE_PREFIX, snippet)
# ══════════════════ END TEMPORARY PRODUCTION DIAGNOSTIC LOGGING ═══════════


def _parse_search_results(
    decoded_text: str, *, provider: str, limit: int
) -> list[SearchResult]:
    """Dual-format dispatcher. Smart Snippets JSON is the primary/expected
    shape and is tried FIRST, completely unchanged - every query that
    already parsed successfully takes the exact same code path as before
    this dispatcher existed. Only when that fails, and only when the body
    actually looks like XML (starts with ``<``), a second attempt is made
    with the classic Yandex search XML shape - see the module docstring's
    "UPDATE (production diagnosis)" note for why that shape can show up
    despite the Smart Snippets flag always being sent. Anything else
    (neither valid Smart Snippets JSON nor XML) re-raises the original JSON
    error, so search()'s existing fail-soft None + warning + diagnostics
    path is completely unchanged for an unrecognized format.
    """
    try:
        return _parse_smart_snippet_results(decoded_text, provider=provider, limit=limit)
    except ValueError as json_error:
        stripped = decoded_text.lstrip("\ufeff \t\r\n")
        if not stripped.startswith("<"):
            raise
        try:
            return _parse_xml_search_results(decoded_text, provider=provider, limit=limit)
        except ValueError:
            raise json_error from None


def _parse_smart_snippet_results(
    decoded_text: str, *, provider: str, limit: int
) -> list[SearchResult]:
    parsed = json.loads(decoded_text)  # may raise json.JSONDecodeError (ValueError)
    if not isinstance(parsed, dict):
        raise _SmartSnippetShapeError("smart snippets payload is not a JSON object")
    docs = parsed.get("docs")
    if not isinstance(docs, list):
        raise _SmartSnippetShapeError("smart snippets payload has no docs list")

    results: list[SearchResult] = []
    seen_urls: set[str] = set()

    for doc in docs:
        if len(results) >= limit:
            break
        if not isinstance(doc, dict):
            continue

        url = doc.get("FullUrl")
        url = url.strip() if isinstance(url, str) else ""
        if not url:
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)

        title = doc.get("DocumentTitle")
        title = title.strip() if isinstance(title, str) and title.strip() else url

        snippet = doc.get("info_context")
        snippet = snippet.strip() if isinstance(snippet, str) else ""
        if not snippet:
            description = doc.get("Description")
            snippet = description.strip() if isinstance(description, str) else ""

        domain = urlsplit(url).hostname or ""

        num = doc.get("Num")
        # bool is an int subclass in Python - exclude it explicitly so a
        # stray True/False in the payload can't masquerade as a rank.
        rank = num if isinstance(num, int) and not isinstance(num, bool) and num > 0 else len(results) + 1

        results.append(SearchResult(
            title=title,
            url=url,
            snippet=snippet,
            domain=domain,
            # Smart Snippets docs carry no publication-date field - never
            # invented here, only ever a real value if one shows up.
            published_at=None,
            provider=provider,
            rank=rank,
        ))

    return results


def _xml_text(element: ET.Element | None) -> str:
    """Full text content of an XML element, including text inside nested
    child tags (e.g. Yandex's <hlword> highlight markup inside <title>/
    <passage>) - a plain ``element.text`` would silently stop at the first
    child tag and drop everything after it. ``itertext()`` walks the whole
    subtree and ElementTree has already resolved XML entities by this
    point, so no separate unescaping is needed."""
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def _parse_xml_search_results(
    decoded_text: str, *, provider: str, limit: int
) -> list[SearchResult]:
    """Classic Yandex search XML shape (<yandexsearch><response><results>
    <grouping><group><doc>...) - see the module docstring's "UPDATE
    (production diagnosis)" note for why this can show up even though the
    request always asks for Smart Snippets. Only called by
    _parse_search_results as a second attempt, after the Smart Snippets
    JSON parse already failed.

    Deliberately best-effort per Yandex's own documented warning that
    response fields may be absent: <doc> elements are found via a
    depth-first search (``.//doc``) rather than a fixed grouping/group
    path, so this does not depend on exactly how deep the grouping nests
    them. A <doc> with no usable <url> is skipped, never fatal for the
    batch; <domain>/<title>/passages are all optional and fall back the
    same way the JSON parser already does for its own optional fields.
    """
    try:
        root = ET.fromstring(decoded_text)
    except ET.ParseError as exc:
        raise _XmlSearchShapeError("not well-formed XML") from exc

    docs = root.findall(".//doc")
    if not docs:
        raise _XmlSearchShapeError("no <doc> elements found in XML response")

    results: list[SearchResult] = []
    seen_urls: set[str] = set()

    for doc in docs:
        if len(results) >= limit:
            break

        url = _xml_text(doc.find("url"))
        if not url:
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)

        domain = _xml_text(doc.find("domain")) or (urlsplit(url).hostname or "")
        title = _xml_text(doc.find("title")) or url

        # Snippet: join whatever <passages><passage> text is present - the
        # task's explicit source for the XML snippet. Docs may have zero,
        # one, or several passages (Yandex's own docs: up to 4 by default).
        passage_texts = [_xml_text(passage) for passage in doc.findall("./passages/passage")]
        snippet = " ".join(text for text in passage_texts if text)

        results.append(SearchResult(
            title=title,
            url=url,
            snippet=snippet,
            domain=domain,
            # Classic XML docs carry a <modtime> in some configurations, but
            # it is not documented as always-present - never invented here,
            # same policy as the JSON parser's published_at.
            published_at=None,
            provider=provider,
            # Rank is by order of appearance in the XML, per this task -
            # unlike the JSON parser's docs there is no per-doc ordinal
            # field (like "Num") documented for this shape to prefer.
            rank=len(results) + 1,
        ))

    return results
