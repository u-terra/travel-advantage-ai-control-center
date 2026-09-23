"""Fix: "используй мои подключённые источники" / "из моих источников" /
"по моим сигналам" must skip generic Web Search entirely - it must never
run, and the Assistant must never append generic sites (vc.ru, Neil
Patel, TexTerra, ...) to the answer's sources when the user explicitly
asked for workspace-only signals.

Requires the web-only dependencies (requirements-web.txt: fastapi,
uvicorn, markdown). Skips cleanly when they're not installed.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("markdown")

from fastapi.testclient import TestClient  # noqa: E402

from app.chat_provider import ChatResult  # noqa: E402
from app.services.web_search.base import SearchResponse, SearchResult, WebSearchProvider  # noqa: E402
from app.services.web_search.service import WebSearchService  # noqa: E402
from app.web_api import _is_workspace_only_request, _wants_long_content_plan  # noqa: E402
from tests._web_auth_test_helpers import login_as  # noqa: E402

OWNER_ID = 586249067


def _run(coro):
    return asyncio.run(coro)


def _now_iso(hours_ago: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _create_radar_db(path: Path, rows: list[dict]) -> None:
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE lead_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT, created_at TEXT,
            source_type TEXT, origin_type TEXT, source_name TEXT, source_url TEXT,
            item_url TEXT UNIQUE, item_title TEXT, item_summary TEXT, published_at TEXT,
            status TEXT DEFAULT 'new', ai_score REAL, ai_category TEXT, ai_reason TEXT,
            suggested_message TEXT, notes TEXT, llm_checked INTEGER DEFAULT 0,
            llm_checked_at TEXT, llm_signal_type TEXT, llm_score REAL,
            llm_relevance TEXT, llm_reason TEXT, llm_suggested_message TEXT
        )""")
        for row in rows:
            db.execute(
                "INSERT INTO lead_signals(id, source_id, source_name, created_at, "
                "source_type, origin_type, item_url, item_title, item_summary, "
                "status, notes, ai_score, ai_category, ai_reason, suggested_message, "
                "llm_checked, llm_checked_at, llm_signal_type, llm_score, "
                "llm_relevance, llm_reason, llm_suggested_message) "
                "VALUES (:id, :source_id, :source_name, :created_at, :source_type, "
                ":origin_type, :item_url, :item_title, :item_summary, 'new', '', "
                ":ai_score, :ai_category, :ai_reason, NULL, 0, NULL, NULL, NULL, "
                "NULL, NULL, NULL)",
                row,
            )
        db.commit()


def _radar_row(row_id: int, *, source_id: str, category: str, **overrides) -> dict:
    base = dict(
        id=row_id, source_id=source_id, source_name=f"Источник {source_id}",
        created_at=_now_iso(1.0), source_type="rss", origin_type="publisher_post",
        item_url=f"https://example.org/item/{row_id}",
        item_title="Уникальный маркер сигнала XYZ123",
        item_summary="summary", ai_score=64.0, ai_category=category,
        ai_reason=f"Причина {row_id}",
    )
    base.update(overrides)
    return base


def _ensure_source_catalog_schema(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS source_catalog (
        id TEXT PRIMARY KEY, identity_key TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
        platform TEXT NOT NULL, url TEXT, username TEXT, source_type TEXT NOT NULL,
        purpose TEXT NOT NULL, priority INTEGER NOT NULL, notes TEXT NOT NULL,
        collector_json TEXT NOT NULL,
        visibility TEXT NOT NULL CHECK (visibility IN ('platform', 'private')),
        owner_workspace_id INTEGER,
        status TEXT NOT NULL CHECK (status IN ('active', 'inactive')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        FOREIGN KEY (owner_workspace_id) REFERENCES partner_workspaces(id),
        CHECK (visibility != 'private' OR owner_workspace_id IS NOT NULL)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS workspace_source_subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL,
        source_id TEXT NOT NULL, enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
        usage_role TEXT NOT NULL DEFAULT 'monitoring'
            CHECK (usage_role IN ('monitoring', 'competitor')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        FOREIGN KEY (workspace_id) REFERENCES partner_workspaces(id),
        FOREIGN KEY (source_id) REFERENCES source_catalog(id),
        UNIQUE (workspace_id, source_id)
    )""")


def _add_active_source_subscription(db_path: Path, *, workspace_id: int, source_id: str) -> None:
    now = "2026-01-01T00:00:00+00:00"
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA foreign_keys = ON")
        _ensure_source_catalog_schema(db)
        db.execute(
            "INSERT INTO source_catalog "
            "(id, identity_key, name, platform, url, username, source_type, purpose, "
            "priority, notes, collector_json, visibility, owner_workspace_id, status, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, 'vk', 'https://vk.com/x', NULL, 'community', 'monitoring', "
            "1, '', '{}', 'platform', NULL, 'active', ?, ?)",
            (source_id, f"identity:{source_id}", source_id, now, now),
        )
        db.execute(
            "INSERT INTO workspace_source_subscriptions "
            "(workspace_id, source_id, enabled, usage_role, created_at, updated_at) "
            "VALUES (?, ?, 1, 'monitoring', ?, ?)",
            (workspace_id, source_id, now, now),
        )
        db.commit()


def _fake_recommender():
    # "observe" (not "content") deliberately sidesteps a pre-existing,
    # unrelated bug in app.services.lead_radar.build_workspace_signals /
    # signal_service.build_unified_feed (content_angle_hint() takes no
    # args but is called with one for recommended_action == "content") -
    # out of scope for this fix, which only touches Web Assistant routing.
    return SimpleNamespace(
        recommend_action=lambda row: {
            "recommended_action": "observe", "action_reason": "Причина",
        },
        action_label=lambda action: "Наблюдать",
    )


def _fake_generate(text="Ответ ассистента", captured=None):
    def _generate(**kwargs):
        if captured is not None:
            captured.update(kwargs)
        return ChatResult(text=text, usage=None)
    return _generate


class _FakeSearchProvider(WebSearchProvider):
    name = "fake_yandex"

    def __init__(self, response: SearchResponse | None):
        self._response = response
        self.calls: list[tuple] = []

    def search(self, query, *, site=None, limit=5, search_type=None, allow_exceeding_configured_max=False):
        self.calls.append((query, site, limit))
        return self._response


def _generic_search_response() -> SearchResponse:
    """Stand-in for the exact bug report: generic marketing sites showing
    up as "Источники" for a workspace-signals question."""
    return SearchResponse(
        query="market",
        results=[
            SearchResult(
                title="Тренды контента 2026", url="https://vc.ru/marketing/trends",
                snippet="...", domain="vc.ru", published_at=None,
                provider="fake_yandex", rank=1,
            ),
        ],
        provider="fake_yandex", elapsed_ms=10,
    )


@pytest.fixture
def api(tmp_path, monkeypatch):
    db_path = tmp_path / "journal.sqlite3"
    radar_db_path = tmp_path / "leads.db"
    monkeypatch.setenv("JOURNAL_DB_PATH", str(db_path))
    monkeypatch.setenv("PLANNER_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("LEAD_RADAR_DB_PATH", str(radar_db_path))
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "false")

    import sys
    sys.modules.pop("app.web_api", None)
    import app.web_api as web_api

    with TestClient(web_api.app, base_url="https://testserver") as client:
        ws, _ = _run(web_api.partner_repository.ensure_owner_workspace(OWNER_ID))
        login_as(client, web_api, ws.id, OWNER_ID)
        yield client, web_api, db_path, radar_db_path, ws.id

    sys.modules.pop("app.web_api", None)


def _new_conversation(client) -> int:
    return client.post("/api/conversations").json()["conversation"]["id"]


# ── routing: keyword detection ──────────────────────────────────────────

def test_is_workspace_only_request_detects_explicit_phrasing() -> None:
    assert _is_workspace_only_request(
        "Какие сейчас самые важные рыночные сигналы и идеи для контента? "
        "Используй мои подключённые источники."
    )
    assert _is_workspace_only_request("Дай идеи из моих источников")
    assert _is_workspace_only_request("Что важно по моим сигналам?")
    assert not _is_workspace_only_request("Помоги составить программу тура по Турции")


def test_wants_long_content_plan_detects_explicit_plan_request() -> None:
    assert _wants_long_content_plan("Составь контент-план на неделю")
    assert not _wants_long_content_plan("Какие сейчас самые важные сигналы?")


# ── endpoint: explicit workspace-only request never triggers generic search ─

def test_workspace_only_request_skips_generic_web_search_and_its_sources(api, monkeypatch) -> None:
    client, web_api, db_path, radar_db_path, workspace_id = api
    _create_radar_db(radar_db_path, [
        _radar_row(1, source_id="src-1", category="content_signal"),
    ])
    _add_active_source_subscription(db_path, workspace_id=workspace_id, source_id="src-1")

    fake_provider = _FakeSearchProvider(_generic_search_response())
    monkeypatch.setattr(web_api, "web_search_service", WebSearchService(fake_provider, enabled=True))

    captured: dict = {}
    conv_id = _new_conversation(client)

    with patch("app.services.lead_radar._load_recommender", return_value=_fake_recommender()):
        with patch.object(web_api.chat_provider, "generate", _fake_generate(captured=captured)):
            response = client.post(
                "/api/chat",
                json={
                    "conversation_id": conv_id,
                    "message": (
                        "Какие сейчас самые важные рыночные сигналы и идеи для "
                        "контента? Используй мои подключённые источники."
                    ),
                },
            )

    assert response.status_code == 200
    body = response.json()

    # Generic Web Search must never even run for this explicit request.
    assert fake_provider.calls == []
    assert body["search_sources"] == []
    assert "vc.ru" not in body["answer"]

    knowledge_context = captured.get("knowledge_context") or ""
    assert "АКТУАЛЬНЫЙ ПОИСК" not in knowledge_context
    assert "СИГНАЛЫ ИЗ ПОДКЛЮЧЁННЫХ ИСТОЧНИКОВ" in knowledge_context
    assert "Уникальный маркер сигнала XYZ123" in knowledge_context
    # The workspace-only + no-invented-sources rules must reach the model.
    assert "НЕ используй результаты" in knowledge_context
    assert "не придумывай ссылку" in knowledge_context


def test_non_workspace_only_signal_query_still_allows_generic_search(api, monkeypatch) -> None:
    """A signal-shaped question that does NOT explicitly say "мои
    источники" still goes through the normal decide_web_search() gate -
    this fix only changes behavior for the explicit opt-out phrasing."""
    client, web_api, db_path, radar_db_path, workspace_id = api
    _create_radar_db(radar_db_path, [
        _radar_row(1, source_id="src-1", category="content_signal"),
    ])
    _add_active_source_subscription(db_path, workspace_id=workspace_id, source_id="src-1")

    fake_provider = _FakeSearchProvider(_generic_search_response())
    monkeypatch.setattr(web_api, "web_search_service", WebSearchService(fake_provider, enabled=True))

    captured: dict = {}
    conv_id = _new_conversation(client)

    with patch("app.services.lead_radar._load_recommender", return_value=_fake_recommender()):
        with patch.object(web_api.chat_provider, "generate", _fake_generate(captured=captured)):
            response = client.post(
                "/api/chat",
                json={
                    "conversation_id": conv_id,
                    "message": "Какие сейчас самые важные рыночные сигналы?",
                },
            )

    assert response.status_code == 200
    # decide_web_search() judges this a "market" query -> search does run.
    assert len(fake_provider.calls) >= 1
