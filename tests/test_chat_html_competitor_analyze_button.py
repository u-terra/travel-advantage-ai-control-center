"""Regression test for the one-click "Анализировать"/"Обновить анализ"
button in the Web "Мои конкуренты" view (app/templates/chat.html).

Previously this button switched the user to the Assistant view and
pre-filled a chat prompt ("Проанализируй конкурента ...") they still had to
send themselves. It must now POST directly to
/api/competitors/{id}/analyze and never touch the Assistant view/message
box. Same node-subprocess technique as
tests/test_chat_html_competitor_report_security.py - there is no jsdom/JS
test toolchain in this repo, so the *actual* shipped `analyzeCompetitor`
function is executed, not a Python reimplementation of it. Skips cleanly
(not failing the suite) when `node` isn't on PATH.
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
    marker = f"function {name}("
    start = source.index(marker)
    # analyzeCompetitor is declared as `async function analyzeCompetitor(...)`
    # - include the `async ` prefix so the extracted text is runnable as-is.
    if source[:start].endswith("async "):
        start -= len("async ")
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


# analyzeCompetitor is a free-standing async function that only reads
# `item`/`button`/`errorEl`/`idleLabel` parameters plus the global `fetch`
# and `loadCompetitors` - stubbing those two globals is enough to execute
# the real function without a DOM.
def _harness(fetch_impl: str) -> str:
    source = _script_source()
    analyze_fn = _extract_function(source, "analyzeCompetitor")
    return f"""
let loadCalled = false;
function loadCompetitors() {{ loadCalled = true; }}
{fetch_impl}

{analyze_fn}

const item = {{ id: 42, label: "Trip.com", url: "https://trip.com", domain: "trip.com" }};
const button = {{ disabled: false, textContent: "Анализировать" }};
const errorEl = {{ textContent: "" }};

analyzeCompetitor(item, button, errorEl, "Анализировать").then(() => {{
    console.log(JSON.stringify({{
        loadCalled, button, errorEl,
        requestUrl: globalThis.__requestUrl || null,
        requestMethod: globalThis.__requestMethod || null,
        buttonTextDuringCall: globalThis.__buttonTextDuringCall || null,
    }}));
}}).catch((err) => {{
    console.error(String(err));
    process.exit(1);
}});
"""


def test_analyze_button_posts_directly_to_the_analyze_endpoint() -> None:
    """The button's handler must call the API itself - not switch views or
    pre-fill a chat message for the user to send."""
    source = _script_source()
    analyze_fn = _extract_function(source, "analyzeCompetitor")

    # Structural guard against the old prefill-Assistant behaviour coming
    # back: this function must never reference the Assistant view/message
    # box or the old hand-off prompt text.
    assert "activateView" not in analyze_fn
    assert "Проанализируй конкурента" not in analyze_fn
    assert "/analyze" in analyze_fn

    fetch_impl = """
globalThis.fetch = async (url, options) => {
    globalThis.__requestUrl = url;
    globalThis.__requestMethod = (options && options.method) || "GET";
    globalThis.__buttonTextDuringCall = button.textContent;
    return {
        ok: true,
        json: async () => ({
            competitor: { id: 42, last_analyzed_at: "2026-02-01T00:00:00+00:00" },
            intelligence: { analyzed_at: "2026-02-01T00:00:00+00:00" },
        }),
    };
};
"""
    result = _run_node(_harness(fetch_impl))
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())

    assert payload["requestUrl"] == "/api/competitors/42/analyze"
    assert payload["requestMethod"] == "POST"
    # It re-fetches the list to refresh the card (last_analyzed_at, report
    # button) instead of building its own separate render path.
    assert payload["loadCalled"] is True


def test_analyze_button_shows_analyzing_state_and_disables_itself() -> None:
    fetch_impl = """
globalThis.fetch = async (url, options) => {
    globalThis.__buttonTextDuringCall = button.textContent;
    globalThis.__buttonDisabledDuringCall = button.disabled;
    return {
        ok: true,
        json: async () => ({
            competitor: { id: 42, last_analyzed_at: "2026-02-01T00:00:00+00:00" },
            intelligence: { analyzed_at: "2026-02-01T00:00:00+00:00" },
        }),
    };
};
"""
    script = _harness(fetch_impl).replace(
        'buttonTextDuringCall: globalThis.__buttonTextDuringCall || null,',
        'buttonTextDuringCall: globalThis.__buttonTextDuringCall || null,'
        ' buttonDisabledDuringCall: globalThis.__buttonDisabledDuringCall || null,',
    )
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())

    assert payload["buttonTextDuringCall"] == "Анализирую…"
    assert payload["buttonDisabledDuringCall"] is True


def test_analyze_button_error_is_shown_in_place_without_leaving_the_view() -> None:
    """On failure the error must render on the card itself (errorEl) - the
    button must re-enable, and there is no navigation to the Assistant view
    anywhere in this code path (see the structural assertion above)."""
    fetch_impl = """
globalThis.fetch = async (url, options) => {
    return {
        ok: true,
        json: async () => ({ error: "Свежие источники не найдены." }),
    };
};
"""
    result = _run_node(_harness(fetch_impl))
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())

    assert payload["errorEl"]["textContent"] == "Свежие источники не найдены."
    assert payload["button"]["disabled"] is False
    assert payload["button"]["textContent"] == "Анализировать"
    # The card was NOT refreshed on failure - stale local state stays as-is.
    assert payload["loadCalled"] is False
