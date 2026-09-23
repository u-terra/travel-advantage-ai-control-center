"""UX fix (before deploying be12cda): deleting the conversation currently
open in the Ассистент tab must not leave the UI holding a stale
conversation_id or showing the now-deleted conversation's messages.

deleteOneConversation()/bulkDeleteConversations()/clearAllConversations()
in app/templates/chat.html already null out currentConversationId when the
deleted conversation is the one currently open - these tests exercise the
same shipped functions via node (see test_chat_html_conversations.py's
docstring for why: no jsdom/npm toolchain in this repo) and additionally
assert clearChatArea() is called in that case, so the visible chat pane is
reset instead of silently keeping the deleted conversation's messages
on screen while the next message actually starts a brand-new backend
conversation (ensureConversationId() creates one once currentConversationId
is null).

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


_HARNESS = """
const historyState = { innerHTML: "" };
const calls = { clearChatArea: 0, loadConversations: 0, renderStateError: [] };
function renderStateLoading() {}
function renderStateError(container, title, message, retry) { calls.renderStateError.push(title); }
function clearChatArea() { calls.clearChatArea += 1; }
function loadConversations() { calls.loadConversations += 1; }
"""


def _fetch_ok(payload: str) -> str:
    return f"global.fetch = async () => ({{ ok: true, json: async () => ({payload}) }});"


# ── deleteOneConversation(): resets the chat pane only when the deleted
# conversation is the one currently open ────────────────────────────────

def test_delete_one_conversation_clears_chat_area_when_it_is_the_open_one() -> None:
    source = _script_source()
    script = f"""
{_HARNESS}
{_fetch_ok('{ deleted: true }')}
let currentConversationId = 42;
{_extract_function(source, "deleteOneConversation")}

deleteOneConversation({{ id: 42 }}).then(() => {{
    console.log(JSON.stringify({{ calls, currentConversationId }}));
}});
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())

    assert payload["currentConversationId"] is None
    assert payload["calls"]["clearChatArea"] == 1
    assert payload["calls"]["loadConversations"] == 1


def test_delete_one_conversation_leaves_a_different_open_conversation_untouched() -> None:
    source = _script_source()
    script = f"""
{_HARNESS}
{_fetch_ok('{ deleted: true }')}
let currentConversationId = 7;
{_extract_function(source, "deleteOneConversation")}

deleteOneConversation({{ id: 99 }}).then(() => {{
    console.log(JSON.stringify({{ calls, currentConversationId }}));
}});
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())

    assert payload["currentConversationId"] == 7
    assert payload["calls"]["clearChatArea"] == 0


# ── bulkDeleteConversations(): same rule, checked against the whole id list ──

def test_bulk_delete_clears_chat_area_when_open_conversation_is_included() -> None:
    source = _script_source()
    script = f"""
{_HARNESS}
{_fetch_ok('{ deleted_count: 2 }')}
let currentConversationId = 5;
{_extract_function(source, "bulkDeleteConversations")}

bulkDeleteConversations([5, 6]).then(() => {{
    console.log(JSON.stringify({{ calls, currentConversationId }}));
}});
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())

    assert payload["currentConversationId"] is None
    assert payload["calls"]["clearChatArea"] == 1


def test_bulk_delete_leaves_open_conversation_untouched_when_not_selected() -> None:
    source = _script_source()
    script = f"""
{_HARNESS}
{_fetch_ok('{ deleted_count: 2 }')}
let currentConversationId = 5;
{_extract_function(source, "bulkDeleteConversations")}

bulkDeleteConversations([1, 2]).then(() => {{
    console.log(JSON.stringify({{ calls, currentConversationId }}));
}});
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())

    assert payload["currentConversationId"] == 5
    assert payload["calls"]["clearChatArea"] == 0


# ── clearAllConversations(): always resets, there is nothing left to open ──

def test_clear_all_conversations_always_clears_chat_area() -> None:
    source = _script_source()
    script = f"""
{_HARNESS}
{_fetch_ok('{ deleted_count: 3 }')}
let currentConversationId = 11;
{_extract_function(source, "clearAllConversations")}

clearAllConversations().then(() => {{
    console.log(JSON.stringify({{ calls, currentConversationId }}));
}});
"""
    result = _run_node(script)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())

    assert payload["currentConversationId"] is None
    assert payload["calls"]["clearChatArea"] == 1
