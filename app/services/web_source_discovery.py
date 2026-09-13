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
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.domain.sources import WorkspaceSource, normalize_url
from app.services.web_search.base import WebSearchProvider

log = logging.getLogger(__name__)

_MAX_DISCOVERY_CANDIDATES = 5


def discovery_query(*, now: datetime | None = None) -> str:
    """Stage 3.1: same fixed, bilingual, source-agnostic anchor as before,
    plus a live freshness nudge (current year/month, RU+EN "new/updated"
    words) so Yandex's own ranking leans toward recently-published pages -
    still not tied to any source's id/name/notes, so trip_com (or any other
    source) cannot become a special case through this. ``now`` is
    injectable for tests; production always calls this with no argument.
    """
    moment = now or datetime.now(timezone.utc)
    month_names_ru = (
        "января", "февраля", "марта", "апреля", "мая", "июня", "июля",
        "августа", "сентября", "октября", "ноября", "декабря",
    )
    month_ru = month_names_ru[moment.month - 1]
    return (
        f"путешествия travel guide советы статьи {moment.year} "
        f"{month_ru} новое свежее updated latest"
    )


# Known tracking/analytics query parameters - stripped so the same article
# reached via a different campaign link normalizes to one dedupe key. Never
# touches the fragment (`#...`) or any other query parameter: those can be
# meaningful parts of the address (see app.domain.sources.normalize_url's
# own docstring on this point), and blindly stripping them would silently
# change which page the link actually points to. `utm_` is a prefix match
# (covers utm_source/utm_medium/... and any future utm_ variant Google/
# Yandex add) - everything else here is an exact name match.
_TRACKING_QUERY_PARAM_PREFIXES = ("utm_",)
_TRACKING_QUERY_PARAM_NAMES = frozenset({
    "yclid", "gclid", "fbclid", "msclkid", "ref", "referrer", "from", "source",
    # Stage 3.1 live bug: Т-Ж's own widget-tracking parameter, observed in
    # production smoke-test output (?cdwuid_attempt=1) - same shape as the
    # rest of this set (a tracking id, not part of the article's identity).
    "cdwuid_attempt",
})


def _is_tracking_param(name: str) -> bool:
    lowered = name.lower()
    return lowered in _TRACKING_QUERY_PARAM_NAMES or lowered.startswith(_TRACKING_QUERY_PARAM_PREFIXES)


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
        if not _is_tracking_param(key)
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


# ── Stage 3.1 Quality Gate ───────────────────────────────────────────────────
# Two independent checks, deliberately generic (path/content markers, never a
# source id) so nothing here can be read as "special-case Aviasales" or
# "special-case Trip" - the same list applies to every platform="web" source.

# Checked against URL PATH SEGMENTS only (never the query/fragment/domain),
# case-insensitive substring match per segment. Covers the site-boilerplate
# pages a domain-restricted search can surface alongside real articles:
# careers/support pages, auth screens, and dead/removed-page URLs.
_NON_CONTENT_PATH_MARKERS: tuple[str, ...] = (
    "about", "vacanc", "career", "jobs", "job", "support", "help",
    "login", "signin", "sign-in", "signup", "sign-up", "register",
    "not-found", "notfound", "404", "contact", "privacy", "terms",
    "cookie", "sitemap",
)


def _rejected_by_url_heuristic(url: str) -> str | None:
    """Returns the matched marker (for logging) if ``url``'s path looks like
    a non-article boilerplate/dead page, else None. Never inspects the
    domain or query string - a legitimate article path containing e.g.
    "sign-in-to-see-prices" in its slug is an acceptable false negative
    (this is a cheap pre-fetch filter, not the only gate - see
    page_looks_like_non_content for the post-fetch content check)."""
    try:
        path = urlsplit(url).path.lower()
    except ValueError:
        return None
    segments = [segment for segment in path.split("/") if segment]
    for segment in segments:
        for marker in _NON_CONTENT_PATH_MARKERS:
            if marker in segment:
                return marker
    return None


# Checked against the FETCHED page's own title/extracted text (bilingual) -
# catches what the URL alone cannot: a 404 that returns HTTP 200 with a
# "page not found" body (common on many sites, including apparently
# Aviasales's /about/vacancies/.../not-found path observed in the Stage 3
# smoke test), or a page whose URL looked fine but turned out to be a
# careers/support page anyway.
_NON_CONTENT_CONTENT_MARKERS: tuple[str, ...] = (
    "страница не найдена", "не найдена", "ошибка 404", "404 error",
    "page not found", "not found", "error 404",
    "vacanc", "we're hiring", "we are hiring", "job opening",
    "career opportunit", "join our team",
    # Most default-pack sources are Russian-language (Aviasales, Т-Ж,
    # OneTwoTrip, Туту, Яндекс) - an English-only marker list would miss a
    # purely Russian vacancy/careers page whose URL path also happened not
    # to match _NON_CONTENT_PATH_MARKERS.
    "вакансия", "вакансии", "открытые вакансии", "присоединяйтесь к команде",
    "присоединяйся к команде", "работа в компании", "мы нанимаем",
)


def page_looks_like_non_content(title: str, text: str) -> str | None:
    """Returns the matched marker (for logging) if the fetched page's own
    title/text looks like a 404/vacancy/support page rather than travel
    content, else None. Checked against a bounded prefix of the extracted
    text (matches how much of it analyze_source itself ever sees) - a
    marker phrase buried deep in an otherwise legitimate long article is a
    deliberate, accepted false negative rather than a reason to scan the
    entire page."""
    haystack = f"{title or ''} {(text or '')[:2000]}".lower()
    for marker in _NON_CONTENT_CONTENT_MARKERS:
        if marker in haystack:
            return marker
    return None


# ── Stage 3.1 freshness ranking (not storage - published_at stays honest) ──

_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
# How many years behind "now" counts as stale enough to rank below a fresh
# result - matches the concrete production example (current year 2026,
# "за 2024 год" flagged as stale: 2026 - 2024 = 2).
_STALE_YEAR_OFFSET = 2


def mentions_stale_year(text: str, *, now: datetime | None = None) -> bool:
    """True if ``text`` (title + summary - never the raw fetched page,
    which routinely has copyright-footer years that are not about the
    article's own content) names an old year and does NOT also name the
    current (or next) year. A page mentioning both an old and the current
    year (e.g. "с 2015 года, обновлено в 2026") is treated as genuinely
    current, not stale - see the module's own "не выдумывать published_at"
    constraint: this never writes a date, it only demotes ranking.
    """
    moment = now or datetime.now(timezone.utc)
    years = {int(match) for match in _YEAR_RE.findall(text or "")}
    if not years:
        return False
    if moment.year in years or (moment.year + 1) in years:
        return False
    cutoff = moment.year - _STALE_YEAR_OFFSET
    return any(year <= cutoff for year in years)


def discover_candidate_urls(
    provider: WebSearchProvider, source: WorkspaceSource, *, limit: int = _MAX_DISCOVERY_CANDIDATES,
) -> list[str]:
    """Distinct, normalized, quality-filtered article URLs found inside
    ``source``'s own domain - never includes ``source.target`` itself (that
    is the landing page, handled separately as a lower-priority fallback by
    the caller) and never a URL rejected by the pre-fetch URL heuristic
    (_rejected_by_url_heuristic - see the Quality Gate section above; the
    post-fetch content check happens later, in the collector, since it
    needs the actually-fetched page). Empty list on any failure (search
    disabled/unconfigured/errored, no results, every candidate filtered
    out, or the domain could not be determined) - the caller falls back to
    the landing page, exactly as before this module existed.
    """
    domain = _source_domain(source)
    if not domain:
        return []
    try:
        response = provider.search(discovery_query(), site=domain, limit=limit)
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
        rejected_marker = _rejected_by_url_heuristic(normalized)
        if rejected_marker is not None:
            log.info(
                "web_source_discovery: URL rejected for domain '%s' "
                "(matched '%s'): %s", domain, rejected_marker, normalized,
            )
            continue
        seen.add(normalized)
        candidates.append(normalized)
        if len(candidates) >= limit:
            break
    return candidates
