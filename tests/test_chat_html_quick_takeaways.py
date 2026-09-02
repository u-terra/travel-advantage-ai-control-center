"""Tests for the "Краткий вывод" (quick takeaways) card on the Competitor
Intelligence report view in app/templates/chat.html.

No new LLM call - the card is built entirely from an already-fetched
snapshot (positioning/strengths/opportunities). As with the report's other
inline-JS behavior, there's no jsdom/npm toolchain in this repo, so these
tests run the *actual* shipped functions via a `node` subprocess rather than
reimplementing the logic in Python. Skips cleanly when `node` is missing.
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


def _build_quick_takeaways_fn() -> str:
    source = _script_source()
    return "\n".join([
        _extract_function(source, "normalizeTakeawayKey"),
        _extract_function(source, "buildQuickTakeaways"),
    ])


def _run_node(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=30,
        encoding="utf-8",
    )


def _run_build_quick_takeaways(intelligence: dict) -> list[str]:
    script = f"""
{_build_quick_takeaways_fn()}
console.log(JSON.stringify(buildQuickTakeaways({json.dumps(intelligence)})));
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip())


def _intelligence(**overrides) -> dict:
    base = {
        "positioning": [],
        "strengths": [],
        "opportunities": [],
    }
    base.update(overrides)
    return base


# ── priority order: positioning, then strengths, then opportunities ─────────

def test_takes_positioning_then_strengths_then_opportunities_in_order() -> None:
    intelligence = _intelligence(
        positioning=["Позиция A", "Позиция B"],
        strengths=["Сила A"],
        opportunities=[{"topic": "Идея A"}],
    )

    result = _run_build_quick_takeaways(intelligence)

    assert result == ["Позиция A", "Позиция B", "Сила A", "Идея A"]


def test_caps_at_five_items_even_with_more_available() -> None:
    intelligence = _intelligence(
        positioning=["P1", "P2", "P3"],
        strengths=["S1", "S2", "S3"],
        opportunities=[{"topic": "O1"}, {"topic": "O2"}, {"topic": "O3"}],
    )

    result = _run_build_quick_takeaways(intelligence)

    assert len(result) <= 5
    # at most 2 taken from each category, in priority order
    assert result == ["P1", "P2", "S1", "S2", "O1"]


# ── scarce data: fewer bullets, nothing fabricated ───────────────────────────

def test_single_positioning_item_yields_single_bullet() -> None:
    intelligence = _intelligence(positioning=["Единственный факт"])

    result = _run_build_quick_takeaways(intelligence)

    assert result == ["Единственный факт"]


def test_no_data_anywhere_yields_empty_list() -> None:
    intelligence = _intelligence()

    result = _run_build_quick_takeaways(intelligence)

    assert result == []


def test_missing_fields_do_not_crash_and_yield_empty_list() -> None:
    """intelligence payloads are whatever was actually saved - a field being
    entirely absent (not just empty) must not throw."""
    script = f"""
{_build_quick_takeaways_fn()}
console.log(JSON.stringify(buildQuickTakeaways({{}})));
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == []


# ── dedup: strengths in the real service can literally repeat positioning ───

def test_duplicate_text_between_positioning_and_strengths_is_not_repeated() -> None:
    """CompetitorIntelligenceService seeds strengths from the same summaries
    used for positioning, so exact repeats are expected input, not an edge
    case - the card must not show the same sentence twice."""
    intelligence = _intelligence(
        positioning=["Общий текст"],
        strengths=["Общий текст", "Другая сила"],
        opportunities=[{"topic": "Идея"}],
    )

    result = _run_build_quick_takeaways(intelligence)

    assert result == ["Общий текст", "Другая сила", "Идея"]


def test_duplicate_ignores_case_and_surrounding_whitespace() -> None:
    intelligence = _intelligence(
        positioning=["  Текст ПРО конкурента  "],
        strengths=["текст про конкурента"],
    )

    result = _run_build_quick_takeaways(intelligence)

    assert result == ["Текст ПРО конкурента"]


def test_blank_and_missing_opportunity_topics_are_skipped() -> None:
    """Only the first two opportunities are ever considered (same top-2
    slicing as positioning/strengths) - within that window, a blank or
    missing topic is skipped rather than fabricated."""
    intelligence = _intelligence(
        opportunities=[{"topic": "   "}, {"topic": "Реальная идея"}],
    )

    result = _run_build_quick_takeaways(intelligence)

    assert result == ["Реальная идея"]


def test_all_blank_or_missing_opportunity_topics_yield_no_bullet() -> None:
    intelligence = _intelligence(opportunities=[{"topic": "   "}, {}])

    result = _run_build_quick_takeaways(intelligence)

    assert result == []


# ── appendQuickTakeaways: renders nothing when there is nothing to show ─────

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
}
const document = { createElement: (tag) => new FakeElement(tag) };
"""


def _append_quick_takeaways_fn() -> str:
    source = _script_source()
    return "\n".join([
        _extract_function(source, "normalizeTakeawayKey"),
        _extract_function(source, "buildQuickTakeaways"),
        _extract_function(source, "appendQuickTakeaways"),
    ])


def test_append_quick_takeaways_adds_nothing_when_no_data() -> None:
    script = f"""
{_FAKE_DOM}
{_append_quick_takeaways_fn()}

const container = document.createElement("div");
appendQuickTakeaways(container, {json.dumps(_intelligence())});
console.log(JSON.stringify({{ childCount: container.children.length }}));
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip())["childCount"] == 0


def test_append_quick_takeaways_renders_compact_card_with_heading_and_items() -> None:
    intelligence = _intelligence(
        positioning=["Позиция A"],
        strengths=["Сила A"],
    )
    script = f"""
{_FAKE_DOM}
{_append_quick_takeaways_fn()}

const container = document.createElement("div");
appendQuickTakeaways(container, {json.dumps(intelligence)});

const card = container.children[0];
const heading = card.children[0];
const list = card.children[1];
const result = {{
    childCount: container.children.length,
    cardClassName: card.className,
    headingText: heading.textContent,
    itemCount: list.children.length,
    itemTexts: list.children.map(li => li.textContent),
}};
console.log(JSON.stringify(result));
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())

    assert payload["childCount"] == 1
    assert payload["cardClassName"] == "report-quick-takeaways"
    assert payload["headingText"] == "Краткий вывод"
    assert payload["itemCount"] == 2
    assert payload["itemTexts"] == ["Позиция A", "Сила A"]


# ── wired into renderReport(), placed right after the header ────────────────

def test_render_report_calls_append_quick_takeaways_right_after_header() -> None:
    source = _script_source()
    render_report = _extract_function(source, "renderReport")

    header_index = render_report.index("wrap.appendChild(header);")
    takeaways_index = render_report.index("appendQuickTakeaways(wrap, intelligence);")
    sections_index = render_report.index("const sections = document.createElement")

    assert header_index < takeaways_index < sections_index
