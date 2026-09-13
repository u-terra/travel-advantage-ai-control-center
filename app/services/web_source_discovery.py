"""Stage 3 (ORCHESTRAVEL): finds specific, fresh article URLs inside one
platform="web" source's own domain, instead of only ever reading that
source's canonical landing page (Stage 2's behavior).

Reuses the search primitive this codebase already has -
app.services.web_search.yandex_provider.YandexSearchProvider, via the
provider-agnostic app.services.web_search.base.WebSearchProvider contract -
restricted to one domain with its existing ``site=`` parameter (Yandex's own
``host:`` operator, see YandexSearchProvider.search). No new crawler, no new
provider, no per-source business logic: every platform="web" source
(including trip_com) goes through the exact same
``search(GENERIC_QUERY, site=domain)`` call - a source's own metadata
(id, name) never changes which query is sent or how results are filtered,
so nothing here can single out or force-include any one source over another.

Deliberately does NOT reuse app.services.web_search.service.WebSearchService
directly: that class's job (``maybe_search``/``decide_web_search``) is "does
THIS chat message need a search", a decision policy that has no meaning
here - a source's subscription being enabled already IS the decision to
look at it. This module talks to the lower-level WebSearchProvider contract
instead (see WebSearchService.provider for how a caller gets one).
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.domain.sources import WorkspaceSource, normalize_url
from app.services.web_search.base import WebSearchProvider

log = logging.getLogger(__name__)

# One fixed, bilingual, source-agnostic anchor query - not tied to any
# source's id/name/notes, so it cannot become a Trip-only (or any-source-
# only) special case. Restricted per-call to one domain via `site=`.
DISCOVERY_QUERY = "путешествия travel guide советы статьи"

_MAX_DISCOVERY_CANDIDATES = 5

# Known tracking/analytics query parameters - stripped so the same article
# reached via a different campaign link normalizes to one dedupe key. Never
# touches the fragment (`#...`) or any other query parameter: those can be
# meaningful parts of the address (see app.domain.sources.normalize_url's
# own docstring on this point), and blindly stripping them would silently
# change which page the link actually points to.
_TRACKING_QUERY_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_referrer", "yclid", "gclid", "fbclid", "msclkid", "ref", "referrer",
    "from",
})


def normalize_article_url(url: str) -> str:
    """Strips known tracking query parameters, then applies the same
    canonicalization the source registry already uses for every other URL
    in this codebase (scheme/host lowercasing, `/`-only path -> "",
    query/fragment otherwise preserved) - see app.domain.sources.normalize_url.
    Empty string for anything that isn't a normalizable http(s) address.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    kept_query = [
        (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_QUERY_PARAMS
    ]
    stripped = urlunsplit((
        parts.scheme, parts.netloc, parts.path, urlencode(kept_query), parts.fragment,
    ))
    return normalize_url(stripped)


def _source_domain(source: WorkspaceSource) -> str:
    try:
        return urlsplit(source.target).hostname or ""
    except ValueError:
        return ""


def discover_candidate_urls(
    provider: WebSearchProvider, source: WorkspaceSource, *, limit: int = _MAX_DISCOVERY_CANDIDATES,
) -> list[str]:
    """Distinct, normalized article URLs found inside ``source``'s own
    domain - never includes ``source.target`` itself (that is the landing
    page, handled separately as a lower-priority fallback by the caller).
    Empty list on any failure (search disabled/unconfigured/errored, no
    results, or the domain could not be determined) - the caller falls back
    to the landing page, exactly as before this module existed.
    """
    domain = _source_domain(source)
    if not domain:
        return []
    try:
        response = provider.search(DISCOVERY_QUERY, site=domain, limit=limit)
    except Exception:
        log.info("web_source_discovery: search raised for domain '%s'", domain, exc_info=True)
        return []
    if response is None or not response.results:
        return []

    own_url = normalize_article_url(source.target)
    seen: set[str] = set()
    candidates: list[str] = []
    for result in response.results:
        normalized = normalize_article_url(result.url)
        if not normalized or normalized == own_url or normalized in seen:
            continue
        seen.add(normalized)
        candidates.append(normalized)
        if len(candidates) >= limit:
            break
    return candidates
