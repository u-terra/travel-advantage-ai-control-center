from __future__ import annotations

import socket as _socket_module

import pytest

import app.planner.fetch as fetch_module
from app.planner.fetch import PublicSourceFetchError, fetch_public_source_sync

_REAL_GETADDRINFO = _socket_module.getaddrinfo


def _addrinfo(ip: str, port: int) -> list:
    return [
        (_socket_module.AF_INET, _socket_module.SOCK_STREAM, _socket_module.IPPROTO_TCP, "", (ip, port))
    ]


def _patch_public_dns(monkeypatch, *, hostname: str, ip: str = "93.184.216.34") -> None:
    """Resolves exactly `hostname` to `ip` (offline). Any other host (in
    particular an IP literal used as a redirect target) falls through to the
    real resolver, which parses IP literals without touching the network."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if isinstance(host, str) and host.lower() == hostname:
            return _addrinfo(ip, port)
        return _REAL_GETADDRINFO(host, port, *args, **kwargs)

    monkeypatch.setattr(fetch_module.socket, "getaddrinfo", fake_getaddrinfo)


def _patch_multi_host_dns(monkeypatch, host_to_ip: dict[str, str]) -> None:
    """Same idea as _patch_public_dns but for tests needing more than one
    resolvable hostname (e.g. vk.ru AND vk.com in the same test)."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        ip = host_to_ip.get(host.lower()) if isinstance(host, str) else None
        if ip is not None:
            return _addrinfo(ip, port)
        return _REAL_GETADDRINFO(host, port, *args, **kwargs)

    monkeypatch.setattr(fetch_module.socket, "getaddrinfo", fake_getaddrinfo)


class _FakeResponse:
    def __init__(self, *, headers: dict[str, str], body: bytes) -> None:
        self.headers = {key.lower(): value for key, value in headers.items()}
        self._body = body
        self._pos = 0

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False

    def getheaders(self):
        return list(self.headers.items())

    def read(self, n: int = 65536) -> bytes:
        if self._pos >= len(self._body):
            return b""
        chunk = self._body[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk


def _patch_open_returning(monkeypatch, response: _FakeResponse) -> list[int]:
    calls: list[int] = []

    def fake_open(req, *, timeout, pinned_ip=None):
        calls.append(1)
        return response

    monkeypatch.setattr(fetch_module, "_open", fake_open)
    return calls


# ── scheme / host / IP validation - no network involved ──────────────────


def test_rejects_non_http_scheme():
    with pytest.raises(PublicSourceFetchError, match="scheme"):
        fetch_public_source_sync("ftp://example.com/file")


def test_rejects_localhost():
    with pytest.raises(PublicSourceFetchError, match="localhost"):
        fetch_public_source_sync("http://localhost/admin")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.0.0.5/",
        "http://172.16.5.5/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0/",
    ],
)
def test_rejects_private_or_reserved_ip_literals(url):
    with pytest.raises(PublicSourceFetchError):
        fetch_public_source_sync(url)


def test_rejects_hostname_resolving_to_private_ip(monkeypatch):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        assert host == "internal.example.com"
        return _addrinfo("10.1.2.3", port)

    monkeypatch.setattr(fetch_module.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(PublicSourceFetchError, match="blocked address"):
        fetch_public_source_sync("http://internal.example.com/")


@pytest.mark.parametrize("bad_url", ["", "   "])
def test_rejects_empty_url(bad_url):
    with pytest.raises(PublicSourceFetchError):
        fetch_public_source_sync(bad_url)


# ── redirect handling ──────────────────────────────────────────────────────


def test_redirect_to_private_ip_is_blocked(monkeypatch):
    _patch_public_dns(monkeypatch, hostname="competitor.example.com")
    calls: list[int] = []

    def fake_open(req, *, timeout, pinned_ip=None):
        calls.append(1)
        raise fetch_module._RedirectCapture("http://169.254.169.254/secret")

    monkeypatch.setattr(fetch_module, "_open", fake_open)

    with pytest.raises(PublicSourceFetchError, match="blocked address"):
        fetch_public_source_sync("http://competitor.example.com/")
    assert len(calls) == 1  # the redirect target must be rejected before a second connection


def test_max_redirects_exceeded(monkeypatch):
    _patch_public_dns(monkeypatch, hostname="competitor.example.com")

    def fake_open(req, *, timeout, pinned_ip=None):
        raise fetch_module._RedirectCapture("http://competitor.example.com/")

    monkeypatch.setattr(fetch_module, "_open", fake_open)

    with pytest.raises(PublicSourceFetchError, match="too many redirects"):
        fetch_public_source_sync("http://competitor.example.com/")


def test_redirect_to_safe_host_is_followed(monkeypatch):
    _patch_public_dns(monkeypatch, hostname="competitor.example.com")
    html = (
        b"<html><head><title>Landing</title></head><body>"
        b"<p>Redirected competitor landing page with plenty of readable text content.</p>"
        b"</body></html>"
    )
    response = _FakeResponse(headers={"Content-Type": "text/html; charset=utf-8"}, body=html)
    calls: list[int] = []

    def fake_open(req, *, timeout, pinned_ip=None):
        calls.append(1)
        if len(calls) == 1:
            raise fetch_module._RedirectCapture("http://competitor.example.com/final")
        return response

    monkeypatch.setattr(fetch_module, "_open", fake_open)

    result = fetch_public_source_sync("http://competitor.example.com/")
    assert result.final_url == "http://competitor.example.com/final"
    assert "Redirected competitor landing page" in result.text
    assert len(calls) == 2


def test_redirect_hop_is_resolved_and_pinned_independently(monkeypatch):
    """Each hop must go through its own _validate_and_resolve - a redirect
    to a DIFFERENT safe hostname must be pinned to THAT hostname's own
    resolved address, not reuse the first hop's."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host == "first.example.com":
            return _addrinfo("93.184.216.34", port)
        if host == "second.example.com":
            return _addrinfo("93.184.216.50", port)
        return _REAL_GETADDRINFO(host, port, *args, **kwargs)

    monkeypatch.setattr(fetch_module.socket, "getaddrinfo", fake_getaddrinfo)

    html = (
        b"<html><body><p>Enough readable content to pass the minimum length "
        b"check for this particular redirect test case right here.</p></body></html>"
    )
    response = _FakeResponse(headers={"Content-Type": "text/html"}, body=html)
    pinned_ips_seen: list[str] = []

    def fake_open(req, *, timeout, pinned_ip=None):
        pinned_ips_seen.append(pinned_ip)
        if len(pinned_ips_seen) == 1:
            raise fetch_module._RedirectCapture("http://second.example.com/final")
        return response

    monkeypatch.setattr(fetch_module, "_open", fake_open)

    fetch_public_source_sync("http://first.example.com/")
    assert pinned_ips_seen == ["93.184.216.34", "93.184.216.50"]


# ── size / timeout / content-type controls ─────────────────────────────────


def test_oversized_response_rejected_via_content_length_header(monkeypatch):
    _patch_public_dns(monkeypatch, hostname="competitor.example.com")
    response = _FakeResponse(
        headers={
            "Content-Type": "text/plain",
            "Content-Length": str(fetch_module.MAX_RESPONSE_BYTES + 1),
        },
        body=b"irrelevant",
    )
    _patch_open_returning(monkeypatch, response)

    with pytest.raises(PublicSourceFetchError, match="exceeds"):
        fetch_public_source_sync("http://competitor.example.com/")


def test_oversized_response_rejected_via_actual_body_size(monkeypatch):
    _patch_public_dns(monkeypatch, hostname="competitor.example.com")
    oversized_body = b"a" * (fetch_module.MAX_RESPONSE_BYTES + 100)
    response = _FakeResponse(headers={"Content-Type": "text/plain"}, body=oversized_body)
    _patch_open_returning(monkeypatch, response)

    with pytest.raises(PublicSourceFetchError, match="exceeds"):
        fetch_public_source_sync("http://competitor.example.com/")


def test_timeout_is_reported_as_controlled_error(monkeypatch):
    _patch_public_dns(monkeypatch, hostname="competitor.example.com")

    def fake_open(req, *, timeout, pinned_ip=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(fetch_module, "_open", fake_open)

    with pytest.raises(PublicSourceFetchError, match="network error"):
        fetch_public_source_sync("http://competitor.example.com/")


def test_binary_content_type_is_rejected(monkeypatch):
    _patch_public_dns(monkeypatch, hostname="competitor.example.com")
    response = _FakeResponse(
        headers={"Content-Type": "application/octet-stream"},
        body=b"\x00\x01\x02binarydata",
    )
    _patch_open_returning(monkeypatch, response)

    with pytest.raises(PublicSourceFetchError, match="content type"):
        fetch_public_source_sync("http://competitor.example.com/")


# ── content extraction ──────────────────────────────────────────────────────


def test_html_text_extraction_strips_script_and_style(monkeypatch):
    _patch_public_dns(monkeypatch, hostname="competitor.example.com")
    html = (
        b"<html><head><title>Competitor Co</title>"
        b"<style>.a{color:red}</style></head><body>"
        b"<script>var evil = 1;</script>"
        b"<p>We offer the best travel deals in town with great customer service every day.</p>"
        b"</body></html>"
    )
    response = _FakeResponse(headers={"Content-Type": "text/html; charset=utf-8"}, body=html)
    _patch_open_returning(monkeypatch, response)

    result = fetch_public_source_sync("http://competitor.example.com/")
    assert result.title == "Competitor Co"
    assert "We offer the best travel deals" in result.text
    assert "evil" not in result.text
    assert "color:red" not in result.text
    assert result.content_type == "text/html"
    assert result.url == "http://competitor.example.com/"
    assert result.final_url == "http://competitor.example.com/"


def test_plain_text_content_is_returned_as_is(monkeypatch):
    _patch_public_dns(monkeypatch, hostname="competitor.example.com")
    body = "Plain text competitor content that is definitely longer than forty characters.".encode(
        "utf-8"
    )
    response = _FakeResponse(headers={"Content-Type": "text/plain; charset=utf-8"}, body=body)
    _patch_open_returning(monkeypatch, response)

    result = fetch_public_source_sync("http://competitor.example.com/")
    assert result.title == ""
    assert "Plain text competitor content" in result.text
    assert result.content_type == "text/plain"


def test_empty_extracted_content_is_a_controlled_error(monkeypatch):
    _patch_public_dns(monkeypatch, hostname="competitor.example.com")
    html = b"<html><body><p>Hi</p></body></html>"
    response = _FakeResponse(headers={"Content-Type": "text/html"}, body=html)
    _patch_open_returning(monkeypatch, response)

    with pytest.raises(PublicSourceFetchError, match="too short"):
        fetch_public_source_sync("http://competitor.example.com/")


def test_whitespace_only_content_is_a_controlled_error(monkeypatch):
    _patch_public_dns(monkeypatch, hostname="competitor.example.com")
    response = _FakeResponse(headers={"Content-Type": "text/plain"}, body=b"   \n\n   ")
    _patch_open_returning(monkeypatch, response)

    with pytest.raises(PublicSourceFetchError, match="too short"):
        fetch_public_source_sync("http://competitor.example.com/")


# ── pinned connection (no process-global monkeypatch) ───────────────────────
#
# Phase 2.1 hardening: DNS-rebinding protection moved from a process-wide
# socket.getaddrinfo monkeypatch (_PinnedDNS, removed) to a per-connection-
# instance override of http.client's own `_create_connection` seam - the
# same attribute http.client.HTTPConnection.__init__ documents as "stored as
# an instance variable to allow unit tests to replace it". Nothing here (or
# in app/planner/fetch.py) reassigns socket.getaddrinfo or
# socket.create_connection at module/process scope. The tests below DO use
# pytest's monkeypatch fixture to stub socket.create_connection for the
# DURATION OF A SINGLE TEST - that is a standard, auto-reverting, per-test
# double used to avoid opening a real socket, not a production-code
# monkeypatch; it is a completely different thing from what was removed.


def test_pinned_create_connection_dials_pinned_ip_not_original_host(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_create_connection(address, timeout=None, source_address=None):
        captured["address"] = address
        return "FAKE-SOCKET"

    monkeypatch.setattr(fetch_module.socket, "create_connection", fake_create_connection)

    connector = fetch_module._pinned_create_connection("93.184.216.34")
    result = connector(("realhost.example.com", 443), timeout=5, source_address=None)

    assert captured["address"] == ("93.184.216.34", 443)
    assert result == "FAKE-SOCKET"


def test_pinned_http_connection_connects_to_pinned_ip_keeps_real_host():
    import unittest.mock

    fake_socket = unittest.mock.MagicMock(name="fake_raw_socket")
    with unittest.mock.patch.object(
        fetch_module.socket, "create_connection", return_value=fake_socket,
    ) as fake:
        conn = fetch_module._PinnedHTTPConnection(
            "realhost.example.com", 80, pinned_ip="93.184.216.34", timeout=5,
        )
        conn.connect()

    fake.assert_called_once()
    (dialed_address, *_rest), _kwargs = fake.call_args
    assert dialed_address == ("93.184.216.34", 80)
    # self.host stays the real hostname - this is what the Host header (and,
    # for HTTPS, TLS SNI/certificate verification) is built from.
    assert conn.host == "realhost.example.com"
    assert conn.sock is fake_socket


def test_pinned_https_connection_uses_pinned_ip_for_socket_and_real_hostname_for_tls():
    """The core Phase 2.1 security property: the raw TCP connection targets
    the pre-validated IP, while TLS SNI/certificate hostname verification
    (via ssl.SSLContext.wrap_socket's server_hostname) targets the REAL
    hostname - never weakened, never pointed at the IP."""
    import unittest.mock

    captured: dict[str, Any] = {}
    fake_raw_socket = unittest.mock.MagicMock(name="fake_raw_socket")

    class _FakeContext:
        check_hostname = True
        verify_mode = "CERT_REQUIRED"

        def wrap_socket(self, sock, server_hostname=None):
            captured["server_hostname"] = server_hostname
            captured["wrapped_sock"] = sock
            return unittest.mock.MagicMock(name="fake_tls_socket")

    with unittest.mock.patch.object(
        fetch_module.socket, "create_connection", return_value=fake_raw_socket,
    ) as fake:
        conn = fetch_module._PinnedHTTPSConnection(
            "realhost.example.com", 443, pinned_ip="93.184.216.34", timeout=5,
            context=_FakeContext(),
        )
        conn.connect()

    (dialed_address, *_rest), _kwargs = fake.call_args
    assert dialed_address == ("93.184.216.34", 443)
    assert captured["server_hostname"] == "realhost.example.com"
    assert captured["wrapped_sock"] is fake_raw_socket


def test_fetch_module_never_reassigns_socket_getaddrinfo_or_create_connection():
    """Structural regression guard for the actual Phase 2.1 requirement: the
    IMPLEMENTATION must never monkeypatch a process-global socket function.
    (Test-time stubbing via pytest's monkeypatch fixture, used above and in
    _patch_public_dns, is unrelated - it is scoped to one test and reverted
    automatically; it is not code that ships or runs in production.)"""
    import inspect

    source = inspect.getsource(fetch_module)
    assert "socket.getaddrinfo =" not in source
    assert "socket.create_connection =" not in source


# ── Stage 3.2 hotfix: Python 3.12 HTTPSHandler compatibility ────────────────
#
# Production runs Python 3.12, where urllib.request.HTTPSHandler.__init__no
# longer sets self._check_hostname (only self._context, which now carries
# check_hostname itself) - Python 3.11 (this test suite's interpreter) still
# sets it to None. The original bug (referencing self._check_hostname in
# https_open) therefore passed on 3.11 and crashed with AttributeError on
# every real HTTPS fetch in production. The fix removed that reference
# entirely. The test below recreates the missing-attribute condition
# directly - by deleting _check_hostname if present - so it fails the same
# way production failed, regardless of which Python version runs this suite.


def test_https_open_does_not_depend_on_check_hostname_attribute(monkeypatch):
    """Direct regression test for the production AttributeError. Simulates
    Python 3.12's HTTPSHandler.__init__ (which never sets _check_hostname at
    all) by deleting the attribute if this interpreter happens to have set
    it, then calls the real https_open() and asserts it does not touch that
    attribute."""
    # No real network under any circumstance: fail the TCP dial immediately
    # and deterministically, rather than actually connecting to the pinned
    # (real, public) IP used in this test.
    def _refuse_connection(address, timeout=None, source_address=None):
        raise OSError("blocked for test - no real network calls allowed")

    monkeypatch.setattr(fetch_module.socket, "create_connection", _refuse_connection)

    handler = fetch_module._PinnedHTTPSHandler("93.184.216.34")
    if hasattr(handler, "_check_hostname"):
        delattr(handler, "_check_hostname")

    req = fetch_module.urllib.request.Request("https://competitor.example.com/")
    # Normally set by OpenerDirector.open() before dispatching to a handler -
    # set directly here since this test calls https_open() standalone.
    req.timeout = 5

    # The connection itself is refused above (OSError) before any real I/O -
    # the only thing under test is that building the call up to that point
    # never raises AttributeError on self._check_hostname.
    try:
        handler.https_open(req)
    except AttributeError as exc:
        if "_check_hostname" in str(exc):
            raise AssertionError(
                "https_open still depends on self._check_hostname - the "
                "exact attribute Python 3.12's HTTPSHandler.__init__ does "
                "not set"
            ) from exc
        raise
    except Exception:
        pass  # any non-AttributeError failure here is unrelated to this test


class _FakeTransportSocket:
    """Minimal socket-like object satisfying http.client's needs: connect
    creation, TCP_NODELAY setsockopt, sendall/send for the request, and
    makefile('rb') for reading a canned raw HTTP response back."""

    def __init__(self, response_bytes: bytes) -> None:
        self._response_bytes = response_bytes
        self.sent = b""

    def makefile(self, mode="r", *args, **kwargs):
        import io

        if "r" in mode:
            return io.BytesIO(self._response_bytes)
        return io.BytesIO()

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def send(self, data: bytes) -> int:
        self.sent += data
        return len(data)

    def close(self) -> None:
        pass

    def settimeout(self, *_a, **_kw) -> None:
        pass

    def setsockopt(self, *_a, **_kw) -> None:
        pass

    def fileno(self) -> int:
        return -1


def _canned_http_response(body: bytes, *, content_type: str = "text/html; charset=utf-8") -> bytes:
    return (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: " + content_type.encode() + b"\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n"
        b"\r\n" + body
    )


def test_real_transport_path_through_build_opener_and_https_handler(monkeypatch):
    """Exercises the REAL chain: fetch_public_source_sync -> _fetch_with_redirects
    -> _open -> urllib.request.build_opener -> _PinnedHTTPSHandler.https_open
    -> do_open -> _PinnedHTTPSConnection construction and .connect(). Unlike
    every other test in this file, _open() itself is NOT mocked - only the
    lowest-level socket primitives (socket.create_connection and
    ssl.SSLContext.wrap_socket) are, so this is the test that would have
    caught the Python 3.12 AttributeError before it reached production."""
    import ssl

    _patch_public_dns(monkeypatch, hostname="competitor.example.com")

    body = (
        b"<html><head><title>Real Transport</title></head><body>"
        b"<p>Real transport path integration test content, long enough to pass the check.</p>"
        b"</body></html>"
    )
    fake_socket = _FakeTransportSocket(_canned_http_response(body))
    captured: dict[str, object] = {}

    def fake_create_connection(address, timeout=None, source_address=None):
        captured["address"] = address
        return fake_socket

    def fake_wrap_socket(self, sock, server_hostname=None, **kwargs):
        captured["server_hostname"] = server_hostname
        captured["wrapped_is_pinned_socket"] = sock is fake_socket
        return fake_socket

    monkeypatch.setattr(fetch_module.socket, "create_connection", fake_create_connection)
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", fake_wrap_socket)

    result = fetch_public_source_sync("https://competitor.example.com/")

    # TCP connected to the pre-validated pinned IP, not the hostname.
    assert captured["address"] == ("93.184.216.34", 443)
    # TLS SNI/hostname verification still targets the real hostname.
    assert captured["server_hostname"] == "competitor.example.com"
    assert captured["wrapped_is_pinned_socket"] is True
    assert result.title == "Real Transport"
    assert "Real transport path integration test content" in result.text


def test_real_transport_path_plain_http_through_build_opener(monkeypatch):
    """Same real-chain proof for plain HTTP (_PinnedHTTPHandler.http_open),
    which was never affected by the check_hostname bug but had equally never
    been exercised end to end through build_opener either."""
    _patch_public_dns(monkeypatch, hostname="competitor.example.com")

    body = b"Plain HTTP transport path integration test content, long enough."
    fake_socket = _FakeTransportSocket(_canned_http_response(body, content_type="text/plain"))
    captured: dict[str, object] = {}

    def fake_create_connection(address, timeout=None, source_address=None):
        captured["address"] = address
        return fake_socket

    monkeypatch.setattr(fetch_module.socket, "create_connection", fake_create_connection)

    result = fetch_public_source_sync("http://competitor.example.com/")

    assert captured["address"] == ("93.184.216.34", 80)
    assert "Plain HTTP transport path integration test content" in result.text


# ── Stage 3.3: browser-like headers ─────────────────────────────────────────


def test_request_sends_browser_like_headers(monkeypatch):
    _patch_public_dns(monkeypatch, hostname="competitor.example.com")
    captured: dict[str, object] = {}
    html = b"<html><body><p>Enough content here to pass the minimum length check for this test.</p></body></html>"
    response = _FakeResponse(headers={"Content-Type": "text/html"}, body=html)

    def fake_open(req, *, timeout, pinned_ip=None):
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        return response

    monkeypatch.setattr(fetch_module, "_open", fake_open)

    fetch_public_source_sync("http://competitor.example.com/")

    headers = captured["headers"]
    assert "chrome" in headers["user-agent"].lower()
    assert "mozilla" in headers["user-agent"].lower()
    assert "text/html" in headers["accept"]
    assert headers["accept-language"].startswith("ru-RU")
    # never fake auth/session state
    assert "cookie" not in headers
    assert "authorization" not in headers


# ── Stage 3.3: vk.ru -> vk.com single fallback ──────────────────────────────


def test_vk_fallback_url_swaps_host_only_preserves_path_query_fragment():
    assert fetch_module._vk_fallback_url("https://vk.ru/progulkipovolge") == "https://vk.com/progulkipovolge"
    assert (
        fetch_module._vk_fallback_url("https://vk.ru/a/b?x=1#frag")
        == "https://vk.com/a/b?x=1#frag"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://vk.com/progulkipovolge",
        "https://example.com/",
        "https://sub.vk.ru/x",
        "https://VK.RU.evil.example.com/x",
    ],
)
def test_vk_fallback_url_is_none_for_non_vk_ru_hosts(url):
    assert fetch_module._vk_fallback_url(url) is None


@pytest.mark.parametrize(
    "url", ["https://vk.ru:8443/x", "https://user@vk.ru/x"],
)
def test_vk_fallback_url_is_none_for_port_or_userinfo(url):
    assert fetch_module._vk_fallback_url(url) is None


def _vk_html(marker: str) -> bytes:
    return (
        f"<html><head><title>{marker}</title></head><body>"
        f"<p>Real community content for {marker}, long enough to pass the minimum length check.</p>"
        "</body></html>"
    ).encode()


def test_vk_fallback_used_when_vk_ru_fails(monkeypatch):
    _patch_multi_host_dns(monkeypatch, {"vk.ru": "87.240.132.67", "vk.com": "87.240.132.67"})
    good_response = _FakeResponse(headers={"Content-Type": "text/html"}, body=_vk_html("vk.com page"))
    calls: list[str] = []

    def fake_open(req, *, timeout, pinned_ip=None):
        calls.append(req.full_url)
        if "vk.ru" in req.full_url:
            raise fetch_module.urllib.error.HTTPError(req.full_url, 404, "Not Found", None, None)
        return good_response

    monkeypatch.setattr(fetch_module, "_open", fake_open)

    result = fetch_public_source_sync("https://vk.ru/progulkipovolge")

    assert calls == ["https://vk.ru/progulkipovolge", "https://vk.com/progulkipovolge"]
    assert result.url == "https://vk.ru/progulkipovolge"  # original_url preserved
    assert result.final_url == "https://vk.com/progulkipovolge"  # what actually served content
    assert "Real community content for vk.com page" in result.text


def test_no_vk_fallback_when_primary_succeeds(monkeypatch):
    _patch_public_dns(monkeypatch, hostname="vk.ru", ip="87.240.132.67")
    good_response = _FakeResponse(headers={"Content-Type": "text/html"}, body=_vk_html("vk.ru page"))
    calls: list[str] = []

    def fake_open(req, *, timeout, pinned_ip=None):
        calls.append(req.full_url)
        return good_response

    monkeypatch.setattr(fetch_module, "_open", fake_open)

    result = fetch_public_source_sync("https://vk.ru/progulkipovolge")

    assert calls == ["https://vk.ru/progulkipovolge"]  # no fallback attempted
    assert result.final_url == "https://vk.ru/progulkipovolge"


def test_vk_fallback_is_attempted_at_most_once_when_both_fail(monkeypatch):
    _patch_multi_host_dns(monkeypatch, {"vk.ru": "87.240.132.67", "vk.com": "87.240.132.78"})
    calls: list[str] = []

    def fake_open(req, *, timeout, pinned_ip=None):
        calls.append(req.full_url)
        raise fetch_module.urllib.error.HTTPError(req.full_url, 404, "Not Found", None, None)

    monkeypatch.setattr(fetch_module, "_open", fake_open)

    with pytest.raises(PublicSourceFetchError, match="404"):
        fetch_public_source_sync("https://vk.ru/progulkipovolge")

    # exactly two attempts total - vk.ru, then vk.com once - never a loop
    assert calls == ["https://vk.ru/progulkipovolge", "https://vk.com/progulkipovolge"]


def test_vk_fallback_not_applied_to_other_domains(monkeypatch):
    _patch_public_dns(monkeypatch, hostname="competitor.example.com")
    calls: list[str] = []

    def fake_open(req, *, timeout, pinned_ip=None):
        calls.append(req.full_url)
        raise fetch_module.urllib.error.HTTPError(req.full_url, 404, "Not Found", None, None)

    monkeypatch.setattr(fetch_module, "_open", fake_open)

    with pytest.raises(PublicSourceFetchError, match="404"):
        fetch_public_source_sync("http://competitor.example.com/")

    assert len(calls) == 1  # no VK-style fallback for a non-vk.ru host


# ── Stage 3.3: login-wall / bot-check / JS-shell detection ──────────────────


def test_login_wall_marker_is_a_controlled_error_regardless_of_length(monkeypatch):
    _patch_public_dns(monkeypatch, hostname="competitor.example.com")
    html = (
        b"<html><body><p>Please sign in. Enable JavaScript to use this site "
        b"properly and access all of its features on every page.</p></body></html>"
    )
    response = _FakeResponse(headers={"Content-Type": "text/html"}, body=html)
    _patch_open_returning(monkeypatch, response)

    with pytest.raises(PublicSourceFetchError, match="login wall|bot"):
        fetch_public_source_sync("http://competitor.example.com/")


def test_large_html_with_almost_no_text_is_detected_as_js_shell(monkeypatch):
    """Reproduces the real vk.ru/vk.com pattern found in Stage 3.3
    diagnostics: a large client-side-rendered app shell (big <script>
    payload) with essentially no server-rendered visible text."""
    _patch_public_dns(monkeypatch, hostname="competitor.example.com")
    filler_script = b"<script>" + b"var x = 1;" * 600 + b"</script>"  # > 5000 chars, all skipped as script
    html = b"<html><head>" + filler_script + b"</head><body></body></html>"
    response = _FakeResponse(headers={"Content-Type": "text/html"}, body=html)
    _patch_open_returning(monkeypatch, response)

    with pytest.raises(PublicSourceFetchError, match="JavaScript"):
        fetch_public_source_sync("http://competitor.example.com/")


def test_small_genuinely_short_page_still_uses_generic_message(monkeypatch):
    """Regression: an ordinary small page with too little text must keep
    getting the generic (pre-Stage-3.3) message, not be misreported as a
    JS-shell just because it is short."""
    _patch_public_dns(monkeypatch, hostname="competitor.example.com")
    html = b"<html><body><p>Hi</p></body></html>"
    response = _FakeResponse(headers={"Content-Type": "text/html"}, body=html)
    _patch_open_returning(monkeypatch, response)

    with pytest.raises(PublicSourceFetchError, match="too short"):
        fetch_public_source_sync("http://competitor.example.com/")


def test_html_extraction_still_works_for_a_normal_content_page(monkeypatch):
    """Stage 3.3 must not regress the ordinary successful-extraction path."""
    _patch_public_dns(monkeypatch, hostname="competitor.example.com")
    html = (
        b"<html><head><title>Normal Page</title></head><body>"
        b"<script>var tracking = 1;</script>"
        b"<p>This is perfectly ordinary, statically rendered page content, long enough.</p>"
        b"</body></html>"
    )
    response = _FakeResponse(headers={"Content-Type": "text/html"}, body=html)
    _patch_open_returning(monkeypatch, response)

    result = fetch_public_source_sync("http://competitor.example.com/")

    assert result.title == "Normal Page"
    assert "perfectly ordinary" in result.text
    assert "tracking" not in result.text
