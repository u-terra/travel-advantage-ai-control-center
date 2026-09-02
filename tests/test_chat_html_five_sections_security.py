"""XSS/security regression tests for the 5 newly wired web-shell sections
(База знаний, Материалы, История/Артефакты, Профиль, Настройки) in
app/templates/chat.html.

All data rendered by these sections comes from real backend records
(knowledge items, saved artifact content, business profile fields,
usage-ledger events) - i.e. external/user-controlled text, not something
this app authored. It must never reach the DOM as parsed HTML.

As with the report/competitor/signals sections, there's no jsdom/npm
toolchain in this repo, so these tests run the *actual* shipped functions
via a `node` subprocess instead of a Python reimplementation. Skips
cleanly when `node` is missing.
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

_FIVE_SECTIONS_START = "/* ---------- generic loading/empty/error"
_FIVE_SECTIONS_END = 'document.querySelectorAll(".quick-card")'


def _script_source() -> str:
    text = CHAT_HTML.read_text(encoding="utf-8")
    match = re.search(r"<script>(.*)</script>", text, re.S)
    assert match is not None, "expected an inline <script> block in chat.html"
    return match.group(1)


def _extract_function(source: str, name: str) -> str:
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


def _extract_const(source: str, name: str) -> str:
    marker = f"const {name} ="
    start = source.index(marker)
    end = source.index(";\n", start)
    return source[start:end + 1]


def _five_sections_block(source: str) -> str:
    start = source.index(_FIVE_SECTIONS_START)
    end = source.index(_FIVE_SECTIONS_END, start)
    return source[start:end]


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
    }
    set textContent(value) { this._textContent = value; }
    get textContent() { return this._textContent; }
    appendChild(el) { this.children.push(el); return el; }
    addEventListener() {}
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
// referenced inside makeMaterialsBackButton()'s click handler closure but
// never invoked in these tests - a no-op stand-in is enough.
function loadMaterials() {}
"""


def _collect_text(node_obj) -> list[str]:
    """Depth-first collection of every non-empty textContent in the tree -
    used to prove a payload is present verbatim as data, never as markup."""
    found = []
    text = node_obj.get("textContent", "")
    if text:
        found.append(text)
    for child in node_obj.get("children", []):
        found.extend(_collect_text(child))
    return found


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


# ── static regressions: no innerHTML interpolation, no href assignment ──────

def test_five_sections_innerhtml_is_only_ever_cleared_not_interpolated() -> None:
    source = _script_source()
    block = _five_sections_block(source)

    assignments = re.findall(r"\.innerHTML\s*=\s*(.+?);", block)
    assert assignments, "expected at least one innerHTML clear in this block"
    assert all(value == '""' for value in assignments), assignments


def test_five_sections_never_assign_href() -> None:
    """None of these 5 sections render any link - if that ever changes, the
    new code must go through the existing safeHttpUrl()/
    appendSafeLinkOrText() helpers, not a raw `.href = ...` assignment."""
    source = _script_source()
    block = _five_sections_block(source)

    assert ".href" not in block


# ── renderKnowledge: item/source text is data, never markup ─────────────────

def test_render_knowledge_treats_item_and_source_text_as_data() -> None:
    source = _script_source()
    script = f"""
{_FAKE_DOM}
{_extract_const(source, "KNOWLEDGE_VERIFICATION_LABELS")}
{_extract_function(source, "knowledgeVerificationLabel")}
const knowledgeState = document.createElement("div");
{_extract_function(source, "renderKnowledge")}

renderKnowledge(
    [{{ id: 1, title: {json.dumps(_XSS_PAYLOAD)}, source_type: "x", source_name: "y", verification_status: "verified_official", version: null }}],
    [{{ stable_key: "k", category: {json.dumps(_XSS_PAYLOAD)}, title: {json.dumps(_XSS_PAYLOAD)}, content: {json.dumps(_XSS_PAYLOAD)}, tags: [], source_title: {json.dumps(_XSS_PAYLOAD)} }}],
);
{_serializable_script("knowledgeState")}
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    tree = json.loads(result.stdout.strip())

    all_text = _collect_text(tree)
    assert any(_XSS_PAYLOAD in item for item in all_text)
    # every element is a plain, safe tag - never one materialized from the payload
    tag_names = {child["tagName"] for child in _flatten(tree)}
    assert tag_names <= {"DIV", "BUTTON", "H2", "H3", "UL", "LI", "P", "#text"}


def _flatten(node_obj):
    yield node_obj
    for child in node_obj.get("children", []):
        yield from _flatten(child)


# ── renderMaterialDetail: artifact version content is data, never markup ────

def test_render_material_detail_treats_version_content_as_data() -> None:
    source = _script_source()
    script = f"""
{_FAKE_DOM}
{_extract_const(source, "MATERIAL_TYPE_LABELS")}
{_extract_const(source, "MATERIAL_STATUS_LABELS")}
{_extract_function(source, "materialTypeLabel")}
{_extract_function(source, "materialStatusLabel")}
{_extract_function(source, "formatSignalDate")}
{_extract_function(source, "makeMaterialsBackButton")}
const materialsState = document.createElement("div");
{_extract_function(source, "renderMaterialDetail")}

renderMaterialDetail(
    {{ title: {json.dumps(_XSS_PAYLOAD)}, artifact_type: "post", status: "draft", created_at: "2026-01-01T00:00:00+00:00", updated_at: "2026-01-01T00:00:00+00:00" }},
    {{ version_number: 1, content: {json.dumps(_XSS_PAYLOAD)}, generation_note: null, created_at: "2026-01-01T00:00:00+00:00" }},
);
{_serializable_script("materialsState")}
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    tree = json.loads(result.stdout.strip())

    all_text = _collect_text(tree)
    assert any(_XSS_PAYLOAD in item for item in all_text)
    tag_names = {child["tagName"] for child in _flatten(tree)}
    assert tag_names <= {"DIV", "BUTTON", "H2", "H3", "UL", "LI", "P", "#text"}


# ── renderProfile: business profile / style fields are data, never markup ───

def test_render_profile_treats_business_and_style_text_as_data() -> None:
    source = _script_source()
    script = f"""
{_FAKE_DOM}
{_extract_function(source, "appendFactListSection")}
{_extract_const(source, "BUSINESS_TYPE_LABELS")}
{_extract_function(source, "businessTypeLabel")}
const profileState = document.createElement("div");
{_extract_function(source, "renderProfile")}

renderProfile(
    {{
        business_name: {json.dumps(_XSS_PAYLOAD)}, business_type: "other",
        short_description: {json.dumps(_XSS_PAYLOAD)}, profile_status: "usable",
        specializations: [{json.dumps(_XSS_PAYLOAD)}], destinations: [], region: "",
        audiences: [], tone: "", public_contacts: {{ website: {json.dumps(_XSS_PAYLOAD)} }},
        verified_claims: [{json.dumps(_XSS_PAYLOAD)}],
    }},
    {{ style_description: {json.dumps(_XSS_PAYLOAD)}, example_posts: [], avoid_phrases: [] }},
);
{_serializable_script("profileState")}
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    tree = json.loads(result.stdout.strip())

    all_text = _collect_text(tree)
    assert any(_XSS_PAYLOAD in item for item in all_text)
    tag_names = {child["tagName"] for child in _flatten(tree)}
    assert tag_names <= {"DIV", "BUTTON", "H2", "H3", "UL", "LI", "P", "#text"}


def test_render_profile_never_shows_workspace_memory() -> None:
    """workspace_memory is internal Assistant context (see /api/chat), not
    a user-facing profile field - renderProfile() must not reference it or
    render an "О проекте" section, even if a caller passed a 3rd argument."""
    source = _script_source()
    render_profile = _extract_function(source, "renderProfile")

    assert "workspaceMemory" not in render_profile
    assert "workspace_memory" not in render_profile
    assert "О проекте" not in render_profile

    script = f"""
{_FAKE_DOM}
{_extract_function(source, "appendFactListSection")}
{_extract_const(source, "BUSINESS_TYPE_LABELS")}
{_extract_function(source, "businessTypeLabel")}
const profileState = document.createElement("div");
{render_profile}

// Even if an old/rogue caller still passes a 3rd argument, renderProfile()
// only declares 2 parameters - it must be silently ignored.
renderProfile(null, null, "секретный внутренний конспект Ассистента");
{_serializable_script("profileState")}
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    tree = json.loads(result.stdout.strip())

    all_text = " ".join(_collect_text(tree))
    assert "секретный внутренний конспект Ассистента" not in all_text
    assert "О проекте" not in all_text


# ── renderHistory / renderSettings: same discipline, quick smoke checks ─────

def test_render_history_treats_module_text_as_data() -> None:
    source = _script_source()
    script = f"""
{_FAKE_DOM}
{_extract_const(source, "USAGE_MODULE_LABELS")}
{_extract_function(source, "usageModuleLabel")}
{_extract_const(source, "MATERIAL_STATUS_LABELS")}
{_extract_function(source, "materialStatusLabel")}
{_extract_function(source, "formatSignalDate")}
const historyState = document.createElement("div");
const ARTIFACT_STATUS_ORDER = ["draft", "review_required", "ready", "used", "archived"];
{_extract_function(source, "renderHistory")}

renderHistory(
    [{{ occurred_at: "2026-01-01T00:00:00+00:00", module: {json.dumps(_XSS_PAYLOAD)}, provider: "openai", model: null, status: "success" }}],
    {{ draft: 1, review_required: 0, ready: 0, used: 0, archived: 0 }},
);
{_serializable_script("historyState")}
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    tree = json.loads(result.stdout.strip())

    assert any(_XSS_PAYLOAD in item for item in _collect_text(tree))


def test_render_settings_treats_workspace_name_as_data() -> None:
    source = _script_source()
    script = f"""
{_FAKE_DOM}
{_extract_function(source, "appendFactListSection")}
{_extract_const(source, "WORKSPACE_STATUS_LABELS")}
{_extract_function(source, "workspaceStatusLabel")}
{_extract_function(source, "formatSignalDate")}
const settingsState = document.createElement("div");
{_extract_function(source, "renderSettings")}

renderSettings({{
    name: {json.dumps(_XSS_PAYLOAD)}, slug: "x", status: "active",
    access_status: "active", access_expires_at: null,
}});
{_serializable_script("settingsState")}
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    tree = json.loads(result.stdout.strip())

    assert any(_XSS_PAYLOAD in item for item in _collect_text(tree))
