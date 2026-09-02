"""Security regression tests for the Competitor Intelligence report view in
app/templates/chat.html.

Report payload (positioning, products, sources, opportunities, ...) comes
from data fetched off competitors' public pages - it is untrusted content
and must never reach the DOM as parsed HTML. There is no existing JS test
toolchain in this repo (no package.json/jsdom), so these tests exercise the
*actual* shipped functions via a real `node` subprocess instead of a Python
reimplementation, which would only prove the reimplementation is correct.
Skips cleanly (not failing the suite) when `node` isn't on PATH.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

CHAT_HTML = Path(__file__).resolve().parent.parent / "app" / "templates" / "chat.html"

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node is not available on PATH")


def _script_source() -> str:
    text = CHAT_HTML.read_text(encoding="utf-8")
    match = re.search(r"<script>(.*)</script>", text, re.S)
    assert match is not None, "expected an inline <script> block in chat.html"
    return match.group(1)


def _extract_function(source: str, name: str) -> str:
    """Grabs `function <name>(...) { ... }` by brace counting - regexes
    alone can't reliably find a matching closing brace."""
    marker = f"function {name}("
    start = source.index(marker)
    brace_start = source.index("{", start)
    depth = 0
    for index in range(brace_start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"unbalanced braces while extracting {name}()")


def _run_node(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=30,
        encoding="utf-8",
    )


# ── safeHttpUrl: only http:/https: absolute URLs are ever accepted ──────────

def test_safe_http_url_rejects_dangerous_schemes_and_accepts_http_https() -> None:
    source = _script_source()
    safe_http_url = _extract_function(source, "safeHttpUrl")

    cases = [
        ("http://example.com/a", "http://example.com/a"),
        ("https://example.com/a?x=1", "https://example.com/a?x=1"),
        ("javascript:alert(1)", None),
        ("JavaScript:alert(document.cookie)", None),
        ("data:text/html,<script>alert(1)</script>", None),
        ("vbscript:msgbox(1)", None),
        ("file:///etc/passwd", None),
        ("not a url", None),
        ("", None),
        ("   ", None),
        ("ftp://example.com/file", None),
    ]

    script = f"""
{safe_http_url}
const cases = {json.dumps(cases)};
const failures = [];
for (const [input, expected] of cases) {{
    const result = safeHttpUrl(input);
    if (result !== expected) {{
        failures.push(`safeHttpUrl(${{JSON.stringify(input)}}) => ${{JSON.stringify(result)}}, expected ${{JSON.stringify(expected)}}`);
    }}
}}
if (failures.length) {{
    console.error(failures.join("\\n"));
    process.exit(1);
}}
console.log("OK");
"""

    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"


# ── appendSafeLinkOrText: dangerous URLs never become <a href>, and any ─────
# ── HTML-looking payload text is stored as literal textContent, never ───────
# ── parsed as markup. ────────────────────────────────────────────────────────

_FAKE_DOM = """
class FakeElement {
    constructor(tag) {
        this.tagName = String(tag).toUpperCase();
        this.children = [];
        this.href = undefined;
        this._textContent = "";
    }
    set textContent(value) { this._textContent = value; }
    get textContent() { return this._textContent; }
    appendChild(el) { this.children.push(el); return el; }
}
const document = { createElement: (tag) => new FakeElement(tag) };
"""


def test_append_safe_link_or_text_blocks_dangerous_schemes() -> None:
    source = _script_source()
    script_fns = "\n".join([
        _extract_function(source, "safeHttpUrl"),
        _extract_function(source, "appendSafeLinkOrText"),
    ])

    payload_label = "<img src=x onerror=alert(1)>"
    script = f"""
{_FAKE_DOM}
{script_fns}

const container = document.createElement("div");
appendSafeLinkOrText(container, "javascript:alert(document.cookie)", {json.dumps(payload_label)});

const child = container.children[0];
const result = {{
    tagName: child.tagName,
    href: child.href === undefined ? null : child.href,
    textContent: child.textContent,
}};
console.log(JSON.stringify(result));
"""

    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())

    # No anchor is created for a dangerous scheme - a plain <span> instead.
    assert payload["tagName"] == "SPAN"
    assert payload["href"] is None
    # The HTML-looking label is stored as inert text, not parsed markup.
    assert payload["textContent"] == payload_label


def test_append_safe_link_or_text_allows_https_and_preserves_text_verbatim() -> None:
    source = _script_source()
    script_fns = "\n".join([
        _extract_function(source, "safeHttpUrl"),
        _extract_function(source, "appendSafeLinkOrText"),
    ])

    payload_label = "Trip.com Blog <script>alert(1)</script>"
    script = f"""
{_FAKE_DOM}
{script_fns}

const container = document.createElement("div");
appendSafeLinkOrText(container, "https://trip.com/blog", {json.dumps(payload_label)});

const child = container.children[0];
const result = {{
    tagName: child.tagName,
    href: child.href,
    textContent: child.textContent,
}};
console.log(JSON.stringify(result));
"""

    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())

    assert payload["tagName"] == "A"
    assert payload["href"] == "https://trip.com/blog"
    # Still the raw label, never interpreted as HTML.
    assert payload["textContent"] == payload_label


# ── Static regressions: no re-introduced innerHTML injection point, and no ──
# ── hardcoded competitor id on the "Открыть отчёт" wiring. ──────────────────

def test_competitors_state_innerhtml_is_only_ever_cleared_not_interpolated() -> None:
    """Every `competitorsState.innerHTML = ...` in the competitors/report
    code must clear to a literal empty string - never receive interpolated
    payload text. If a future change routes untrusted data through
    innerHTML, this fails instead of silently reintroducing XSS."""
    source = _script_source()
    assignments = re.findall(r"competitorsState\.innerHTML\s*=\s*(.+?);", source)
    assert assignments, "expected at least one competitorsState.innerHTML assignment"
    assert all(value == '""' for value in assignments), assignments


def test_report_links_only_assigned_through_safe_url_helper() -> None:
    """appendReportSourcesSection/appendReportOpportunitiesSection must not
    assign `.href` directly from payload data (source.final_url,
    opp.source_url, ...) - only appendSafeLinkOrText() is allowed to touch
    `.href`, after running the URL through safeHttpUrl()."""
    source = _script_source()
    sources_section = _extract_function(source, "appendReportSourcesSection")
    opportunities_section = _extract_function(source, "appendReportOpportunitiesSection")

    direct_href_assignments = re.findall(
        r"\.href\s*=", sources_section + opportunities_section,
    )
    assert direct_href_assignments == [], direct_href_assignments
    assert "appendSafeLinkOrText(" in sources_section
    assert "appendSafeLinkOrText(" in opportunities_section


def test_open_report_button_uses_item_id_not_a_hardcoded_competitor_id() -> None:
    """'Открыть отчёт' must call openCompetitorReport(item) using the id
    that came back from GET /api/competitors, never a hardcoded literal
    (production Trip.com is id=3, not id=1)."""
    source = _script_source()

    assert "openReportBtn.addEventListener(\"click\", () => openCompetitorReport(item));" in source
    assert '"/api/competitors/" + encodeURIComponent(item.id) + "/intelligence"' in source

    # No literal competitor id anywhere in a competitors API path.
    assert not re.search(r"/api/competitors/\d+", source)
