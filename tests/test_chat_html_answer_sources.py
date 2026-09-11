"""UI tests for the "Источники" block after an assistant answer
(ORCHESTRAVEL web-search MVP - this is the ONE place sources are shown to
the user; the model is separately instructed not to print its own closing
"Источники"/"Sources" section, see test_web_search_service.py::
test_format_search_context_instructs_model_not_to_add_final_sources_section
and test_web_api_web_search.py::
test_knowledge_context_instructs_model_not_to_add_final_sources_section).

No jsdom/npm toolchain in this repo, so this runs the *actual* shipped
collectAnswerSources()/appendAnswerSources()/sourceLabel() via a `node`
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


def _collect_script(data: dict) -> str:
    source = _script_source()
    return f"""
{_extract_function(source, "looksLikeHttpUrl")}
{_extract_function(source, "domainFromUrl")}
{_extract_function(source, "sourceLabel")}
{_extract_function(source, "collectAnswerSources")}
console.log(JSON.stringify(collectAnswerSources({json.dumps(data, ensure_ascii=False)})));
"""


def _run_collect(data: dict) -> list:
    result = _run_node(_collect_script(data))
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


def _run_full(data: dict) -> dict:
    """collectAnswerSources(data) -> appendAnswerSources(bubble, items), in
    one node process - the actual end-to-end path submitMessage() uses."""
    source = _script_source()
    script = f"""
{_FAKE_DOM}
{_extract_function(source, "looksLikeHttpUrl")}
{_extract_function(source, "domainFromUrl")}
{_extract_function(source, "sourceLabel")}
{_extract_function(source, "collectAnswerSources")}
{_extract_function(source, "appendAnswerSources")}
const bubble = document.createElement("div");
appendAnswerSources(bubble, collectAnswerSources({json.dumps(data, ensure_ascii=False)}));
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
        "label": "MWR Life Compensation Plan — example.org",
    }]


def test_knowledge_sources_without_url_reference_are_dropped():
    """F: a non-URL citation (e.g. a document name) cannot be made
    clickable - no <a href="..."> is created for it, nothing is shown."""
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
        "label": "Правила въезда — example.org",
    }]
    # provider is deliberately not part of the collected item - see
    # test_provider_name_never_appears_in_rendered_text below.


def test_both_kinds_of_sources_combine_search_first_then_knowledge():
    """5: live web-search sources must stay visible - they are listed
    before knowledge-base sources."""
    items = _run_collect({
        "knowledge_sources": [{"title": "K", "reference": "https://k.example/"}],
        "search_sources": [{"title": "S", "url": "https://s.example/", "domain": "s.example"}],
    })
    assert [item["url"] for item in items] == ["https://s.example/", "https://k.example/"]


def test_missing_source_lists_are_treated_as_empty():
    assert _run_collect({}) == []


# ── D: title equal to the URL (or missing) falls back to domain ────────────


def test_search_source_with_title_equal_to_url_shows_domain_not_url():
    items = _run_collect({
        "knowledge_sources": [],
        "search_sources": [
            {"title": "https://example.org/very/long/path/to/entry-rules", "url": "https://example.org/very/long/path/to/entry-rules", "domain": "example.org"},
        ],
    })
    assert items == [{"url": "https://example.org/very/long/path/to/entry-rules", "label": "example.org"}]


def test_search_source_with_missing_title_shows_domain():
    items = _run_collect({
        "knowledge_sources": [],
        "search_sources": [
            {"title": "", "url": "https://example.org/a", "domain": "example.org"},
        ],
    })
    assert items == [{"url": "https://example.org/a", "label": "example.org"}]


def test_source_label_with_no_title_and_no_domain_falls_back_to_url_as_last_resort():
    """sourceLabel() directly - collectAnswerSources() always derives a
    domain from a valid http(s) URL via domainFromUrl(), so this all-empty
    case cannot occur through the normal data path, but sourceLabel() must
    still degrade gracefully if it ever does (e.g. a malformed domain)."""
    source = _script_source()
    script = f"""
{_extract_function(source, "sourceLabel")}
console.log(JSON.stringify(sourceLabel("", "https://example.org/a", "")));
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == "https://example.org/a"


# ── E: dedupe by exact URL, across and within the two lists ────────────────


def test_duplicate_url_across_search_and_knowledge_is_shown_once():
    items = _run_collect({
        "knowledge_sources": [{"title": "K title", "reference": "https://shared.example/x"}],
        "search_sources": [{"title": "S title", "url": "https://shared.example/x", "domain": "shared.example"}],
    })
    assert len(items) == 1
    # search_sources wins the dedupe (processed first - see ordering above).
    assert items[0]["label"] == "S title — shared.example"


def test_duplicate_url_within_search_sources_is_shown_once():
    items = _run_collect({
        "knowledge_sources": [],
        "search_sources": [
            {"title": "First", "url": "https://a.example/1", "domain": "a.example"},
            {"title": "Duplicate", "url": "https://a.example/1", "domain": "a.example"},
        ],
    })
    assert len(items) == 1
    assert items[0]["label"] == "First — a.example"


# ── appendAnswerSources: rendering, clickability, empty block ──────────────


def test_empty_items_render_nothing():
    tree = _run_append([])
    assert tree["children"] == []


def test_items_render_as_clickable_links_opening_in_new_tab():
    items = [{"url": "https://example.org/a", "label": "Заголовок — example.org"}]
    tree = _run_append(items)

    wrap = tree["children"][0]
    assert wrap["className"] == "answer-sources"

    label = wrap["children"][0]
    assert label["className"] == "answer-sources-label"
    assert label["textContent"] == "Источники:"

    link = wrap["children"][1]
    assert link["tagName"] == "A"
    # B: href is ONLY the URL - no title/domain/display text mixed in.
    assert link["href"] == "https://example.org/a"
    assert link["target"] == "_blank"
    assert link["rel"] == "noopener noreferrer"
    # C: display text is title + domain.
    assert link["textContent"] == "Заголовок — example.org"


def test_provider_name_never_appears_in_rendered_text():
    """Yandex finds, OpenAI answers - the UI must not read like "ответ
    Яндекса" even though provider=yandex is carried in the JSON."""
    tree = _run_full({
        "knowledge_sources": [],
        "search_sources": [
            {"title": "Заголовок", "url": "https://example.org/a", "domain": "example.org", "provider": "yandex"},
        ],
    })
    all_text = " ".join(n["textContent"] for n in _flatten(tree) if n["textContent"])
    assert "yandex" not in all_text.lower()
    assert "яндекс" not in all_text.lower()


def test_multiple_items_each_get_their_own_link():
    items = [
        {"url": "https://a.example/", "label": "A — a.example"},
        {"url": "https://b.example/", "label": "B — b.example"},
    ]
    tree = _run_append(items)
    wrap = tree["children"][0]
    links = [n for n in wrap["children"] if n["tagName"] == "A"]
    assert [link["href"] for link in links] == ["https://a.example/", "https://b.example/"]


# ── A, G: end-to-end collect+render ─────────────────────────────────────────


def test_two_search_sources_render_as_one_block_two_links():
    tree = _run_full({
        "knowledge_sources": [],
        "search_sources": [
            {"title": "First", "url": "https://a.example/1", "domain": "a.example"},
            {"title": "Second", "url": "https://b.example/2", "domain": "b.example"},
        ],
    })
    blocks = [n for n in tree["children"] if n["className"] == "answer-sources"]
    assert len(blocks) == 1
    links = [n for n in blocks[0]["children"] if n["tagName"] == "A"]
    assert len(links) == 2
    assert [link["href"] for link in links] == ["https://a.example/1", "https://b.example/2"]


def test_no_sources_at_all_renders_no_block():
    tree = _run_full({"knowledge_sources": [], "search_sources": []})
    assert tree["children"] == []


def test_only_non_url_knowledge_sources_renders_no_block():
    tree = _run_full({
        "knowledge_sources": [{"title": "Doc", "reference": "не URL, просто название"}],
        "search_sources": [],
    })
    assert tree["children"] == []


# ── I: appending sources does not disturb existing bubble content ──────────


def test_appending_sources_preserves_existing_bubble_children():
    """The answer's own (already-rendered Markdown) content lives in the
    bubble before appendAnswerSources runs (see submitMessage() in
    chat.html: thinking.innerHTML is set first, appendAnswerSources called
    after) - it must still be there, untouched, in front of the sources
    block."""
    source = _script_source()
    script = f"""
{_FAKE_DOM}
{_extract_function(source, "appendAnswerSources")}
const bubble = document.createElement("div");
const answerText = document.createElement("p");
answerText.textContent = "Официальная форма: https://example.org/a";
bubble.appendChild(answerText);
appendAnswerSources(bubble, [{{"url": "https://example.org/a", "label": "example.org"}}]);
{_serialize_script("bubble")}
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    tree = json.loads(result.stdout.strip())

    assert len(tree["children"]) == 2
    assert tree["children"][0]["tagName"] == "P"
    assert tree["children"][0]["textContent"] == "Официальная форма: https://example.org/a"
    assert tree["children"][1]["className"] == "answer-sources"
