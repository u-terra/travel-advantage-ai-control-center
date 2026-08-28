"""Minimal, SSRF-hardened public URL fetch + HTML text extraction.

Backs the ``fetch_public_source`` executor (see ``app.planner.executors``):
URL of a competitor's public page -> extracted readable text + basic
metadata, ready for ``analyze_source``. Stdlib-only by design - the project
declares no HTTP/HTML dependency beyond ``urllib``/``html.parser`` (see
``requirements.txt``; ``app.services.content_factory`` and
``app.orchestration.openai_provider`` already use the same
``urllib.request`` pattern for outbound HTTP), so this does not introduce a
new dependency.

Threat model / defenses:

- Only ``http``/``https`` schemes are accepted.
- The hostname is resolved once via ``socket.getaddrinfo`` and EVERY
  resolved address is checked against private/loopback/link-local/reserved/
  multicast/unspecified ranges (covers RFC1918, 127.0.0.0/8, 169.254.0.0/16
  - which includes the AWS/GCP/Azure metadata address 169.254.169.254 - and
  IPv6 equivalents, including IPv4-mapped IPv6 addresses used to smuggle a
  private v4 address inside a v6 literal).
- The literal hostnames ``localhost`` and ``metadata.google.internal`` are
  blocked outright, before any DNS resolution.
- The actual TCP connection is pinned to the exact address that was just
  validated (see ``_PinnedHTTPConnection``/``_PinnedHTTPSConnection``) -
  resolving once for validation and again for the real connection would
  leave a classic DNS-rebinding gap (a malicious host answering safely to
  the first lookup and pointing at an internal address on the second). This
  is done entirely at the connection-instance level, via the
  ``self._create_connection`` seam ``http.client.HTTPConnection`` itself
  documents as "stored as an instance variable to allow unit tests to
  replace it" - there is NO process-global monkeypatch of
  ``socket.getaddrinfo``/``socket.create_connection`` anywhere in this
  module. TLS SNI/certificate hostname verification is untouched stock
  ``http.client.HTTPSConnection.connect()`` behavior, which wraps the socket
  with ``server_hostname=self.host`` - since ``self.host`` is never changed
  (only the low-level ``_create_connection`` callable is), verification
  keeps checking the real hostname even though the raw TCP socket connects
  to the pinned IP.
- Redirects are never auto-followed by urllib: each ``Location`` is
  intercepted (see ``_NoAutoRedirectHandler``) and re-validated/re-resolved/
  re-pinned exactly like the initial URL, up to ``MAX_REDIRECTS`` hops.
- Response size is capped (``Content-Length`` is checked up front when
  present, and the body is additionally read in bounded chunks so a server
  lying about its length cannot bypass the cap).
- Only ``text/html``/``text/plain`` content types are accepted - anything
  else (including any binary content type) is rejected before the body is
  even decoded as text.
- No JavaScript execution, no browser automation - a single GET request via
  ``urllib.request``, nothing more.

Known limitation (documented, not silently ignored): once a step succeeds,
the underlying content is not re-verified as still public/safe on a second
call - matches the MVP scope. There is no scanning of response *content*
(e.g. an internal admin page happening to be reachable from a public
hostname is not a fetch-layer concern).

Stage 3.2 hotfix: ``_PinnedHTTPSHandler.https_open`` used to also forward
``check_hostname=self._check_hostname`` to ``do_open``. Python 3.11's
``HTTPSHandler.__init__`` sets that attribute (to ``None``); Python 3.12
removed it entirely (``check_hostname`` is folded into the context object
instead), so every HTTPS fetch crashed with ``AttributeError`` in production
(3.12) while passing every local test (3.11). Fixed by never referencing
``self._check_hostname`` at all - ``self._context`` (set by
``HTTPSHandler.__init__`` on both versions, with secure defaults -
``check_hostname=True``, ``verify_mode=CERT_REQUIRED`` - when no context is
supplied) is the only thing threaded through, which is both version-safe and
does not weaken verification.

Stage 3.3 VK compatibility: a live test against a real vk.ru community page
came back HTTP 404 from production, even though the same URL opens fine in a
browser. Diagnosed read-only (no code change) by reproducing the exact
request from the production VPS: plain ``urlopen`` with this module's own
headers succeeded (200) on some attempts and failed (404, then separately
"too short content") on others from the SAME VPS/IP - narrowing it to VK's
own inconsistent/defensive backend behavior (VK resolves to 6+ distinct
IPs), not our SSRF/TLS/pinning logic, which was independently confirmed
correct. Separately, inspecting the actual successful response showed both
vk.ru AND vk.com serve a ~110KB client-side-rendered app shell for community
pages - the real content is injected by JavaScript after load, which this
fetcher deliberately never executes (no browser automation, per design).
Two narrow, honest responses to this, neither of which is a full fix for
JS-rendered pages (impossible without a browser, which is out of scope):

- A realistic desktop-browser ``User-Agent``/``Accept``/``Accept-Language``
  (see ``_USER_AGENT`` etc.) - reduces the chance of being treated as an
  obviously-scripted client by header-based heuristics. No cookies, no
  Referer, no faked auth.
- A single, narrow ``vk.ru`` -> ``vk.com`` fallback (see
  ``_vk_fallback_url``) for when one host fails and the other might not -
  exactly one extra attempt, both URLs independently pass full SSRF
  validation, never applied to any other domain.
- Explicit login-wall/bot-check/JS-shell detection (see
  ``_require_meaningful_content``) so a page like this fails with a clear,
  specific ``PublicSourceFetchError`` instead of either silently "succeeding"
  with unusable content or being indistinguishable from any other
  too-short-content failure.
"""

from __future__ import annotations

import functools
import http.client
import ipaddress
import logging
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger(__name__)

ALLOWED_SCHEMES = frozenset({"http", "https"})
MAX_REDIRECTS = 5
REQUEST_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 1_500_000
ALLOWED_CONTENT_TYPE_PREFIXES = ("text/html", "text/plain")
# Mirrors app.handlers.source_analysis's own 12_000-char cap on user-pasted
# text (receive_source_text) - fetched content feeds the same analyze_source
# call, so it should not exceed what that flow already treats as reasonable.
MAX_EXTRACTED_TEXT_CHARS = 12_000
MIN_EXTRACTED_TEXT_CHARS = 40
# Stage 3.3: an ordinary current desktop browser UA/Accept/Accept-Language -
# NOT a real browser's TLS fingerprint (that would need a different HTTP
# stack entirely), but enough to stop looking like an obviously-scripted
# client to header-based heuristics. No cookies, no auth, no Referer - see
# module docstring for why this does not cross into "browser automation".
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_ACCEPT_HEADER = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
_ACCEPT_LANGUAGE_HEADER = "ru-RU,ru;q=0.9,en;q=0.8"

_BLOCKED_HOSTNAMES = frozenset({"localhost", "metadata.google.internal"})

# Stage 3.3: narrow, single-hop VK domain fallback (see module docstring).
_VK_FALLBACK_SOURCE_HOST = "vk.ru"
_VK_FALLBACK_TARGET_HOST = "vk.com"

# Stage 3.3: login-wall/anti-bot/JS-shell detection. A page this large in
# raw HTML with almost no extractable visible text is not a coincidence -
# confirmed against real vk.ru/vk.com community pages, which ship a
# client-side-rendered app shell (~110KB of HTML/JS) with the actual content
# injected by JavaScript we deliberately never execute.
_LIKELY_JS_SHELL_MIN_HTML_CHARS = 5_000
_BOT_OR_LOGIN_WALL_MARKERS = (
    "enable javascript",
    "включите javascript",
    "поддержку javascript",
    "checking your browser",
    "verify you are human",
    "are you a robot",
    "captcha",
)

_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


class PublicSourceFetchError(RuntimeError):
    """Controlled fetch failure - bad scheme, blocked host, oversized/
    binary/timeout/network error, or empty extracted content. Callers must
    never let a raw socket/urllib exception escape past this module."""


@dataclass(frozen=True)
class FetchedPublicSource:
    url: str
    final_url: str
    title: str
    text: str
    content_type: str


class _RedirectCapture(Exception):
    """Raised by _NoAutoRedirectHandler.redirect_request to hand the
    redirect target back to _fetch_with_redirects for its own
    validate-then-follow loop, instead of letting urllib auto-follow it
    unvalidated."""

    def __init__(self, location: str) -> None:
        super().__init__(location)
        self.location = location


class _NoAutoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise _RedirectCapture(newurl)


def _is_blocked_ip(ip_text: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return True  # cannot parse -> fail closed
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _resolve_safe_address(hostname: str, port: int) -> str:
    """Resolves hostname and rejects it if ANY resolved address is unsafe -
    a hostname resolving to both a public and a private address is treated
    as unsafe, fail-closed. Returns the first resolved address, used as the
    pinned connection target (see _PinnedHTTPConnection/_PinnedHTTPSConnection
    below)."""
    try:
        infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise PublicSourceFetchError(f"cannot resolve host: {hostname}") from exc
    if not infos:
        raise PublicSourceFetchError(f"cannot resolve host: {hostname}")

    ips: list[str] = []
    for info in infos:
        ip_text = info[4][0]
        if ip_text not in ips:
            ips.append(ip_text)

    for ip_text in ips:
        if _is_blocked_ip(ip_text):
            raise PublicSourceFetchError(
                f"host {hostname} resolves to a blocked address ({ip_text})"
            )
    return ips[0]


def _validate_and_resolve(url: str) -> tuple[str, str, str, int]:
    """Returns (url, hostname, resolved_ip, port). Raises
    PublicSourceFetchError on any scheme/host/address violation."""
    parts = urlsplit(url)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise PublicSourceFetchError(f"unsupported scheme: {parts.scheme!r}")
    hostname = (parts.hostname or "").strip().lower()
    if not hostname:
        raise PublicSourceFetchError("URL has no host")
    if hostname in _BLOCKED_HOSTNAMES:
        raise PublicSourceFetchError(f"blocked host: {hostname}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    resolved_ip = _resolve_safe_address(hostname, port)
    return url, hostname, resolved_ip, port


def _pinned_create_connection(pinned_ip: str):
    """Returns a drop-in replacement for socket.create_connection that
    ignores the hostname it is given and always dials `pinned_ip` instead,
    keeping the port. Assigned to a single connection INSTANCE's
    `_create_connection` attribute (see the classes below) - never to the
    module-level `socket.create_connection` itself, so nothing process-global
    is ever touched."""

    def _create(address: tuple[str, int], timeout: Any = None, source_address: Any = None) -> Any:
        _original_host, port = address
        return socket.create_connection((pinned_ip, port), timeout, source_address)

    return _create


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Plain HTTP: connects to `pinned_ip` at the TCP layer while `self.host`
    (used for the Host header) stays the original hostname."""

    def __init__(self, host: str, port: int | None = None, *, pinned_ip: str, **kwargs: Any) -> None:
        super().__init__(host, port, **kwargs)
        self._create_connection = _pinned_create_connection(pinned_ip)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS: same TCP-level pinning as _PinnedHTTPConnection. Deliberately
    does NOT override connect() - http.client.HTTPSConnection.connect()
    calls super().connect() (which uses our pinned self._create_connection
    for the socket) and then does
    ``self._context.wrap_socket(self.sock, server_hostname=self.host)``
    completely unmodified, so TLS SNI and certificate hostname verification
    keep checking the real hostname, never the pinned IP. check_hostname/
    verify_mode are whatever the caller's ssl.SSLContext says (default:
    Python's stock secure defaults, since no context is weakened here)."""

    def __init__(self, host: str, port: int | None = None, *, pinned_ip: str, **kwargs: Any) -> None:
        super().__init__(host, port, **kwargs)
        self._create_connection = _pinned_create_connection(pinned_ip)


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, pinned_ip: str) -> None:
        super().__init__()
        self._pinned_ip = pinned_ip

    def http_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(
            functools.partial(_PinnedHTTPConnection, pinned_ip=self._pinned_ip), req,
        )


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, pinned_ip: str) -> None:
        super().__init__()
        self._pinned_ip = pinned_ip

    def https_open(self, req: urllib.request.Request) -> Any:
        # Stage 3.2 hotfix: do NOT pass check_hostname here. self._context is
        # the only attribute HTTPSHandler.__init__ is guaranteed to set -
        # Python 3.12 removed self._check_hostname entirely (check_hostname
        # is folded straight into the context object instead), while 3.11
        # still set it to None. Referencing self._check_hostname crashed
        # every HTTPS fetch on 3.12 with AttributeError (confirmed in
        # production). self._context already carries a secure default
        # (check_hostname=True, verify_mode=CERT_REQUIRED, created by
        # HTTPSHandler.__init__ itself when no context is passed in) on both
        # versions, so omitting check_hostname here does not weaken
        # verification - it removes a redundant, version-fragile pass-through
        # of a setting the context already encodes.
        return self.do_open(
            functools.partial(_PinnedHTTPSConnection, pinned_ip=self._pinned_ip),
            req,
            context=self._context,
        )


def _open(req: urllib.request.Request, *, timeout: float, pinned_ip: str) -> Any:
    """Isolated seam for the actual network call - tests monkeypatch this
    function directly instead of touching real sockets. Builds a fresh
    opener per call using connection classes pinned to `pinned_ip` - no
    shared/global state, safe under concurrent use."""
    opener = urllib.request.build_opener(
        _NoAutoRedirectHandler,
        _PinnedHTTPHandler(pinned_ip),
        _PinnedHTTPSHandler(pinned_ip),
    )
    return opener.open(req, timeout=timeout)


def _check_content_length(headers: dict[str, str]) -> None:
    raw = headers.get("content-length")
    if raw is None:
        return
    try:
        declared = int(raw)
    except ValueError:
        return
    if declared > MAX_RESPONSE_BYTES:
        raise PublicSourceFetchError("declared response size exceeds limit")


def _read_limited(response: Any, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise PublicSourceFetchError("response exceeds maximum allowed size")
        chunks.append(chunk)
    return b"".join(chunks)


def _fetch_with_redirects(start_url: str) -> tuple[str, str, bytes]:
    """Returns (final_url, content_type, body). Validates and pins every hop
    - including redirect targets - before connecting."""
    url = start_url
    for _ in range(MAX_REDIRECTS + 1):
        normalized_url, _hostname, resolved_ip, port = _validate_and_resolve(url)
        req = urllib.request.Request(
            normalized_url,
            method="GET",
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": _ACCEPT_HEADER,
                "Accept-Language": _ACCEPT_LANGUAGE_HEADER,
            },
        )
        try:
            with _open(req, timeout=REQUEST_TIMEOUT_SECONDS, pinned_ip=resolved_ip) as resp:
                headers = {
                    key.lower(): value for key, value in resp.getheaders()
                }
                _check_content_length(headers)
                body = _read_limited(resp, MAX_RESPONSE_BYTES)
                content_type = headers.get("content-type", "")
                return normalized_url, content_type, body
        except _RedirectCapture as exc:
            url = exc.location
            continue
        except PublicSourceFetchError:
            raise
        except urllib.error.HTTPError as exc:
            raise PublicSourceFetchError(f"HTTP error {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PublicSourceFetchError(f"network error: {exc}") from exc
    raise PublicSourceFetchError(f"too many redirects (max {MAX_REDIRECTS})")


def _require_allowed_content_type(content_type: str) -> str:
    normalized = content_type.split(";", 1)[0].strip().lower()
    if not any(normalized.startswith(prefix) for prefix in ALLOWED_CONTENT_TYPE_PREFIXES):
        raise PublicSourceFetchError(f"unsupported content type: {content_type!r}")
    return normalized


def _decode_body(body: bytes, content_type: str) -> str:
    charset = "utf-8"
    for param in content_type.split(";")[1:]:
        param = param.strip()
        if param.lower().startswith("charset="):
            charset = param.split("=", 1)[1].strip().strip('"')
            break
    try:
        return body.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return body.decode("utf-8", errors="replace")


class _TextExtractingParser(HTMLParser):
    _SKIP_TAGS = frozenset({"script", "style", "noscript", "template"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self.title = ""

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
            return
        stripped = data.strip()
        if stripped:
            self._chunks.append(stripped)

    def get_text(self) -> str:
        return "\n".join(self._chunks)


def _extract_html(html_text: str) -> tuple[str, str]:
    parser = _TextExtractingParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception as exc:
        raise PublicSourceFetchError(f"failed to parse HTML: {exc}") from exc
    return parser.get_text(), parser.title.strip()


def _collapse_whitespace(text: str) -> str:
    text = _WHITESPACE_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def _looks_like_bot_or_login_wall(raw_html_lower: str) -> bool:
    return any(marker in raw_html_lower for marker in _BOT_OR_LOGIN_WALL_MARKERS)


def _require_meaningful_content(text: str, raw_decoded: str) -> None:
    """Fail-closed content gate. Two independent signals, checked against the
    RAW decoded body (not the extracted text - _TextExtractingParser skips
    <noscript> along with <script>/<style>, so a "please enable JavaScript"
    fallback message would never even reach `text`):

    1. Explicit bot-check/login-wall/JS-required marker text, anywhere in the
       raw HTML - reported regardless of how much text was extracted.
    2. A large raw HTML document (Stage 3.3: confirmed empirically against
       real vk.ru/vk.com community pages, which ship a ~110KB client-side-
       rendered app shell with the actual content injected by JavaScript we
       deliberately never execute) that yields almost no visible text - not
       a coincidence, reported as a distinct, more specific reason than the
       generic "too short" case below.
    """
    raw_lower = raw_decoded.lower()
    if _looks_like_bot_or_login_wall(raw_lower):
        raise PublicSourceFetchError(
            "page appears to be a login wall or bot/verification check "
            "(detected an explicit marker in the page) - not usable content"
        )
    if len(text) < MIN_EXTRACTED_TEXT_CHARS:
        if len(raw_decoded) >= _LIKELY_JS_SHELL_MIN_HTML_CHARS:
            raise PublicSourceFetchError(
                "page appears to require JavaScript to render (large HTML "
                "document but almost no visible text could be extracted "
                "statically) - not usable content"
            )
        raise PublicSourceFetchError("extracted text is empty or too short to be useful")


def _vk_fallback_url(url: str) -> str | None:
    """Stage 3.3: narrow, single-hop fallback - returns the vk.com-equivalent
    of a vk.ru URL (same path/query/fragment), or None if `url`'s host is not
    EXACTLY vk.ru (never applied to any other domain, including vk.ru
    subdomains) or if the URL carries an explicit port/userinfo this simple
    host swap cannot safely represent (fail-closed: no fallback attempted,
    not a guess)."""
    parts = urlsplit(url)
    if (parts.hostname or "").lower() != _VK_FALLBACK_SOURCE_HOST:
        return None
    if parts.port is not None or "@" in parts.netloc:
        return None
    return urlunsplit((parts.scheme, _VK_FALLBACK_TARGET_HOST, parts.path, parts.query, parts.fragment))


def _fetch_and_extract(fetch_url: str, *, original_url: str) -> FetchedPublicSource:
    final_url, content_type, body = _fetch_with_redirects(fetch_url)
    normalized_content_type = _require_allowed_content_type(content_type)
    decoded = _decode_body(body, content_type)

    if normalized_content_type == "text/html":
        text, title = _extract_html(decoded)
    else:
        text, title = decoded.strip(), ""

    text = _collapse_whitespace(text)
    _require_meaningful_content(text, decoded)

    return FetchedPublicSource(
        url=original_url,
        final_url=final_url,
        title=title[:300],
        text=text[:MAX_EXTRACTED_TEXT_CHARS],
        content_type=normalized_content_type,
    )


def fetch_public_source_sync(url: str) -> FetchedPublicSource:
    """Blocking call - run via ``asyncio.to_thread`` from the executor, same
    convention as every other provider call in this codebase. Raises
    ``PublicSourceFetchError`` on any violation; never returns a partial/
    unsafe result.

    Stage 3.3: if the URL's host is exactly ``vk.ru`` and the fetch fails for
    any reason (HTTP error, network error, unusable content), makes exactly
    ONE additional attempt at the vk.com-equivalent URL before giving up -
    both URLs independently go through the full normal SSRF validation in
    ``_fetch_and_extract`` -> ``_fetch_with_redirects``. Every other host is
    unaffected - a single failed fetch simply fails, no retries, no loop.
    """
    if not isinstance(url, str) or not url.strip():
        raise PublicSourceFetchError("url must be a non-empty string")
    start_url = url.strip()

    try:
        return _fetch_and_extract(start_url, original_url=start_url)
    except PublicSourceFetchError:
        fallback_url = _vk_fallback_url(start_url)
        if fallback_url is None:
            raise
        log.info("planner_fetch: vk.ru fetch failed, trying single vk.com fallback")
        return _fetch_and_extract(fallback_url, original_url=start_url)
