"""Unit tests for app.web_api._signal_intent_context - the prompt block
built for "какие сейчас важные сигналы" style questions (see
tests/test_web_api_workspace_only_signals.py for the end-to-end /api/chat
routing coverage of the same feature).

Live quality bug (2026-09-23): the Assistant wrote "вопрос был вынесен на
обсуждение 24 сентября" the day BEFORE that date, as if it had already
happened, invented no-URL sources, and blurred "факт из источника" with
its own content idea. These tests cover the three fixes: date handling
(today's date + a signal's own publish date are both explicit, with a rule
against stating a future event as already past or inventing an unclear
date), source_name/URL fidelity, and an explicit fact-vs-idea separation
rule - plus reconfirm the compact 3-5 signal shape.

Requires the web-only dependencies (requirements-web.txt: fastapi,
uvicorn, markdown). Skips cleanly when they're not installed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("markdown")

from app.services.signal_service import UnifiedSignal  # noqa: E402
from app.web_api import _signal_intent_context  # noqa: E402


def _signal(
    *, title="Signal title", source_name="Турпром", url="https://example.org/item",
    created_at=None, action_reason="Причина",
) -> UnifiedSignal:
    return UnifiedSignal(
        id="radar:1", kind="radar", title=title, summary="", category=None,
        category_label=None, recommended_action="observe", source_type="rss",
        source_name=source_name, created_at=created_at or datetime.now(timezone.utc).isoformat(),
        url=url, score=None, action_reason=action_reason, content_hint=None,
        freshness_hours=1.0,
    )


# ── dates: today + publish date explicit, no past-tense-for-future rule ──

def test_context_includes_todays_date() -> None:
    context = _signal_intent_context([_signal()], workspace_only=False, compact=True)

    today = datetime.now(timezone.utc).date().isoformat()
    assert f"Сегодняшняя дата: {today}" in context


def test_context_includes_signal_publish_date_separately_from_event_date() -> None:
    published_at = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    context = _signal_intent_context(
        [_signal(
            title="Вопрос вынесен на обсуждение 24 сентября",
            created_at=published_at,
        )],
        workspace_only=False, compact=True,
    )

    published_date = published_at.split("T", 1)[0]
    assert f"опубликовано источником: {published_date}" in context


def test_context_forbids_describing_a_future_event_as_already_past() -> None:
    context = _signal_intent_context([_signal()], workspace_only=False, compact=True)

    lowered = context.lower()
    assert "будущее время" in lowered or "предстоящее событие" in lowered
    assert "никогда не описывай будущее событие так, будто оно уже произошло" in lowered


def test_context_forbids_inventing_an_unclear_date() -> None:
    context = _signal_intent_context([_signal()], workspace_only=False, compact=True)

    assert "не придумывай и не уточняй её самостоятельно" in context.lower()


# ── sources: source_name + real URL only, never invented ────────────────

def test_context_lists_signal_source_name_and_url() -> None:
    context = _signal_intent_context(
        [_signal(source_name="Гогов", url="https://gogov.ru/covid19/travel")],
        workspace_only=False, compact=True,
    )

    assert "источник: Гогов" in context
    assert "URL: https://gogov.ru/covid19/travel" in context


def test_context_never_shows_url_placeholder_when_signal_has_none() -> None:
    context = _signal_intent_context(
        [_signal(source_name="Турпром", url="")],
        workspace_only=False, compact=True,
    )

    assert "источник: Турпром" in context
    assert "URL:" not in context


def test_context_forbids_inventing_a_missing_url() -> None:
    context = _signal_intent_context([_signal()], workspace_only=False, compact=True)

    assert "не придумывай ссылку" in context.lower()


# ── fact vs idea: explicit separation rule ───────────────────────────────

def test_context_requires_explicit_fact_vs_idea_separation() -> None:
    context = _signal_intent_context([_signal()], workspace_only=False, compact=True)

    lowered = context.lower()
    assert "факт из источника" in lowered
    assert "идея оркестратора" in lowered
    assert "не смешивай факт и интерпретацию" in lowered


# ── compact shape: 3-5 signals, one idea each, no long report ────────────

def test_compact_context_asks_for_3_to_5_signals_and_one_idea_each() -> None:
    context = _signal_intent_context([_signal()], workspace_only=True, compact=True)

    lowered = context.lower()
    assert "3-5" in lowered
    assert "ровно одну идею" in lowered
    assert "не превращай ответ в развёрнутый" in lowered


# ── workspace-only rule still present alongside the new date/source rules ──

def test_workspace_only_rule_still_present() -> None:
    context = _signal_intent_context([_signal()], workspace_only=True, compact=True)

    assert "не используй результаты" in context.lower()
    assert "общего веб-поиска" in context.lower()
