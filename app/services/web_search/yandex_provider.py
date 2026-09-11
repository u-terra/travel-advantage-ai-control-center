"""Yandex Web Search API v2 adapter (Search API / Smart Snippets), NOT the
deprecated XML/v1 API.

Endpoint and schema confirmed against the current official documentation
(https://aistudio.yandex.ru/docs/en/search-api/ - "Web Search API, REST:
WebSearch.Search" and "Getting started with Yandex Search API") before
writing this file, per the ORCHESTRAVEL web-search task's explicit
instruction not to guess:

    POST https://searchapi.api.cloud.yandex.net/v2/web/search
    Headers: Authorization: Api-Key <api_key>
    Body:    {"query": {"searchType": ..., "queryText": ...},
              "folderId": ..., "responseFormat": "FORMAT_XML" | "FORMAT_HTML",
              "groupSpec": {"groupMode": ..., "groupsOnPage": ..., "docsInGroup": ...}}
    Response: {"rawData": "<base64>"}  - base64-encoded XML (or HTML) document.

``responseFormat=FORMAT_XML`` is used here (not HTML) because the decoded
payload is the well-established Yandex search XML shape
(``yandexsearch/response/results/grouping/group/doc`` with ``url``/``title``/
``headline``/``passages`` children) - the same shape Yandex's XML search API
has used for years, now delivered base64-wrapped inside a v2 JSON envelope.
Third-party client libraries built against this v2 endpoint
(e.g. github.com/starkeen/yandex-search-api) confirm the same
title/url/domain/snippet field mapping used below.

Honest limitation, documented rather than silently assumed: the Search-API-
specific operator list documented at .../concepts/search-operators only
covers word-level operators (``-``, ``!``, ``+``, quotes, ``[]``, ``(|)``) -
it does NOT list a ``site:``/``host:`` domain-restriction operator for this
particular API. ``host:<domain>`` is Yandex's long-standing general search
query-language operator (see yandex.com/support/search "query-language" docs)
and this endpoint runs on the same search index, so it is used here as a
best-effort restriction: if Yandex ignores or mishandles it, the query simply
degrades to an unrestricted search (never an error), which is exactly the
fail-soft behavior this module is built around either way.

No page is fetched here - only the snippet/title/url Yandex itself returns is
used (see the task's explicit "one search call instead of Search + N fetch").
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

# RU-only for this MVP: the product and its audience are Russian-language
# travel content (see the rest of this codebase's instructions/copy). Not
# exposed as a config knob - there is no real scenario for another value yet,
# and adding one is a one-line change here plus one new env var when needed.
_SEARCH_TYPE = "SEARCH_TYPE_RU"
_RESPONSE_FORMAT = "FORMAT_XML"
# GROUP_MODE_FLAT + docsInGroup=1: one document per group, so
# groupsOnPage behaves like a plain "top N results" limit instead of
# Yandex's usual "N distinct hosts, each possibly expandable" grouping -
# the simplest mapping onto SearchResponse.results.
_GROUP_MODE = "GROUP_MODE_FLAT"
_DOCS_IN_GROUP = 1


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


class YandexSearchProvider(WebSearchProvider):
    name = PROVIDER_NAME

    def __init__(self, config: YandexSearchConfig) -> None:
        self.config = config

    def search(
        self, query: str, *, site: str | None = None, limit: int = 5
    ) -> SearchResponse | None:
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
                "searchType": _SEARCH_TYPE,
                "queryText": query_text_for_api[:_MAX_QUERY_TEXT_CHARS],
            },
            "folderId": self.config.folder_id,
            "responseFormat": _RESPONSE_FORMAT,
            "groupSpec": {
                "groupMode": _GROUP_MODE,
                "groupsOnPage": effective_limit,
                "docsInGroup": _DOCS_IN_GROUP,
            },
        }

        started = monotonic()
        try:
            xml_text = self._call(payload)
        except _YandexSearchCallError as exc:
            # Fixed, secret-free reason only - see _YandexSearchCallError.
            log.warning("web_search: yandex request failed (%s)", exc.safe_reason)
            return None
        elapsed_ms = int((monotonic() - started) * 1000)

        try:
            results = _parse_xml_results(
                xml_text, provider=self.name, limit=effective_limit
            )
        except ET.ParseError:
            log.warning("web_search: yandex response could not be parsed (bad xml)")
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


def _parse_xml_results(
    xml_text: str, *, provider: str, limit: int
) -> list[SearchResult]:
    root = ET.fromstring(xml_text)
    results: list[SearchResult] = []
    seen_urls: set[str] = set()

    for doc in root.iter("doc"):
        if len(results) >= limit:
            break

        url = _text(doc.find("url")).strip()
        if not url:
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)

        title = _text(doc.find("title")) or url
        domain = _text(doc.find("domain")) or (urlsplit(url).hostname or "")

        results.append(SearchResult(
            title=title,
            url=url,
            snippet=_extract_snippet(doc),
            domain=domain,
            # The Search API's <doc> does not carry a publication date field
            # - never invented here, only ever a real value if one shows up.
            published_at=None,
            provider=provider,
            rank=len(results) + 1,
        ))

    return results


def _extract_snippet(doc: ET.Element) -> str:
    passages = doc.find("passages")
    if passages is not None:
        parts = [_text(passage) for passage in passages.findall("passage")]
        joined = " … ".join(part for part in parts if part)
        if joined:
            return joined
    return _text(doc.find("headline"))


def _text(element: ET.Element | None) -> str:
    """Text content including nested tags (Yandex wraps matched query words
    in <hlword> inside title/headline/passage) - itertext() flattens that."""
    if element is None:
        return ""
    return "".join(element.itertext()).strip()
