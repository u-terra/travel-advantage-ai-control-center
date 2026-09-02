"""Tests for the "Краткий вывод" (quick takeaways) card on the Competitor
Intelligence report view in app/templates/chat.html.

No new LLM call - the card is built entirely from an already-fetched
snapshot (positioning/strengths/opportunities), with client-side filtering
so truncated/fragment-like source text (e.g. "GPT Apps You Can Use in",
"направления: Shanghai...") never reaches the card. As with the report's
other inline-JS behavior, there's no jsdom/npm toolchain in this repo, so
these tests run the *actual* shipped functions via a `node` subprocess
rather than reimplementing the logic in Python. Skips cleanly when `node`
is missing.
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
    "stripTakeawayTechnicalPrefix",
    "stripTakeawayTrailingEllipsis",
    "isCutOffTakeaway",
    "isTooThinToBeAThought",
    "truncateTakeawayAtBoundary",
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
        "opportunities": [],
    }
    base.update(overrides)
    return base


# ── priority order: positioning, then strengths, then opportunities ─────────

def test_takes_positioning_then_strengths_then_opportunities_in_order() -> None:
    intelligence = _intelligence(
        positioning=["Позиционируется как ОТА полного цикла.", "Фокус на азиатский рынок."],
        strengths=["Широкий инвентарь отелей."],
        opportunities=[{"key_thesis": "Запущен новый AI-планировщик поездок."}],
    )

    result = _run_build_quick_takeaways(intelligence)

    assert result == [
        "Позиционируется как ОТА полного цикла.",
        "Фокус на азиатский рынок.",
        "Широкий инвентарь отелей.",
        "Запущен новый AI-планировщик поездок.",
    ]


def test_caps_at_four_items_even_with_more_available() -> None:
    intelligence = _intelligence(
        positioning=["Позиция один.", "Позиция два.", "Позиция три."],
        strengths=["Сила один.", "Сила два.", "Сила три."],
        opportunities=[
            {"key_thesis": "Идея один."},
            {"key_thesis": "Идея два."},
            {"key_thesis": "Идея три."},
        ],
    )

    result = _run_build_quick_takeaways(intelligence)

    assert len(result) <= 4
    # at most 2 taken from positioning/strengths each, in priority order -
    # the two opportunities are never reached because the cap is already hit.
    assert result == ["Позиция один.", "Позиция два.", "Сила один.", "Сила два."]


# ── scarce data: fewer bullets than 3-4, nothing fabricated ─────────────────

def test_single_positioning_item_yields_single_bullet() -> None:
    intelligence = _intelligence(positioning=["Единственный содержательный факт."])

    result = _run_build_quick_takeaways(intelligence)

    assert result == ["Единственный содержательный факт."]


def test_two_valid_items_stay_two_not_padded_to_three_or_four() -> None:
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
        opportunities=[{"key_thesis": "Отдельная идея для контента."}],
    )

    result = _run_build_quick_takeaways(intelligence)

    assert result == [
        "Общий текст про конкурента.",
        "Другая сильная сторона.",
        "Отдельная идея для контента.",
    ]


def test_duplicate_ignores_case_and_surrounding_whitespace() -> None:
    intelligence = _intelligence(
        positioning=["  Текст ПРО конкурента.  "],
        strengths=["текст про конкурента."],
    )

    result = _run_build_quick_takeaways(intelligence)

    assert result == ["Текст ПРО конкурента."]


# ── opportunities: prefer key_thesis / audience_value over topic ────────────

def test_opportunity_uses_key_thesis_not_the_category_prefixed_topic() -> None:
    intelligence = _intelligence(
        opportunities=[{
            "topic": "направления: Shanghai Disneyland",
            "key_thesis": "Trip.com выпустил гид по Shanghai Disneyland.",
            "audience_value": "Помогает планировать поездку с детьми.",
        }],
    )

    result = _run_build_quick_takeaways(intelligence)

    assert result == ["Trip.com выпустил гид по Shanghai Disneyland."]
    assert not any("направления" in item for item in result)


def test_opportunity_falls_back_to_audience_value_when_key_thesis_is_cut_off() -> None:
    intelligence = _intelligence(
        opportunities=[{
            "topic": "AI и технологии в travel: AI-планировщик",
            "key_thesis": "GPT Apps You Can Use in",
            "audience_value": "Помогает путешественникам находить нужные инструменты быстрее.",
        }],
    )

    result = _run_build_quick_takeaways(intelligence)

    assert result == ["Помогает путешественникам находить нужные инструменты быстрее."]


def test_opportunity_topic_is_never_used_as_a_takeaway_source() -> None:
    """opportunity.topic is always built by the backend as "category: entity"
    (see _opportunities() in competitor_intelligence.py) - by construction
    it is a label, not a thought, so it must never surface here even as a
    last resort."""
    intelligence = _intelligence(
        opportunities=[{"topic": "loyalty и promotions: Trip Coins бонусная программа"}],
    )

    result = _run_build_quick_takeaways(intelligence)

    assert result == []


def test_opportunity_with_blank_key_thesis_and_audience_value_yields_nothing() -> None:
    intelligence = _intelligence(
        opportunities=[{"key_thesis": "   ", "audience_value": ""}],
    )

    result = _run_build_quick_takeaways(intelligence)

    assert result == []


# ── the specific truncated/fragment examples reported by the user ───────────

def test_filters_out_dangling_preposition_fragment_gpt_apps_example() -> None:
    """Real-world obrubok: a raw fact/thesis cut off mid-phrase, ending on a
    dangling preposition with no terminal punctuation."""
    intelligence = _intelligence(positioning=["GPT Apps You Can Use in"])

    result = _run_build_quick_takeaways(intelligence)

    assert result == []
    assert not any("GPT Apps You Can Use in" in item for item in result)


def test_gpt_apps_fragment_dropped_but_valid_sibling_item_kept() -> None:
    """Garbage is dropped, not used to pad the list, and doesn't block a
    genuinely valid item from the same field."""
    intelligence = _intelligence(
        strengths=["GPT Apps You Can Use in", "Гибкая отмена бронирования без штрафа."],
    )

    result = _run_build_quick_takeaways(intelligence)

    assert result == ["Гибкая отмена бронирования без штрафа."]


def test_strips_category_prefix_and_drops_bare_entity_shanghai_example() -> None:
    """Real-world obrubok: "направления: Shanghai..." - a technical
    category-prefixed opportunity-topic label, not a thought. The category
    prefix is stripped, the trailing "..." is stripped, and what remains
    ("Shanghai") is a single bare word - too thin to count as a
    self-sufficient idea, so nothing is shown for it."""
    intelligence = _intelligence(positioning=["направления: Shanghai..."])

    result = _run_build_quick_takeaways(intelligence)

    assert result == []
    assert not any("направления" in item for item in result)
    assert not any(item == "Shanghai..." for item in result)


def test_technical_prefix_stripped_when_remainder_is_a_full_thought() -> None:
    intelligence = _intelligence(
        positioning=["направления: Гид по паркам Шанхая набирает популярность."],
    )

    result = _run_build_quick_takeaways(intelligence)

    assert result == ["Гид по паркам Шанхая набирает популярность."]


# ── long but valid text: shorten only at a sentence/word boundary ───────────

def test_long_valid_sentence_is_truncated_at_sentence_boundary_not_mid_word() -> None:
    long_text = (
        "Конкурент активно продвигает новую программу лояльности для часто "
        "путешествующих клиентов. Это отдельное предложение, которое не "
        "должно попасть в вывод, потому что оно идёт уже после первой точки "
        "и делает исходный текст длиннее порога сокращения."
    )
    intelligence = _intelligence(positioning=[long_text])

    result = _run_build_quick_takeaways(intelligence)

    assert len(result) == 1
    takeaway = result[0]
    assert takeaway == (
        "Конкурент активно продвигает новую программу лояльности для часто "
        "путешествующих клиентов."
    )
    assert "Это отдельное предложение" not in takeaway
    # no dangling half-word: every remaining token is a real word/punctuation
    assert not takeaway.rstrip(".").split(" ")[-1] == ""


def test_long_text_without_early_sentence_end_is_cut_at_word_boundary_with_ellipsis() -> None:
    long_text = "Слово" + " word" * 40  # no "." anywhere, well past the length cap
    intelligence = _intelligence(positioning=[long_text])

    result = _run_build_quick_takeaways(intelligence)

    assert len(result) == 1
    takeaway = result[0]
    assert takeaway.endswith("…")
    # the character right before the ellipsis is not mid-word: the boundary
    # is always a full "word" token from the original text.
    body = takeaway[:-1].strip()
    assert body != "" and long_text.startswith(body)
    assert long_text[len(body):len(body) + 1] in (" ", "")


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
