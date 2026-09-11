"""GET /api/signals - read-only Radar signals listing for the web shell.

Reuses the exact same data path as the Telegram on_find_signals() handler
(app/handlers/menu.py): WorkspaceSignalRepository.list_for_workspace() then
build_workspace_signals() from app/services/lead_radar.py - no parallel
Radar model, no new filtering/scoring logic. The only deliberate difference
is that this endpoint does NOT call sync_eligible(): that's a write step
(materializes new interpretation rows) owned by the bot process, and this
endpoint must stay strictly read-only. Tests below simulate "the bot
already synced this data" by calling sync_eligible() directly on the
repository during setup, exactly like app/main.py does at bot startup.

Requires the web-only dependencies (requirements-web.txt: fastapi,
uvicorn, markdown) that app/web_api.py imports at module level. Skips
cleanly instead of failing the whole suite when they're not installed
(base requirements.txt does not include them).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("markdown")

from fastapi.testclient import TestClient  # noqa: E402

from tests._web_auth_test_helpers import login_as  # noqa: E402
from app.services.lead_radar import DISPLAY_LIMIT  # noqa: E402

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


def _radar_row(
    row_id: int, *, source_id: str, category: str, hours_ago: float = 1.0, **overrides
) -> dict:
    base = dict(
        id=row_id, source_id=source_id, source_name=f"Источник {source_id}",
        created_at=_now_iso(hours_ago), source_type="rss", origin_type="publisher_post",
        item_url=f"https://example.org/item/{row_id}", item_title=f"Заголовок {row_id}",
        item_summary="summary", ai_score=64.0, ai_category=category,
        ai_reason=f"Причина {row_id}",
    )
    base.update(overrides)
    return base


def _ensure_source_catalog_schema(db) -> None:
    """web_api.py never constructs SourceCatalogRepository (out of scope for
    the web signals slice), so these tables don't exist yet in the journal
    DB - create them here, matching app/repositories/source_catalog_repository.py."""
    db.execute("""CREATE TABLE IF NOT EXISTS source_catalog (
        id TEXT PRIMARY KEY,
        identity_key TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        platform TEXT NOT NULL,
        url TEXT,
        username TEXT,
        source_type TEXT NOT NULL,
        purpose TEXT NOT NULL,
        priority INTEGER NOT NULL,
        notes TEXT NOT NULL,
        collector_json TEXT NOT NULL,
        visibility TEXT NOT NULL CHECK (visibility IN ('platform', 'private')),
        owner_workspace_id INTEGER,
        status TEXT NOT NULL CHECK (status IN ('active', 'inactive')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (owner_workspace_id) REFERENCES partner_workspaces(id),
        CHECK (visibility != 'private' OR owner_workspace_id IS NOT NULL)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS workspace_source_subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        workspace_id INTEGER NOT NULL,
        source_id TEXT NOT NULL,
        enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
        usage_role TEXT NOT NULL DEFAULT 'monitoring'
            CHECK (usage_role IN ('monitoring', 'competitor')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (workspace_id) REFERENCES partner_workspaces(id),
        FOREIGN KEY (source_id) REFERENCES source_catalog(id),
        UNIQUE (workspace_id, source_id)
    )""")


def _add_active_source_subscription(
    db_path: Path, *, workspace_id: int, source_id: str, source_name: str,
) -> None:
    """Direct SQL (mirrors source_catalog/workspace_source_subscriptions schema
    in app/repositories/source_catalog_repository.py) - the request/approval
    workflow isn't the concern of this test, only the resulting active,
    subscribed state that make a synced signal visible."""
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
            (source_id, f"identity:{source_id}", source_name, now, now),
        )
        db.execute(
            "INSERT INTO workspace_source_subscriptions "
            "(workspace_id, source_id, enabled, usage_role, created_at, updated_at) "
            "VALUES (?, ?, 1, 'monitoring', ?, ?)",
            (workspace_id, source_id, now, now),
        )
        db.commit()


_ACTION_BY_CATEGORY = {
    "lead_signal": "careful_reply",
    "market_signal": "observe",
    "content_signal": "content",
}


def _fake_recommender():
    return SimpleNamespace(
        recommend_action=lambda row: {
            "recommended_action": _ACTION_BY_CATEGORY.get(row.get("ai_category"), "skip"),
            "action_reason": f"Причина: {row.get('item_title')}",
        },
        action_label=lambda action: {
            "careful_reply": "Ответить лично",
            "observe": "Наблюдать",
            "content": "Тема для контента",
        }.get(action, action),
    )


@pytest.fixture
def api(tmp_path, monkeypatch):
    db_path = tmp_path / "journal.sqlite3"
    radar_db_path = tmp_path / "leads.db"
    monkeypatch.setenv("JOURNAL_DB_PATH", str(db_path))
    monkeypatch.setenv("PLANNER_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("LEAD_RADAR_DB_PATH", str(radar_db_path))

    import sys
    sys.modules.pop("app.web_api", None)
    import app.web_api as web_api

    with TestClient(web_api.app, base_url="https://testserver") as client:
        ws, _ = _run(web_api.partner_repository.ensure_owner_workspace(OWNER_ID))
        login_as(client, web_api, ws.id, OWNER_ID)
        # In production, app/main.py (the bot process) already initializes
        # SourceCatalogRepository against this same shared journal DB before
        # the web process ever runs - list_for_workspace() unconditionally
        # queries source_catalog/workspace_source_subscriptions even for a
        # workspace with zero synced signals. web_api.py itself never
        # constructs that repository (out of scope for this read-only
        # signals slice), so tests recreate just the schema here.
        with sqlite3.connect(db_path) as db:
            _ensure_source_catalog_schema(db)
            db.commit()
        yield client, web_api, db_path, radar_db_path, ws.id

    sys.modules.pop("app.web_api", None)


def _sync(web_api) -> None:
    _run(web_api.workspace_signal_repository.sync_eligible())


def test_no_radar_db_file_returns_unavailable_error_not_500(api) -> None:
    """LEAD_RADAR_DB_PATH pointing at nothing (e.g. local/dev without the
    Radar deployment) must degrade to the same 'unavailable' shape Telegram
    uses (unavailable_summary()), never a 500."""
    client, web_api, _, radar_db_path, workspace_id = api
    assert not radar_db_path.exists()

    response = client.get("/api/signals")

    assert response.status_code == 200
    body = response.json()
    assert body["signals"] == []
    assert "error" in body


def test_empty_workspace_with_no_synced_signals_returns_empty_list(api) -> None:
    client, web_api, db_path, radar_db_path, workspace_id = api
    _create_radar_db(radar_db_path, [])

    with patch("app.services.lead_radar._load_recommender", return_value=_fake_recommender()):
        response = client.get("/api/signals")

    assert response.status_code == 200
    assert response.json() == {"signals": []}


def test_returns_real_synced_signal_with_required_card_fields(api) -> None:
    client, web_api, db_path, radar_db_path, workspace_id = api
    _create_radar_db(radar_db_path, [
        _radar_row(1, source_id="src-1", category="market_signal", hours_ago=2.0),
    ])
    _add_active_source_subscription(
        db_path, workspace_id=workspace_id,
        source_id="src-1", source_name="VK: Путешествия",
    )
    _sync(web_api)

    with patch("app.services.lead_radar._load_recommender", return_value=_fake_recommender()):
        response = client.get("/api/signals")

    assert response.status_code == 200
    body = response.json()
    assert len(body["signals"]) == 1
    signal = body["signals"][0]
    # заголовок/тема, тип сигнала, источник, дата/свежесть, score - все реальные
    assert signal["title"] == "Заголовок 1"
    assert signal["category"] == "market_signal"
    assert signal["category_label"] == "👀 Наблюдать рынок"
    # source_name is the Radar-reported name from lead_signals itself (via
    # WorkspaceSignalRecord.source_name), not source_catalog's editorial
    # name - source_catalog only gates visibility here, it isn't the label.
    assert signal["source_name"] == "Источник src-1"
    assert signal["created_at"]  # ISO timestamp, formatted client-side
    assert signal["score"] == 64.0
    assert signal["url"] == "https://example.org/item/1"
    assert "id" in signal


def test_signal_without_stored_score_reports_null_not_fabricated(api) -> None:
    client, web_api, db_path, radar_db_path, workspace_id = api
    _create_radar_db(radar_db_path, [
        _radar_row(1, source_id="src-1", category="market_signal", ai_score=None),
    ])
    _add_active_source_subscription(
        db_path, workspace_id=workspace_id,
        source_id="src-1", source_name="VK: Путешествия",
    )
    _sync(web_api)

    with patch("app.services.lead_radar._load_recommender", return_value=_fake_recommender()):
        response = client.get("/api/signals")

    assert response.json()["signals"][0]["score"] is None


def test_signal_from_disabled_source_is_not_shown(api) -> None:
    """list_for_workspace() already hides interpretations whose source has
    since been disabled/deactivated - this endpoint must not bypass that."""
    client, web_api, db_path, radar_db_path, workspace_id = api
    _create_radar_db(radar_db_path, [
        _radar_row(1, source_id="src-1", category="market_signal"),
    ])
    _add_active_source_subscription(
        db_path, workspace_id=workspace_id,
        source_id="src-1", source_name="VK: Путешествия",
    )
    _sync(web_api)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE workspace_source_subscriptions SET enabled = 0 "
            "WHERE workspace_id = ? AND source_id = ?",
            (workspace_id, "src-1"),
        )
        db.commit()

    with patch("app.services.lead_radar._load_recommender", return_value=_fake_recommender()):
        response = client.get("/api/signals")

    assert response.json() == {"signals": []}


def test_only_current_web_workspace_signals_are_returned(api) -> None:
    client, web_api, db_path, radar_db_path, workspace_id = api
    _create_radar_db(radar_db_path, [
        _radar_row(1, source_id="src-mine", category="market_signal"),
        _radar_row(2, source_id="src-other", category="market_signal"),
    ])
    other = _run(web_api.partner_repository.provision_partner(
        222333555, "Other Agency", "other-agency-signals",
        business_name="Other Agency", business_type="independent_agent",
        short_description="Другое рабочее пространство.",
        context={},
    ))
    _add_active_source_subscription(
        db_path, workspace_id=workspace_id,
        source_id="src-mine", source_name="Моя подписка",
    )
    _add_active_source_subscription(
        db_path, workspace_id=other.workspace.id,
        source_id="src-other", source_name="Чужая подписка",
    )
    _sync(web_api)

    with patch("app.services.lead_radar._load_recommender", return_value=_fake_recommender()):
        response = client.get("/api/signals")

    titles = [item["title"] for item in response.json()["signals"]]
    assert titles == ["Заголовок 1"]


def test_endpoint_does_not_write_new_interpretation_rows_itself(api) -> None:
    """Read-only requirement: GET /api/signals must not materialize new
    interpretations on its own - only what sync_eligible() (owned by the bot
    process) already synced should ever be visible."""
    client, web_api, db_path, radar_db_path, workspace_id = api
    _create_radar_db(radar_db_path, [
        _radar_row(1, source_id="src-1", category="market_signal"),
    ])
    _add_active_source_subscription(
        db_path, workspace_id=workspace_id,
        source_id="src-1", source_name="VK: Путешествия",
    )
    # Deliberately NOT calling sync_eligible() here.

    with patch("app.services.lead_radar._load_recommender", return_value=_fake_recommender()):
        response = client.get("/api/signals")

    assert response.json() == {"signals": []}
    with sqlite3.connect(db_path) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM workspace_signal_interpretations"
        ).fetchone()[0]
    assert count == 0


def test_noise_and_stale_signals_are_excluded_same_as_telegram(api) -> None:
    """No parallel filtering policy - the same _is_allowed_row()/freshness
    rules from app/services/lead_radar.py apply here (noise category and
    signals older than the per-action freshness window are dropped)."""
    client, web_api, db_path, radar_db_path, workspace_id = api
    _create_radar_db(radar_db_path, [
        _radar_row(1, source_id="src-1", category="noise"),
        # "observe" freshness window is 7 days (168h); 10 days is well past
        # it but still inside the 30-day _is_fresh() cutoff, so this
        # specifically exercises the per-action freshness check.
        _radar_row(2, source_id="src-1", category="market_signal", hours_ago=24 * 10),
        _radar_row(3, source_id="src-1", category="market_signal", hours_ago=1.0),
    ])
    _add_active_source_subscription(
        db_path, workspace_id=workspace_id,
        source_id="src-1", source_name="VK: Путешествия",
    )
    _sync(web_api)

    with patch("app.services.lead_radar._load_recommender", return_value=_fake_recommender()):
        response = client.get("/api/signals")

    titles = [item["title"] for item in response.json()["signals"]]
    assert titles == ["Заголовок 3"]


def test_signals_endpoint_returns_up_to_display_limit_not_hardcoded_one_plus_one(api) -> None:
    """/api/signals must use the same DISPLAY_LIMIT as the Telegram handler
    (app/handlers/menu.py), not a stale hardcoded 5. Builds exactly enough
    eligible rows (3 careful_reply + 3 observe + 5 content = 11) to fill every
    quota bucket at once, then asserts the endpoint returns DISPLAY_LIMIT (10)
    - not the old 1 market + 1 content, and not all 11 candidates either."""
    client, web_api, db_path, radar_db_path, workspace_id = api
    rows = (
        [_radar_row(i, source_id="src-1", category="lead_signal", hours_ago=i)
         for i in range(1, 4)]
        + [_radar_row(i, source_id="src-1", category="market_signal", hours_ago=i)
           for i in range(10, 13)]
        + [_radar_row(i, source_id="src-1", category="content_signal", hours_ago=i)
           for i in range(20, 25)]
    )
    assert len(rows) == 11
    _create_radar_db(radar_db_path, rows)
    _add_active_source_subscription(
        db_path, workspace_id=workspace_id,
        source_id="src-1", source_name="VK: Путешествия",
    )
    _sync(web_api)

    with patch("app.services.lead_radar._load_recommender", return_value=_fake_recommender()):
        response = client.get("/api/signals")

    signals = response.json()["signals"]
    assert DISPLAY_LIMIT == 10
    assert len(signals) == DISPLAY_LIMIT
    categories = [s["category"] for s in signals]
    assert categories.count("lead_signal") == 3
    assert categories.count("market_signal") == 3
    # Квота content — 5, но сумма квот (11) больше DISPLAY_LIMIT (10), поэтому
    # самый старый content_signal обрезается итоговым срезом — см. тот же
    # компромисс в tests/test_lead_radar.py::
    # test_overall_cap_trims_last_content_item_when_all_quotas_are_full.
    assert categories.count("content_signal") == 4
