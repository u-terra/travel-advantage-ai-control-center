"""Catalog pages vs. concrete articles (signal -> post quality).

Production 13.10 (see the incident write-up in the commit message): the web
signal collector stored section/catalog pages (``t-j.ru/flows/world-travel/``,
``lonelyplanet.com/articles/category/travel-advice``) as if they were
articles - their "summary" was a description of a LIST of headlines, and the
generated post retold that list. A catalog is only useful for DISCOVERY: its
links lead to the real articles. This module is pure (no network, no I/O):

- ``looks_like_catalog_page`` - URL shape + page shape (many short headline
  links, few long paragraphs) decide "this is a listing";
- ``extract_article_links`` - picks concrete same-site article links out of a
  catalog page's anchors;
- ``parse_published_at`` - normalizes a page-declared date; unknown stays
  ``None`` and is never treated as fresh.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from app.services.web_source_discovery import (
    normalize_article_url,
    title_looks_like_generic_section,
    url_fails_quality_gate,
)

# A path segment from this set marks a taxonomy/listing URL ("/flows/travel/",
# "/articles/category/travel-advice", "/tag/podborka/"). A concrete article
# under such a segment normally has a long descriptive slug (see
# _ARTICLE_SLUG_MIN_TOKENS) or a numeric id.
_LISTING_SEGMENTS = frozenset({
    "flows", "flow", "category", "categories", "tag", "tags", "topic", "topics",
    "rubric", "rubrics", "section", "sections", "archive", "archives",
    "collection", "collections", "subject", "subjects", "theme", "themes",
    "feed", "all", "page",
})
_ARTICLE_SLUG_MIN_TOKENS = 4
_NUMERIC_ID_RE = re.compile(r"\d{5,}")
_PAGINATION_QUERY_RE = re.compile(r"(^|&)(page|p|paged)=\d+", re.IGNORECASE)

# Page-shape signals (used when the URL alone is ambiguous).
_MIN_ARTICLE_LINKS_FOR_CATALOG = 8
_MIN_ANCHOR_CHARS = 25
_MIN_ANCHOR_WORDS = 3
_MAX_LONG_LINE_SHARE_FOR_CATALOG = 0.15
_LONG_LINE_CHARS = 200

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _segments(url: str) -> list[str]:
    try:
        return [s for s in urlsplit(url).path.lower().split("/") if s]
    except ValueError:
        return []


def _slug_looks_like_article(segment: str) -> bool:
    if _NUMERIC_ID_RE.search(segment):
        return True
    return len([t for t in re.split(r"[-_]", segment) if t]) >= _ARTICLE_SLUG_MIN_TOKENS


def url_looks_like_catalog(url: str) -> bool:
    """URL-only verdict: taxonomy/listing path or pagination query. Works on
    ALREADY-STORED rows too (no fetched page needed)."""
    try:
        query = urlsplit(url).query
    except ValueError:
        return False
    if _PAGINATION_QUERY_RE.search(query):
        return True
    segments = _segments(url)
    if not segments:
        return True  # bare domain root
    listing_at = [i for i, s in enumerate(segments) if s in _LISTING_SEGMENTS]
    if not listing_at:
        return False
    last = segments[-1]
    # "/flows/" or "/articles/category/" - the listing word itself is last.
    if last in _LISTING_SEGMENTS:
        return True
    # "/flows/world-travel/": the slug after the listing word is a taxonomy
    # name, not an article, unless it is long/descriptive or carries an id.
    return not _slug_looks_like_article(last)


def _long_line_share(text: str) -> float:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return 0.0
    return sum(1 for ln in lines if len(ln) >= _LONG_LINE_CHARS) / len(lines)


def _host(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _is_candidate_anchor(page_url: str, href: str, anchor: str) -> str:
    """Normalized absolute URL of ``href`` when it is a plausible concrete
    article link from ``page_url`` with a headline-like ``anchor`` text,
    else ""."""
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
        return ""
    from urllib.parse import urljoin  # local: keep module import list small

    absolute = normalize_article_url(urljoin(page_url, href))
    if not absolute or _host(absolute) != _host(page_url):
        return ""
    text = " ".join((anchor or "").split())
    if len(text) < _MIN_ANCHOR_CHARS or len(_WORD_RE.findall(text)) < _MIN_ANCHOR_WORDS:
        return ""
    if title_looks_like_generic_section(text):
        return ""
    if url_fails_quality_gate(absolute) or url_looks_like_catalog(absolute):
        return ""
    if absolute == normalize_article_url(page_url):
        return ""
    return absolute


def extract_article_links(
    page_url: str, links: tuple[tuple[str, str], ...], *, limit: int = 5,
) -> list[str]:
    """Concrete same-site article URLs from a catalog page's anchors, in
    document order (catalogs list newest first), distinct, at most
    ``limit``."""
    found: list[str] = []
    seen: set[str] = set()
    for href, anchor in links:
        candidate = _is_candidate_anchor(page_url, href, anchor)
        if candidate and candidate not in seen:
            seen.add(candidate)
            found.append(candidate)
            if len(found) >= limit:
                break
    return found


def looks_like_catalog_page(
    url: str, text: str, links: tuple[tuple[str, str], ...],
) -> bool:
    """True when the fetched page is a listing of other materials rather
    than one article: a listing URL, or a page made of many headline links
    with almost no long paragraphs."""
    if url_looks_like_catalog(url):
        return True
    article_links = extract_article_links(url, links, limit=_MIN_ARTICLE_LINKS_FOR_CATALOG)
    return (
        len(article_links) >= _MIN_ARTICLE_LINKS_FOR_CATALOG
        and _long_line_share(text) < _MAX_LONG_LINE_SHARE_FOR_CATALOG
    )


def parse_published_at(raw: str | None) -> str | None:
    """ISO timestamp from a page-declared date string, or ``None`` when the
    page declared nothing parseable. Never guesses."""
    value = (raw or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(value[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


# ── sufficiency of what we hold about ONE concrete publication ──────────────

MIN_BODY_CHARS = 500


def has_publication_body(body: str) -> bool:
    """True when a stored publication body is long enough to write from."""
    return len((body or "").strip()) >= MIN_BODY_CHARS
