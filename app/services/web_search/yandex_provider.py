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
only used as a fallback when a doc has no ``info_context``. There is no XML
fallback path in this module: Smart Snippets is the only mode this
integration ever requests (the metadata flag is always sent), so a second,
unused parser for the old XML shape would be complexity with no live code
path exercising it - see the task's "Не усложняй код без необходимости".
If plain Web Search is ever needed again, it should come back as an
explicit, separately-tested mode, not a silent fallback here.

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
            decoded_text = self._call(payload)
        except _YandexSearchCallError as exc:
            # Fixed, secret-free reason only - see _YandexSearchCallError.
            log.warning("web_search: yandex request failed (%s)", exc.safe_reason)
            return None
        elapsed_ms = int((monotonic() - started) * 1000)

        try:
            results = _parse_smart_snippet_results(
                decoded_text, provider=self.name, limit=effective_limit
            )
        except ValueError:
            # Covers both malformed JSON (json.JSONDecodeError, a ValueError
            # subclass) and an unexpected-shape payload (_SmartSnippetShapeError).
            log.warning("web_search: yandex response could not be parsed (bad smart snippets json)")
            return None

        return SearchResponse(
            query=query_text,
            results=results,
            provider=self.name,
            elapsed_ms=elapsed_ms,
        )

    def _call(self, payload: dict[str, Any]) -> str:
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
            return base64.b64decode(raw_data).decode("utf-8", errors="replace")
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
