"""Bug 4: signal-shaped Assistant queries ("какие сейчас самые важные
рыночные сигналы и идеи для контента?", "что сейчас важно на рынке", "дай
идеи из моих источников") must consult the workspace's own connected-source
Signal Service (the same app.services.signal_service feed behind GET
/api/signals) before/alongside generic Web Search - not answer purely from
generic LLM knowledge or web search.

Requires the web-only dependencies (requirements-web.txt: fastapi, uvicorn,
markdown). Skips cleanly when they're not installed.
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
    return SimpleNamespace(
        recommend_action=lambda row: {
            "recommended_action": "content", "action_reason": "Причина",
        },
        action_label=lambda action: "Тема для контента",
    )


def _fake_generate(text="Ответ ассистента", captured=None):
    def _generate(**kwargs):
        if captured is not None:
            captured.update(kwargs)
        return ChatResult(text=text, usage=None)
    return _generate


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


def test_signal_intent_query_injects_workspace_signals_into_prompt(api) -> None:
    client, web_api, db_path, radar_db_path, workspace_id = api
    _create_radar_db(radar_db_path, [
        _radar_row(1, source_id="src-1", category="content_signal"),
    ])
    _add_active_source_subscription(db_path, workspace_id=workspace_id, source_id="src-1")

    captured = {}
    conv_id = _new_conversation(client)

    with patch("app.services.lead_radar._load_recommender", return_value=_fake_recommender()):
        with patch.object(web_api.chat_provider, "generate", _fake_generate(captured=captured)):
            response = client.post(
                "/api/chat",
                json={
                    "conversation_id": conv_id,
                    "message": "Какие сейчас самые важные рыночные сигналы и идеи для контента?",
                },
            )

    assert response.status_code == 200
    assert "Уникальный маркер сигнала XYZ123" in captured["knowledge_context"]
    assert "СИГНАЛЫ ИЗ ПОДКЛЮЧЁННЫХ ИСТОЧНИКОВ" in captured["knowledge_context"]


def test_non_signal_query_does_not_inject_signal_context(api) -> None:
    client, web_api, db_path, radar_db_path, workspace_id = api
    _create_radar_db(radar_db_path, [
        _radar_row(1, source_id="src-1", category="content_signal"),
    ])
    _add_active_source_subscription(db_path, workspace_id=workspace_id, source_id="src-1")

    captured = {}
    conv_id = _new_conversation(client)

    with patch("app.services.lead_radar._load_recommender", return_value=_fake_recommender()):
        with patch.object(web_api.chat_provider, "generate", _fake_generate(captured=captured)):
            response = client.post(
                "/api/chat",
                json={
                    "conversation_id": conv_id,
                    "message": "Помоги составить программу тура по Турции на 5 дней.",
                },
            )

    assert response.status_code == 200
    assert "Уникальный маркер сигнала XYZ123" not in (captured.get("knowledge_context") or "")
