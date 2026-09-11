"""UX unification tests for the "Сигналы и идеи" Web section in
app/templates/chat.html (ORCHESTRAVEL: unify Web UX with Telegram).

Telegram and Web now read identical data through the same
build_workspace_signals() (see app/services/lead_radar.py) - this file only
tests what the Web card actually RENDERS from that data:

- action_reason ("Почему стоит обратить внимание") and, for content signals,
  content_hint ("Как можно подать") are shown - the same text Telegram
  already shows (why_text()/content_angle_hint() in lead_radar.py), not a
  separate Web-only formulation;
- ai_score is never rendered (it is a flat per-category constant, not a
  quality signal - see app/services/lead_radar.py:_content_quality_rank);
- action buttons depend on recommended_action, not a single fixed set for
  every card;
- "Открыть источник" appears only when item.url is present.

No jsdom/npm toolchain in this repo, so this runs the *actual* shipped
renderSignalsList()/signalActionPlan() via a `node` subprocess, same pattern
as the other tests/test_chat_html_*.py files. Skips cleanly when `node` is
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
        this.disabled = false;
        this._listeners = {};
    }
    set textContent(value) { this._textContent = value; }
    get textContent() { return this._textContent; }
    set innerHTML(value) { if (value === "") this.children = []; }
    appendChild(el) { this.children.push(el); return el; }
    addEventListener(type, handler) { this._listeners[type] = handler; }
    click() { if (this._listeners.click) this._listeners.click(); }
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


def _serialize_script(node_expr: str) -> str:
    return f"""
function serialize(el) {{
    return {{
        tagName: el.tagName,
        className: el.className,
        textContent: el.textContent,
        href: el.href || null,
        target: el.target || null,
        children: el.children.map(serialize),
    }};
}}
console.log(JSON.stringify(serialize({node_expr})));
"""


def _flatten(node_obj):
    yield node_obj
    for child in node_obj.get("children", []):
        yield from _flatten(child)


def _texts_by_class(tree, class_name: str) -> list[str]:
    return [n["textContent"] for n in _flatten(tree) if n["className"] == class_name]


def _all_text(tree) -> str:
    return " ".join(n["textContent"] for n in _flatten(tree) if n["textContent"])


_BASE_ITEM = {
    "id": 1,
    "title": "Тема сигнала",
    "category": "market_signal",
    "category_label": "👀 Наблюдать рынок",
    "recommended_action": "observe",
    "source_type": "telegram",
    "source_name": "Тестовый источник",
    "created_at": "2026-01-01T00:00:00+00:00",
    "score": 45.0,
    "url": "https://example.org/post/1",
    "action_reason": "Похоже, эта тема сейчас активно обсуждается на рынке.",
    "content_hint": None,
}


def _render(item: dict) -> dict:
    source = _script_source()
    script = f"""
{_FAKE_DOM}
function formatSignalDate() {{ return "01.01.2026"; }}
function openSignalInAssistant() {{}}
function createMaterialFromSignal() {{}}
const signalsState = document.createElement("div");
{_extract_function(source, "signalActionPlan")}
{_extract_function(source, "renderSignalsList")}

renderSignalsList([{json.dumps(item, ensure_ascii=False)}]);
{_serialize_script("signalsState")}
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip())


# ── A: market / observe card ─────────────────────────────────────────────────

def test_A_market_card_shows_reason_and_discuss_post_not_client_message():
    tree = _render({**_BASE_ITEM})
    all_text = _all_text(tree)

    assert _BASE_ITEM["action_reason"] in all_text
    assert "Разобрать в Ассистенте" in all_text
    assert "Подготовить пост" in all_text
    assert "Сообщение клиентам" not in all_text
    assert "Score" not in all_text
    assert "45" not in all_text  # округлённый ai_score нигде не должен всплыть


# ── B: content card ──────────────────────────────────────────────────────────

def test_B_content_card_shows_reason_hint_post_and_discuss_not_client_message():
    item = {
        **_BASE_ITEM,
        "category": "content_signal",
        "category_label": "💡 Тема для контента",
        "recommended_action": "content",
        "action_reason": "Тема перекликается с интересами вашей аудитории.",
        "content_hint": "Свяжите тему с вашим направлением и добавьте личный пример или мнение.",
    }
    tree = _render(item)
    all_text = _all_text(tree)

    assert item["action_reason"] in all_text
    assert "Как можно подать:" in all_text
    assert item["content_hint"] in all_text
    assert "Подготовить пост" in all_text
    assert "Разобрать в Ассистенте" in all_text
    assert "Сообщение клиентам" not in all_text
    assert "Score" not in all_text


# ── C: careful_reply card ─────────────────────────────────────────────────────

def test_C_careful_reply_card_shows_reply_action_not_generic_button_set():
    item = {
        **_BASE_ITEM,
        "category": "lead_signal",
        "category_label": "🎯 Вопрос клиента",
        "recommended_action": "careful_reply",
        "action_reason": "Похоже на вопрос от потенциального клиента.",
    }
    tree = _render(item)
    all_text = _all_text(tree)

    assert "Подготовить ответ" in all_text
    # Никакого бессмысленного общего набора: ни "разобрать пост"-кнопки, ни
    # "подготовить пост" как для контентной идеи.
    assert "Подготовить пост" not in all_text
    assert "Разобрать в Ассистенте" not in all_text
    assert "Score" not in all_text


# ── D / E: "Открыть источник" depends only on item.url ───────────────────────

def test_D_source_link_shown_when_url_present():
    tree = _render({**_BASE_ITEM, "url": "https://example.org/post/1"})
    link = next(n for n in _flatten(tree) if n["className"] == "signal-source-link")
    assert link["textContent"] == "Открыть источник"
    assert link["href"] == "https://example.org/post/1"
    assert link["target"] == "_blank"


def test_E_source_link_absent_when_url_missing():
    tree = _render({**_BASE_ITEM, "url": ""})
    links = [n for n in _flatten(tree) if n["className"] == "signal-source-link"]
    assert links == []
    assert "Открыть источник" not in _all_text(tree)
