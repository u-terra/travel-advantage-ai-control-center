"""UX fix: "Подготовить пост" on a signal used to jump straight to the
«Материалы» view and open the new draft, throwing the user off the signals
list they were reading. Success must now stay on the signals page - a
compact "Материал создан" confirmation with an explicit "Открыть материал"
action - and navigate to «Материалы» only on that click, never
automatically. An error must render in place too, without navigating.

No jsdom/npm toolchain in this repo, so this runs the *actual* shipped
createMaterialFromSignal()/renderMaterialCreatedConfirmation() via a `node`
subprocess, same pattern as the other tests/test_chat_html_*.py files.
Skips cleanly when `node` is missing.
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
    addEventListener(type, handler) { (this._handlers = this._handlers || {})[type] = handler; }
    click() { if (this._handlers && this._handlers.click) this._handlers.click(); }
    remove() {}
}
const document = {
    createElement: (tag) => new FakeElement(tag),
};
"""


def _serialize_script(node_expr: str) -> str:
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


def _flatten(node_obj):
    yield node_obj
    for child in node_obj.get("children", []):
        yield from _flatten(child)


# ── success: stays put, shows a compact confirmation, navigates only on click ──

def test_success_does_not_navigate_and_shows_open_material_action() -> None:
    source = _script_source()
    script = f"""
{_FAKE_DOM}

const calls = {{ activateView: [], openMaterial: [] }};
function activateView(key) {{ calls.activateView.push(key); }}
function openMaterial(material) {{ calls.openMaterial.push(material); }}

global.fetch = async () => ({{
    ok: true,
    json: async () => ({{ material: {{ id: 77, title: "Пост" }} }}),
}});

const button = document.createElement("button");
button.textContent = "Подготовить пост";
const errorEl = document.createElement("p");
const statusEl = document.createElement("div");

{_extract_function(source, "renderMaterialCreatedConfirmation")}
{_extract_function(source, "createMaterialFromSignal")}

createMaterialFromSignal({{ id: 5 }}, "post", button, errorEl, statusEl).then(() => {{
    console.log(JSON.stringify({{
        calls,
        buttonDisabled: button.disabled,
        errorText: errorEl.textContent,
        status: (() => {{
            function serialize(el) {{
                return {{ tagName: el.tagName, className: el.className, textContent: el.textContent, children: el.children.map(serialize) }};
            }}
            return serialize(statusEl);
        }})(),
    }}));
}});
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())

    # Never navigates automatically on success.
    assert payload["calls"]["activateView"] == []
    assert payload["calls"]["openMaterial"] == []
    assert payload["errorText"] == ""
    assert payload["buttonDisabled"] is False

    status_tree = payload["status"]
    all_text = [n["textContent"] for n in _flatten(status_tree) if n["textContent"]]
    assert any("Материал создан" in t for t in all_text)
    open_buttons = [n for n in _flatten(status_tree) if n["tagName"] == "BUTTON"]
    assert len(open_buttons) == 1
    assert "Открыть материал" in open_buttons[0]["textContent"]


def test_open_material_button_navigates_only_when_clicked() -> None:
    source = _script_source()
    script = f"""
{_FAKE_DOM}

const calls = {{ activateView: [], openMaterial: [] }};
function activateView(key) {{ calls.activateView.push(key); }}
function openMaterial(material) {{ calls.openMaterial.push(material); }}

const statusEl = document.createElement("div");
const material = {{ id: 77, title: "Пост" }};

{_extract_function(source, "renderMaterialCreatedConfirmation")}

renderMaterialCreatedConfirmation(statusEl, material);
const button = statusEl.children.find(c => c.tagName === "BUTTON");
console.log(JSON.stringify({{ before: calls }}));
button.click();
console.log(JSON.stringify({{ after: calls }}));
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    lines = [json.loads(line) for line in result.stdout.strip().splitlines()]
    before, after = lines[0]["before"], lines[1]["after"]

    assert before["activateView"] == [] and before["openMaterial"] == []
    assert after["activateView"] == ["materials"]
    assert after["openMaterial"] == [{"id": 77, "title": "Пост"}]


# ── error: rendered in place, no navigation, no confirmation shown ──────

def test_error_renders_in_place_without_navigating() -> None:
    source = _script_source()
    script = f"""
{_FAKE_DOM}

const calls = {{ activateView: [], openMaterial: [] }};
function activateView(key) {{ calls.activateView.push(key); }}
function openMaterial(material) {{ calls.openMaterial.push(material); }}

global.fetch = async () => ({{
    ok: false,
    json: async () => ({{ error: "Недостаточно данных источника." }}),
}});

const button = document.createElement("button");
button.textContent = "Подготовить пост";
const errorEl = document.createElement("p");
const statusEl = document.createElement("div");

function renderMaterialCreatedConfirmation() {{ throw new Error("must not be called on error"); }}
{_extract_function(source, "createMaterialFromSignal")}

createMaterialFromSignal({{ id: 5 }}, "post", button, errorEl, statusEl).then(() => {{
    console.log(JSON.stringify({{
        calls, errorText: errorEl.textContent, buttonDisabled: button.disabled,
        buttonText: button.textContent,
    }}));
}});
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())

    assert payload["calls"]["activateView"] == []
    assert payload["calls"]["openMaterial"] == []
    assert payload["errorText"] == "Недостаточно данных источника."
    assert payload["buttonDisabled"] is False
    assert payload["buttonText"] == "Подготовить пост"
