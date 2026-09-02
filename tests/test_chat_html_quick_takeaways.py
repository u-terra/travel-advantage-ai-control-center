"""Tests for the "Краткий вывод" (quick takeaways) card on the Competitor
Intelligence report view in app/templates/chat.html.

No new LLM call - the card is built entirely from an already-fetched
snapshot's positioning and strengths only (opportunities, products, signals
and source titles/headlines are never used), with client-side filtering so
truncated/headline-like source text (e.g. "GPT Apps You Can Use in",
"...Which Is Better for Kids?...") never reaches the card. As with the
report's other inline-JS behavior, there's no jsdom/npm toolchain in this
repo, so these tests run the *actual* shipped functions via a `node`
subprocess rather than reimplementing the logic in Python. Skips cleanly
when `node` is missing.
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

_TAKEAWAY_FUNCTIONS = (
    "normalizeTakeawayKey",
    "normalizeTakeawayWhitespace",
    "stripTakeawayTrailingEllipsis",
    "isCutOffTakeaway",
    "isTooThinToBeAThought",
    "looksLikeHeadlineTitle",
    "shortenTakeawayAtSentenceBoundary",
    "prepareTakeawayCandidate",
    "buildQuickTakeaways",
)


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


def _build_quick_takeaways_fn() -> str:
    source = _script_source()
    parts = [
        _extract_const(source, "TAKEAWAY_DANGLING_WORDS"),
        _extract_const(source, "TAKEAWAY_TERMINAL_PUNCTUATION_RE"),
        _extract_const(source, "TAKEAWAY_MAX_LENGTH"),
    ]
    parts.extend(_extract_function(source, name) for name in _TAKEAWAY_FUNCTIONS)
    return "\n".join(parts)


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
        # present in real snapshots but must never be used by the takeaways
        # builder - included by default in a few tests below to prove that.
        "opportunities": [],
        "products": [],
        "fresh_signals": [],
    }
    base.update(overrides)
    return base


# ── priority order: positioning, then strengths; only these two fields ──────

def test_takes_positioning_then_strengths_in_order() -> None:
    intelligence = _intelligence(
        positioning=["Позиционируется как ОТА полного цикла.", "Фокус на азиатский рынок."],
        strengths=["Широкий инвентарь отелей."],
    )

    result = _run_build_quick_takeaways(intelligence)

    assert result == [
        "Позиционируется как ОТА полного цикла.",
        "Фокус на азиатский рынок.",
        "Широкий инвентарь отелей.",
    ]


def test_caps_at_three_items_even_with_more_available() -> None:
    intelligence = _intelligence(
        positioning=["Позиция один.", "Позиция два.", "Позиция три."],
        strengths=["Сила один.", "Сила два.", "Сила три."],
    )

    result = _run_build_quick_takeaways(intelligence)

    assert result == ["Позиция один.", "Позиция два.", "Позиция три."]


def test_opportunities_products_and_signals_are_never_used() -> None:
    """Even when positioning/strengths are empty, the card must stay empty -
    it must not fall back to opportunities/products/fresh_signals."""
    intelligence = _intelligence(
        opportunities=[{"key_thesis": "Полноценная идея из opportunities."}],
        products=["Отели", "Авиабилеты"],
        fresh_signals=["Полноценный свежий сигнал о конкуренте."],
    )

    result = _run_build_quick_takeaways(intelligence)

    assert result == []


# ── scarce data: fewer bullets than 3, nothing fabricated ───────────────────

def test_single_positioning_item_yields_single_bullet() -> None:
    intelligence = _intelligence(positioning=["Единственный содержательный факт."])

    result = _run_build_quick_takeaways(intelligence)

    assert result == ["Единственный содержательный факт."]


def test_two_valid_items_stay_two_not_padded_to_three() -> None:
    intelligence = _intelligence(
        positioning=["Первая законченная мысль."],
        strengths=["Вторая законченная мысль."],
    )

    result = _run_build_quick_takeaways(intelligence)

    assert result == ["Первая законченная мысль.", "Вторая законченная мысль."]


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
        positioning=["Общий текст про конкурента."],
        strengths=["Общий текст про конкурента.", "Другая сильная сторона."],
    )

    result = _run_build_quick_takeaways(intelligence)

    assert result == ["Общий текст про конкурента.", "Другая сильная сторона."]


def test_duplicate_ignores_case_and_surrounding_whitespace() -> None:
    intelligence = _intelligence(
        positioning=["  Текст ПРО конкурента.  "],
        strengths=["текст про конкурента."],
    )

    result = _run_build_quick_takeaways(intelligence)

    assert result == ["Текст ПРО конкурента."]


def test_dedup_does_not_starve_later_valid_items_below_the_cap() -> None:
    """A duplicate must simply be skipped, not consume one of the 3 slots -
    the third distinct valid item still gets in."""
    intelligence = _intelligence(
        positioning=["Общая мысль про конкурента."],
        strengths=[
            "Общая мысль про конкурента.",
            "Вторая сильная сторона конкурента.",
            "Третья сильная сторона конкурента.",
        ],
    )

    result = _run_build_quick_takeaways(intelligence)

    assert result == [
        "Общая мысль про конкурента.",
        "Вторая сильная сторона конкурента.",
        "Третья сильная сторона конкурента.",
    ]


# ── the specific real Trip.com fragments this round targets ─────────────────

def test_filters_out_dangling_preposition_fragment_gpt_apps_example() -> None:
    """Real-world obrubok #1 from Trip.com: a raw fact cut off mid-phrase,
    ending on a dangling preposition with no terminal punctuation."""
    intelligence = _intelligence(positioning=["GPT Apps You Can Use in"])

    result = _run_build_quick_takeaways(intelligence)

    assert result == []
    assert not any("GPT Apps You Can Use in" in item for item in result)


def test_filters_out_numbered_headline_variant_of_gpt_apps_example() -> None:
    """The real Trip.com item is prefixed with a listicle number ("8 GPT
    Apps..."). Title Case + a leading number is exactly the headline
    signature, so this must also be dropped."""
    intelligence = _intelligence(
        strengths=["8 GPT Apps You Can Use in ChatGPT for Travel Planning"],
    )

    result = _run_build_quick_takeaways(intelligence)

    assert result == []


def test_filters_out_which_is_better_for_kids_headline_fragment() -> None:
    """Real-world obrubok #2 from Trip.com: a comparison-headline fragment
    ending in a trailing "...". Even though "?" alone would look like a
    complete sentence, the Title Case pattern marks it as a source headline,
    not a thought about the competitor."""
    intelligence = _intelligence(
        strengths=["Shanghai Disneyland vs Universal Beijing: Which Is Better for Kids?..."],
    )

    result = _run_build_quick_takeaways(intelligence)

    assert result == []
    assert not any("Which Is Better for Kids" in item for item in result)


def test_headline_fragment_dropped_but_valid_sibling_item_kept() -> None:
    """Garbage is dropped, not used to pad the list, and doesn't block a
    genuinely valid item from the same field."""
    intelligence = _intelligence(
        strengths=[
            "GPT Apps You Can Use in",
            "Shanghai Disneyland vs Universal Beijing: Which Is Better for Kids?...",
            "Гибкая отмена бронирования без штрафа.",
        ],
    )

    result = _run_build_quick_takeaways(intelligence)

    assert result == ["Гибкая отмена бронирования без штрафа."]


def test_normal_sentence_with_one_proper_noun_is_not_mistaken_for_a_headline() -> None:
    """A real, complete sentence naturally capitalizes its first word and
    any proper nouns (here: "Trip.com", "OTA", "Азию") - it must not trip
    the headline-title heuristic just because a few words are capitalized."""
    intelligence = _intelligence(
        positioning=["Trip.com позиционируется как OTA полного цикла с фокусом на Азию."],
    )

    result = _run_build_quick_takeaways(intelligence)

    assert result == ["Trip.com позиционируется как OTA полного цикла с фокусом на Азию."]


def test_bare_short_remainder_after_ellipsis_strip_is_dropped() -> None:
    intelligence = _intelligence(positioning=["Shanghai..."])

    result = _run_build_quick_takeaways(intelligence)

    assert result == []


# ── long but valid text: shorten only at a sentence boundary, else keep whole ─

def test_long_valid_sentence_is_shortened_at_sentence_boundary() -> None:
    long_text = (
        "Конкурент активно продвигает новую программу лояльности для часто "
        "путешествующих клиентов. Это отдельное предложение, которое не "
        "должно попасть в вывод, потому что оно идёт уже после первой точки "
        "и делает исходный текст длиннее порога сокращения."
    )
    intelligence = _intelligence(positioning=[long_text])

    result = _run_build_quick_takeaways(intelligence)

    assert result == [
        "Конкурент активно продвигает новую программу лояльности для часто "
        "путешествующих клиентов."
    ]


def test_long_text_without_a_clean_sentence_boundary_is_kept_whole() -> None:
    """If there is no good sentence boundary within the length budget, the
    text must be kept in full rather than cut off mid-meaning."""
    long_text = "Слово" + " word" * 40  # no "." anywhere, well past the length cap
    intelligence = _intelligence(positioning=[long_text])

    result = _run_build_quick_takeaways(intelligence)

    assert result == [long_text]


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
        _build_quick_takeaways_fn(),
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
        positioning=["Позиция полностью законченная."],
        strengths=["Сила полностью законченная."],
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
    assert payload["itemTexts"] == [
        "Позиция полностью законченная.",
        "Сила полностью законченная.",
    ]


# ── wired into renderReport(), placed right after the header ────────────────

def test_render_report_calls_append_quick_takeaways_right_after_header() -> None:
    source = _script_source()
    render_report = _extract_function(source, "renderReport")

    header_index = render_report.index("wrap.appendChild(header);")
    takeaways_index = render_report.index("appendQuickTakeaways(wrap, intelligence);")
    sections_index = render_report.index("const sections = document.createElement")

    assert header_index < takeaways_index < sections_index
