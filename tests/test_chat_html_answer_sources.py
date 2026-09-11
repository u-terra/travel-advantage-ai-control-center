"""UI tests for the "Источники" block after an assistant answer
(ORCHESTRAVEL web-search MVP - closes the pre-existing gap where
knowledge_sources was already returned by /api/chat but never rendered, and
adds the same treatment for search_sources).

No jsdom/npm toolchain in this repo, so this runs the *actual* shipped
collectAnswerSources()/appendAnswerSources() via a `node` subprocess, same
pattern as the other tests/test_chat_html_*.py files. Skips cleanly when
`node` is missing.
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
        this._attrs = {};
    }
    set textContent(value) { this._textContent = value; }
    get textContent() { return this._textContent; }
    set href(value) { this._attrs.href = value; }
    get href() { return this._attrs.href; }
    set target(value) { this._attrs.target = value; }
    get target() { return this._attrs.target; }
    set rel(value) { this._attrs.rel = value; }
    get rel() { return this._attrs.rel; }
    appendChild(el) { this.children.push(el); return el; }
}
const document = { createElement: (tag) => new FakeElement(tag) };
"""


def _serialize_script(node_expr: str) -> str:
    return f"""
function serialize(el) {{
    return {{
        tagName: el.tagName,
        className: el.className,
        textContent: el.textContent,
        href: el.href || null,
        target: el.target || null,
        rel: el.rel || null,
        children: el.children.map(serialize),
    }};
}}
console.log(JSON.stringify(serialize({node_expr})));
"""


def _flatten(node_obj):
    yield node_obj
    for child in node_obj.get("children", []):
        yield from _flatten(child)


def _run_collect(data: dict) -> list:
    source = _script_source()
    script = f"""
{_extract_function(source, "looksLikeHttpUrl")}
{_extract_function(source, "domainFromUrl")}
{_extract_function(source, "collectAnswerSources")}
console.log(JSON.stringify(collectAnswerSources({json.dumps(data, ensure_ascii=False)})));
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip())


def _run_append(items: list) -> dict:
    source = _script_source()
    script = f"""
{_FAKE_DOM}
{_extract_function(source, "appendAnswerSources")}
const bubble = document.createElement("div");
appendAnswerSources(bubble, {json.dumps(items, ensure_ascii=False)});
{_serialize_script("bubble")}
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip())


# ── collectAnswerSources: filtering + shape ─────────────────────────────────

def test_knowledge_sources_with_real_url_are_kept():
    items = _run_collect({
        "knowledge_sources": [
            {"title": "MWR Life Compensation Plan", "reference": "https://example.org/plan.pdf"},
        ],
        "search_sources": [],
    })
    assert items == [{
        "url": "https://example.org/plan.pdf",
        "title": "MWR Life Compensation Plan",
        "domain": "example.org",
    }]


def test_knowledge_sources_without_url_reference_are_dropped():
    """A non-URL citation (e.g. a document name) cannot be made clickable -
    show nothing for it rather than a dead, unclickable line."""
    items = _run_collect({
        "knowledge_sources": [
            {"title": "Внутренний документ", "reference": "Compensation Plan v3, стр. 12"},
        ],
        "search_sources": [],
    })
    assert items == []


def test_search_sources_are_included_with_their_own_domain():
    items = _run_collect({
        "knowledge_sources": [],
        "search_sources": [
            {"title": "Правила въезда", "url": "https://example.org/entry", "domain": "example.org", "provider": "yandex"},
        ],
    })
    assert items == [{
        "url": "https://example.org/entry",
        "title": "Правила въезда",
        "domain": "example.org",
    }]
    # provider is deliberately not part of the collected item - see
    # appendAnswerSources test below confirming it never reaches the DOM text.


def test_both_kinds_of_sources_combine_into_one_list():
    items = _run_collect({
        "knowledge_sources": [{"title": "K", "reference": "https://k.example/"}],
        "search_sources": [{"title": "S", "url": "https://s.example/", "domain": "s.example"}],
    })
    assert [item["url"] for item in items] == ["https://k.example/", "https://s.example/"]


def test_missing_source_lists_are_treated_as_empty():
    assert _run_collect({}) == []


# ── appendAnswerSources: rendering, clickability, empty block ──────────────

def test_empty_items_render_nothing():
    tree = _run_append([])
    assert tree["children"] == []


def test_items_render_as_clickable_links_opening_in_new_tab():
    items = [
        {"url": "https://example.org/a", "title": "Заголовок", "domain": "example.org"},
    ]
    tree = _run_append(items)

    wrap = tree["children"][0]
    assert wrap["className"] == "answer-sources"

    label = wrap["children"][0]
    assert label["className"] == "answer-sources-label"
    assert label["textContent"] == "Источники:"

    link = wrap["children"][1]
    assert link["tagName"] == "A"
    assert link["href"] == "https://example.org/a"
    assert link["target"] == "_blank"
    assert link["rel"] == "noopener noreferrer"
    assert link["textContent"] == "Заголовок — example.org"


def test_provider_name_never_appears_in_rendered_text():
    """Yandex finds, OpenAI answers - the UI must not read like "ответ
    Яндекса" even though provider=yandex is carried in the JSON."""
    items = [
        {"url": "https://example.org/a", "title": "Заголовок", "domain": "example.org"},
    ]
    tree = _run_append(items)
    all_text = " ".join(n["textContent"] for n in _flatten(tree) if n["textContent"])
    assert "yandex" not in all_text.lower()
    assert "яндекс" not in all_text.lower()


def test_multiple_items_each_get_their_own_link():
    items = [
        {"url": "https://a.example/", "title": "A", "domain": "a.example"},
        {"url": "https://b.example/", "title": "B", "domain": "b.example"},
    ]
    tree = _run_append(items)
    wrap = tree["children"][0]
    links = [n for n in wrap["children"] if n["tagName"] == "A"]
    assert [link["href"] for link in links] == ["https://a.example/", "https://b.example/"]
