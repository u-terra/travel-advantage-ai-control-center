"""XSS/security regression tests for the История (persistent server-side
conversation history) section added to app/templates/chat.html.

Two trust boundaries matter here:

- renderConversationsList(): conversation titles come from real backend
  records (derived from the user's own first message - see
  derive_conversation_title() in app.repositories.web_conversation_repository)
  and must never reach the DOM as parsed HTML, same rule as every other
  backend-driven section (see test_chat_html_five_sections_security.py).
- openConversation(): must restore old messages through the exact same
  addUser()/addAssistant() functions the live chat already uses - user
  content as data (textContent), assistant content as the server-rendered
  content_html (the same markdown.markdown() path /api/chat uses for a
  live answer - see _render_markdown() in app/web_api.py), never a new,
  separately-built innerHTML string.

No jsdom/npm toolchain in this repo, so these tests run the *actual*
shipped functions via a `node` subprocess. Skips cleanly when `node` is
missing.
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

_XSS_PAYLOAD = "<img src=x onerror=alert(1)><script>alert(2)</script>"


def _script_source() -> str:
    text = CHAT_HTML.read_text(encoding="utf-8")
    match = re.search(r"<script>(.*)</script>", text, re.S)
    assert match is not None, "expected an inline <script> block in chat.html"
    return match.group(1)


def _extract_function(source: str, name: str) -> str:
    for marker in (f"async function {name}(", f"function {name}("):
        if marker in source:
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
    raise AssertionError(f"function {name}() not found")


def _run_node(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=30,
        encoding="utf-8",
    )


_FAKE_DOM = """
class FakeElement {
    constructor(tag) {
        this.tagName = String(tag).toUpperCase();
        this.children = [];
        this.className = "";
        this._textContent = "";
        this.classList = { add() {}, remove() {}, toggle() {} };
    }
    set textContent(value) { this._textContent = value; }
    get textContent() { return this._textContent; }
    set innerHTML(value) { if (value === "") this.children = []; }
    appendChild(el) { this.children.push(el); return el; }
    addEventListener() {}
    remove() {}
}
class FakeTextNode {
    constructor(text) {
        this.tagName = "#text";
        this.className = "";
        this.children = [];
        this.textContent = text;
    }
}
const document = {
    createElement: (tag) => new FakeElement(tag),
    createTextNode: (text) => new FakeTextNode(text),
};
"""


def _collect_text(node_obj) -> list[str]:
    found = []
    text = node_obj.get("textContent", "")
    if text:
        found.append(text)
    for child in node_obj.get("children", []):
        found.extend(_collect_text(child))
    return found


def _flatten(node_obj):
    yield node_obj
    for child in node_obj.get("children", []):
        yield from _flatten(child)


def _serializable_script(node_expr: str) -> str:
    return f"""
function serialize(el) {{
    return {{
        tagName: el.tagName,
        className: el.className,
        textContent: el.textContent,
        children: el.children.map(serialize),
    }};
}}
console.log(JSON.stringify(serialize({node_expr})));
"""


# ── renderConversationsList: title/date text is data, never markup ──────────

def test_render_conversations_list_treats_title_as_data() -> None:
    source = _script_source()
    script = f"""
{_FAKE_DOM}
{_extract_function(source, "formatConversationDate")}
function openConversation() {{}}
const historyState = document.createElement("div");
{_extract_function(source, "renderConversationsList")}

renderConversationsList([
    {{ id: 1, title: {json.dumps(_XSS_PAYLOAD)}, created_at: "2026-01-01T00:00:00+00:00", updated_at: "2026-01-01T00:00:00+00:00" }},
]);
{_serializable_script("historyState")}
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    tree = json.loads(result.stdout.strip())

    all_text = _collect_text(tree)
    assert any(_XSS_PAYLOAD in item for item in all_text)
    tag_names = {child["tagName"] for child in _flatten(tree)}
    assert tag_names <= {"DIV", "BUTTON", "H2", "H3", "P", "#text"}


def test_render_conversations_list_never_assigns_innerhtml_with_content() -> None:
    source = _script_source()
    block = _extract_function(source, "renderConversationsList")

    assignments = re.findall(r"\.innerHTML\s*=\s*(.+?);", block)
    assert assignments, "expected the initial innerHTML clear"
    assert all(value == '""' for value in assignments), assignments


# ── openConversation: restored messages go through addUser()/addAssistant() ──

def test_open_conversation_restores_messages_through_safe_helpers() -> None:
    """User content must be passed to addUser() as a bare string (rendered
    via textContent there); assistant content must be passed to
    addAssistant(content_html, content) using the server-rendered HTML,
    exactly like a live answer - never a separately built innerHTML."""
    source = _script_source()
    script = f"""
{_FAKE_DOM}

const calls = {{ addUser: [], addAssistant: [], activateView: [] }};
function activateView(key) {{ calls.activateView.push(key); }}
function clearChatArea() {{}}
function scrollBottom() {{}}
function addUser(text) {{ calls.addUser.push(text); return document.createElement("div"); }}
function addAssistant(html, plainText) {{
    calls.addAssistant.push([html, plainText]);
    return document.createElement("div");
}}
const welcome = {{ style: {{}} }};
const message = {{ focus: () => {{}} }};
let currentConversationId = null;

const serverMessages = [
    {{ role: "user", content: {json.dumps(_XSS_PAYLOAD)} }},
    {{ role: "assistant", content: "plain text", content_html: "<strong>plain text</strong>" }},
];

global.fetch = async (url) => ({{
    ok: true,
    json: async () => ({{
        conversation: {{ id: 42, title: "t", created_at: "x", updated_at: "x" }},
        messages: serverMessages,
    }}),
}});

{_extract_function(source, "openConversation")}

openConversation({{ id: 42 }}).then(() => {{
    console.log(JSON.stringify({{ calls, currentConversationId }}));
}});
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())

    assert payload["calls"]["activateView"] == ["assistant"]
    assert payload["calls"]["addUser"] == [_XSS_PAYLOAD]
    assert payload["calls"]["addAssistant"][-1] == ["<strong>plain text</strong>", "plain text"]
    assert payload["currentConversationId"] == 42


def test_open_conversation_shows_error_bubble_on_failure_without_raw_html() -> None:
    source = _script_source()
    script = f"""
{_FAKE_DOM}

const calls = {{ addAssistant: [] }};
function activateView() {{}}
function clearChatArea() {{}}
function scrollBottom() {{}}
function addUser() {{}}
function addAssistant(html, plainText) {{
    calls.addAssistant.push([html, plainText]);
    return document.createElement("div");
}}
const welcome = {{ style: {{}} }};
const message = {{ focus: () => {{}} }};
let currentConversationId = null;

global.fetch = async () => ({{ ok: false, json: async () => ({{ error: "nope" }}) }});

{_extract_function(source, "openConversation")}

openConversation({{ id: 7 }}).then(() => {{
    console.log(JSON.stringify(calls));
}});
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())

    # error text passed as plainText (2nd arg), never as the html (1st) arg.
    assert all(entry[0] == "" for entry in payload["addAssistant"])
