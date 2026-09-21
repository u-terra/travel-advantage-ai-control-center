"""Signal / competitor -> material: "Создать материал" actions.

The key product chain: signal or competitor opportunity -> explanation ->
suggested action -> "Создать материал" -> a real Artifact, generated
through the SAME MaterialOrchestrationService + competitor_llm_provider
(Content Factory) that Telegram already uses for Radar
(app/handlers/menu.py) and competitor opportunities
(app/handlers/competitors.py) - no second generator, no second LLM
provider. See POST /api/signals/{id}/actions and
POST /api/competitors/{id}/opportunities/{id}/actions in app/web_api.py.

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

from tests._web_auth_test_helpers import login_as  # noqa: E402

OWNER_ID = 586249067


def _run(coro):
    return asyncio.run(coro)


def _now_iso(hours_ago: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


# ── Radar signal fixtures (mirrors tests/test_web_api_signals.py) ──────────

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


def _radar_row(row_id: int, *, source_id: str, category: str, hours_ago: float = 1.0, **overrides) -> dict:
    base = dict(
        id=row_id, source_id=source_id, source_name=f"Источник {source_id}",
        created_at=_now_iso(hours_ago), source_type="rss", origin_type="publisher_post",
        item_url=f"https://example.org/item/{row_id}", item_title=f"Раннее бронирование Турции {row_id}",
        item_summary=(
            "Цена от 45000 рублей до конца месяца, доступны прямые рейсы из "
            "Москвы и Санкт-Петербурга каждую субботу."
        ),
        ai_score=64.0, ai_category=category,
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


def _add_active_source_subscription(db_path: Path, *, workspace_id: int, source_id: str, source_name: str) -> None:
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


def _fake_recommender():
    return SimpleNamespace(
        recommend_action=lambda row: {"recommended_action": "content", "action_reason": f"Причина: {row.get('item_title')}"},
        action_label=lambda action: {"content": "Тема для контента"}.get(action, action),
    )


def _sync(web_api) -> None:
    _run(web_api.workspace_signal_repository.sync_eligible())


def _fake_analysis(**overrides):
    from app.services.llm.models import SourceAnalysisPayload

    base = dict(
        summary="summary", key_facts=(), disputed_claims=(), audience_value="",
        target_audiences=(), content_angles=(), recommended_formats=(), warnings=(),
    )
    base.update(overrides)
    return SourceAnalysisPayload(**base)


def _fake_draft(text: str = "Готовый черновик поста."):
    from app.services.llm.models import ContentDraft

    return ContentDraft(text=text, warnings=())


# ── shared fixture ──────────────────────────────────────────────────────────

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
        with sqlite3.connect(db_path) as db:
            _ensure_source_catalog_schema(db)
            db.commit()
        yield client, web_api, db_path, radar_db_path, ws.id

    sys.modules.pop("app.web_api", None)


def _make_signal(web_api, db_path, radar_db_path, workspace_id, *, row_id: int = 1) -> None:
    _create_radar_db(radar_db_path, [
        _radar_row(row_id, source_id=f"src-{row_id}", category="market_signal"),
    ])
    _add_active_source_subscription(
        db_path, workspace_id=workspace_id, source_id=f"src-{row_id}", source_name="VK: Путешествия",
    )
    _sync(web_api)


# ── POST /api/signals/{id}/actions ──────────────────────────────────────────

def test_signal_action_creates_a_real_material(api, monkeypatch) -> None:
    client, web_api, db_path, radar_db_path, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
    _make_signal(web_api, db_path, radar_db_path, workspace_id)

    monkeypatch.setattr(web_api.competitor_llm_provider, "analyze_source", lambda **kw: _fake_analysis())
    monkeypatch.setattr(web_api.competitor_llm_provider, "generate_draft", lambda **kw: _fake_draft())

    with patch("app.services.lead_radar._load_recommender", return_value=_fake_recommender()):
        response = client.post("/api/signals/1/actions", json={"action": "post"})

    assert response.status_code == 200
    body = response.json()
    assert "error" not in body
    assert body["material"]["artifact_type"] == "post"
    assert body["version"]["content"] == "Готовый черновик поста."
    assert body["origin"]["kind"] == "signal"

    materials = _run(web_api.artifact_repository.list_artifacts(workspace_id, limit=10))
    assert len(materials) == 1
    assert materials[0].id == body["material"]["id"]


def test_signal_action_does_not_use_a_truncated_title_as_material_title(api, monkeypatch) -> None:
    """Quality fix: a forwarded-post signal title cut off by the original
    author ("...осенние свитера и куртки, другие достают загранпаспорт...")
    must not become the Artifact/Material's own title verbatim - no LLM
    call to invent a replacement, just a neutral fallback."""
    client, web_api, db_path, radar_db_path, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
    truncated_title = (
        "Пока одни достают осенние свитера и куртки, другие достают "
        "загранпаспорт..."
    )
    _create_radar_db(radar_db_path, [
        _radar_row(
            1, source_id="src-1", category="market_signal",
            item_title=truncated_title,
            item_summary=(
                "Спрос на туры в Грузию вырос на 30% за последний месяц, "
                "путешественники бронируют туры на ноябрьские праздники."
            ),
        ),
    ])
    _add_active_source_subscription(
        db_path, workspace_id=workspace_id, source_id="src-1", source_name="Tripster",
    )
    _sync(web_api)

    monkeypatch.setattr(web_api.competitor_llm_provider, "analyze_source", lambda **kw: _fake_analysis())
    monkeypatch.setattr(web_api.competitor_llm_provider, "generate_draft", lambda **kw: _fake_draft())

    with patch("app.services.lead_radar._load_recommender", return_value=_fake_recommender()):
        response = client.post("/api/signals/1/actions", json={"action": "post"})

    assert response.status_code == 200
    body = response.json()
    assert "error" not in body
    assert body["material"]["title"] != truncated_title
    assert not body["material"]["title"].endswith("...")


def test_web_source_signal_action_uses_the_same_shared_material_service(api, monkeypatch) -> None:
    """Stage 2/3 web-source signal ("web:<id>") must go through the exact
    same MaterialOrchestrationService.build_radar_generation_spec +
    competitor_llm_provider pipeline as a Radar signal - no second
    generator, and it must actually succeed instead of the old hardcoded
    'Подготовка поста по сигналам с сайта пока недоступна.'"""
    from app.repositories.web_signal_repository import WebSignalRecord

    client, web_api, db_path, radar_db_path, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
    _create_radar_db(radar_db_path, [])
    _add_active_source_subscription(
        db_path, workspace_id=workspace_id, source_id="trip", source_name="Trip.com",
    )
    _run(web_api.web_signal_repository.save_many([
        WebSignalRecord(
            workspace_id=workspace_id, source_id="trip", source_name="Trip.com",
            source_url="https://trip.example", item_url="https://trip.example/article-1",
            title="Дешёвые билеты в Стамбул",
            summary=(
                "Билеты Москва-Стамбул подешевели до 8000 рублей туда-обратно, "
                "акция действует до конца месяца."
            ),
            fetched_at=_now_iso(1.0), published_at=_now_iso(1.0),
        ),
    ]))
    [record] = _run(web_api.web_signal_repository.list_for_workspace(workspace_id))

    monkeypatch.setattr(web_api.competitor_llm_provider, "analyze_source", lambda **kw: _fake_analysis())
    monkeypatch.setattr(web_api.competitor_llm_provider, "generate_draft", lambda **kw: _fake_draft())

    response = client.post(f"/api/signals/web:{record.id}/actions", json={"action": "post"})

    assert response.status_code == 200
    body = response.json()
    assert "error" not in body
    assert body["material"]["artifact_type"] == "post"
    assert body["version"]["content"] == "Готовый черновик поста."
    assert body["origin"]["source_name"] == "Trip.com"

    materials = _run(web_api.artifact_repository.list_artifacts(workspace_id, limit=10))
    assert len(materials) == 1


def test_web_source_signal_action_unknown_id_is_rejected(api) -> None:
    client, web_api, db_path, radar_db_path, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
    _create_radar_db(radar_db_path, [])

    response = client.post("/api/signals/web:999/actions", json={"action": "post"})

    assert response.status_code == 200
    assert response.json() == {"error": "Сигнал недоступен.", "material": None}


def test_signal_action_uses_the_correct_workspace_signal(api, monkeypatch) -> None:
    """The generated source_text must carry THIS signal's real title/summary
    (via SOURCE FACTS), not a fabricated or generic one - no re-typing the
    context by the user."""
    client, web_api, db_path, radar_db_path, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
    _make_signal(web_api, db_path, radar_db_path, workspace_id)

    captured = {}

    def fake_generate_draft(**kwargs):
        captured.update(kwargs)
        return _fake_draft()

    monkeypatch.setattr(web_api.competitor_llm_provider, "analyze_source", lambda **kw: _fake_analysis())
    monkeypatch.setattr(web_api.competitor_llm_provider, "generate_draft", fake_generate_draft)

    with patch("app.services.lead_radar._load_recommender", return_value=_fake_recommender()):
        response = client.post("/api/signals/1/actions", json={"action": "post"})

    assert response.status_code == 200
    assert "Раннее бронирование Турции 1" in captured["source_text"]
    assert "45000" in captured["source_text"]


def test_cannot_use_another_workspaces_signal(api, monkeypatch) -> None:
    client, web_api, db_path, radar_db_path, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))

    other = _run(web_api.partner_repository.provision_partner(
        222333999, "Other Agency", "other-agency-signal-actions",
        business_name="Other", business_type="independent_agent",
        short_description="x", context={},
    ))
    _create_radar_db(radar_db_path, [_radar_row(1, source_id="src-other", category="market_signal")])
    _add_active_source_subscription(
        db_path, workspace_id=other.workspace.id, source_id="src-other", source_name="Чужой",
    )
    _sync(web_api)

    called = {"count": 0}

    def fake_generate_draft(**kwargs):
        called["count"] += 1
        return _fake_draft()

    monkeypatch.setattr(web_api.competitor_llm_provider, "analyze_source", lambda **kw: _fake_analysis())
    monkeypatch.setattr(web_api.competitor_llm_provider, "generate_draft", fake_generate_draft)

    with patch("app.services.lead_radar._load_recommender", return_value=_fake_recommender()):
        response = client.post("/api/signals/1/actions", json={"action": "post"})

    assert response.status_code == 200
    assert response.json()["error"] == "Сигнал недоступен."
    assert called["count"] == 0
    assert _run(web_api.artifact_repository.list_artifacts(workspace_id, limit=10)) == []


def test_signal_action_rejects_unknown_action(api) -> None:
    client, web_api, db_path, radar_db_path, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
    _make_signal(web_api, db_path, radar_db_path, workspace_id)

    with patch("app.services.lead_radar._load_recommender", return_value=_fake_recommender()):
        response = client.post("/api/signals/1/actions", json={"action": "itinerary"})

    assert response.status_code == 200
    assert response.json() == {"error": "Неизвестное действие.", "material": None}


def test_signal_action_fails_closed_when_analysis_unavailable(api, monkeypatch) -> None:
    """Source Analysis Quality Gate: if analyze_source can't run, no draft is
    generated and no Artifact is created - same fail-closed rule Telegram's
    on_radar_content_selected() already enforces."""
    client, web_api, db_path, radar_db_path, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
    _make_signal(web_api, db_path, radar_db_path, workspace_id)

    monkeypatch.setattr(web_api.competitor_llm_provider, "analyze_source", lambda **kw: None)

    def fail_if_called(**kwargs):
        raise AssertionError("generate_draft must not be called when analysis is unavailable")

    monkeypatch.setattr(web_api.competitor_llm_provider, "generate_draft", fail_if_called)

    with patch("app.services.lead_radar._load_recommender", return_value=_fake_recommender()):
        response = client.post("/api/signals/1/actions", json={"action": "post"})

    assert response.status_code == 200
    assert "error" in response.json()
    assert _run(web_api.artifact_repository.list_artifacts(workspace_id, limit=10)) == []


def test_signal_action_fails_closed_when_source_content_is_too_thin(api, monkeypatch) -> None:
    """Quality fix: title+summary is the only source content this pipeline
    persists for a Radar signal - when it's too thin to write a concrete
    post from, fail closed with a clear status BEFORE calling
    analyze_source/generate_draft, instead of shipping a knowingly-weak
    draft (production example: "Пока одни достают осенние свитера...")."""
    client, web_api, db_path, radar_db_path, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
    _create_radar_db(radar_db_path, [
        _radar_row(
            1, source_id="src-1", category="market_signal",
            item_title="Коротко", item_summary="",
        ),
    ])
    _add_active_source_subscription(
        db_path, workspace_id=workspace_id, source_id="src-1", source_name="VK: Путешествия",
    )
    _sync(web_api)

    def fail_if_called(**kwargs):
        raise AssertionError("analyze_source/generate_draft must not be called for thin source content")

    monkeypatch.setattr(web_api.competitor_llm_provider, "analyze_source", fail_if_called)
    monkeypatch.setattr(web_api.competitor_llm_provider, "generate_draft", fail_if_called)

    with patch("app.services.lead_radar._load_recommender", return_value=_fake_recommender()):
        response = client.post("/api/signals/1/actions", json={"action": "post"})

    assert response.status_code == 200
    assert response.json()["error"] == "Недостаточно данных источника для качественного поста."
    assert _run(web_api.artifact_repository.list_artifacts(workspace_id, limit=10)) == []

    events = _run(web_api.operational_event_repository.list_recent_events(limit=200))
    matching = [
        e for e in events
        if e.event_type == "material_created_from_signal" and not e.success
    ]
    assert matching
    assert matching[0].error_code == "insufficient_source_content"


def test_signal_action_generate_draft_returning_none_is_diagnosable(api, monkeypatch) -> None:
    """Bug 5/3: generate_draft returning None (the LLM producing nothing
    usable) must be distinguishable from analysis_unavailable via
    error_code, and must not silently vanish into the same generic
    message/telemetry as every other failure."""
    client, web_api, db_path, radar_db_path, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
    _make_signal(web_api, db_path, radar_db_path, workspace_id)

    monkeypatch.setattr(web_api.competitor_llm_provider, "analyze_source", lambda **kw: _fake_analysis())
    monkeypatch.setattr(web_api.competitor_llm_provider, "generate_draft", lambda **kw: None)

    with patch("app.services.lead_radar._load_recommender", return_value=_fake_recommender()):
        response = client.post("/api/signals/1/actions", json={"action": "post"})

    assert response.status_code == 200
    assert "error" in response.json()
    assert _run(web_api.artifact_repository.list_artifacts(workspace_id, limit=10)) == []

    events = _run(web_api.operational_event_repository.list_recent_events(limit=200))
    matching = [
        e for e in events
        if e.event_type == "material_created_from_signal" and not e.success
    ]
    assert matching
    assert matching[0].error_code == "draft_unavailable"


def test_signal_action_unhandled_exception_gets_a_distinct_error_code_and_log(
    api, monkeypatch, caplog,
) -> None:
    """Bug 3 (production id 21879): an exception raised INSIDE the pipeline
    (not just analyze_source/generate_draft returning None) must not be
    silently swallowed - it must produce a distinct, diagnosable error_code
    and a logged traceback, not the exact same generic outcome as every
    other failure mode."""
    client, web_api, db_path, radar_db_path, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
    _make_signal(web_api, db_path, radar_db_path, workspace_id)

    def boom(**kw):
        raise RuntimeError("forced failure for test_signal_action_unhandled_exception")

    monkeypatch.setattr(web_api.competitor_llm_provider, "analyze_source", boom)

    import logging
    with caplog.at_level(logging.ERROR, logger="app.web_api"):
        with patch("app.services.lead_radar._load_recommender", return_value=_fake_recommender()):
            response = client.post("/api/signals/1/actions", json={"action": "post"})

    assert response.status_code == 200
    assert "error" in response.json()
    assert _run(web_api.artifact_repository.list_artifacts(workspace_id, limit=10)) == []

    events = _run(web_api.operational_event_repository.list_recent_events(limit=200))
    matching = [
        e for e in events
        if e.event_type == "material_created_from_signal" and not e.success
    ]
    assert matching
    assert matching[0].error_code == "unhandled_exception"
    # The real traceback must be diagnosable from the server log, not just
    # "something failed".
    assert any(
        "forced failure for test_signal_action_unhandled_exception" in record.getMessage()
        or (record.exc_text and "RuntimeError" in record.exc_text)
        for record in caplog.records
    )


def test_signal_action_applies_saved_personal_style(api, monkeypatch) -> None:
    client, web_api, db_path, radar_db_path, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
    _make_signal(web_api, db_path, radar_db_path, workspace_id)
    _run(web_api.partner_repository.set_user_voice_sample(
        workspace_id, OWNER_ID, "Всем привет! Погнали в путешествие вместе со мной!",
    ))

    captured = {}

    def fake_generate_draft(**kwargs):
        captured.update(kwargs)
        return _fake_draft()

    monkeypatch.setattr(web_api.competitor_llm_provider, "analyze_source", lambda **kw: _fake_analysis())
    monkeypatch.setattr(web_api.competitor_llm_provider, "generate_draft", fake_generate_draft)

    with patch("app.services.lead_radar._load_recommender", return_value=_fake_recommender()):
        client.post("/api/signals/1/actions", json={"action": "post"})

    assert "Погнали в путешествие вместе со мной" in captured["source_text"]
    assert "[PERSONAL STYLE - DATA]" in captured["source_text"]


def test_signal_action_facts_override_style(api, monkeypatch) -> None:
    """Task's главное правило: SOURCE FACTS take priority over personal
    style, and the style sample is never a source of facts - the constraint
    text warning against carrying over old prices/dates from the style
    sample must be present in what's sent to the model."""
    client, web_api, db_path, radar_db_path, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
    _make_signal(web_api, db_path, radar_db_path, workspace_id)
    _run(web_api.partner_repository.set_user_voice_sample(
        workspace_id, OWNER_ID, "Старая акция: тур за 12000 рублей до 1 января 2020!",
    ))

    captured = {}

    def fake_generate_draft(**kwargs):
        captured.update(kwargs)
        return _fake_draft()

    monkeypatch.setattr(web_api.competitor_llm_provider, "analyze_source", lambda **kw: _fake_analysis())
    monkeypatch.setattr(web_api.competitor_llm_provider, "generate_draft", fake_generate_draft)

    with patch("app.services.lead_radar._load_recommender", return_value=_fake_recommender()):
        client.post("/api/signals/1/actions", json={"action": "post"})

    source_text = captured["source_text"]
    # The real signal's fact (45000) is present as SOURCE FACTS...
    assert "45000" in source_text
    # ...and the model is explicitly told the style sample's own numbers
    # (12000/2020) are not current facts.
    assert "не считаются актуальной информацией" in source_text
    assert "voice_sample" in source_text


def test_signal_action_telemetry_never_contains_raw_material_text(api, monkeypatch) -> None:
    client, web_api, db_path, radar_db_path, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
    _make_signal(web_api, db_path, radar_db_path, workspace_id)

    secret_text = "СЕКРЕТНЫЙ-ТЕКСТ-ЧЕРНОВИКА-ДЛЯ-ПРОВЕРКИ-ТЕЛЕМЕТРИИ"
    monkeypatch.setattr(web_api.competitor_llm_provider, "analyze_source", lambda **kw: _fake_analysis())
    monkeypatch.setattr(web_api.competitor_llm_provider, "generate_draft", lambda **kw: _fake_draft(secret_text))

    with patch("app.services.lead_radar._load_recommender", return_value=_fake_recommender()):
        client.post("/api/signals/1/actions", json={"action": "post"})

    events = _run(web_api.operational_event_repository.list_recent_events(limit=200))
    assert any(e.event_type == "action_selected" and e.module == "signals" for e in events)
    assert any(e.event_type == "material_created_from_signal" for e in events)
    for event in events:
        assert secret_text not in (event.safe_message or "")
        assert secret_text not in (event.metadata_json or "")


# ── POST /api/competitors/{id}/opportunities/{id}/actions ──────────────────

def _opportunity(**overrides):
    from app.domain.competitor_intelligence import ContentOpportunity

    base = dict(
        id="opp-1", competitor_id=1, topic="Конкурент снизил цены на пакетные туры",
        source_title="Пост конкурента", source_url="https://example.com/post",
        freshness="сегодня", key_thesis="Конкурент демпингует на популярных направлениях",
        audience_value="Клиентам важна прозрачная цена", own_post_angle="Наш сервис включает поддержку 24/7",
        travel_advantage_link=None,
    )
    base.update(overrides)
    return ContentOpportunity(**base)


def _intelligence(competitor_id: int, *, opportunities, data_origin="direct_fetch"):
    from app.domain.competitor_intelligence import CompetitorIntelligence

    return CompetitorIntelligence(
        competitor_id=competitor_id, competitor_label="RivalCo", analyzed_at="2026-01-01T00:00:00+00:00",
        positioning=(), products=(), destinations_and_categories=(), promotions=(),
        loyalty_mechanics=(), service_and_ux=(), strengths=(), travel_advantage_comparison=(),
        fresh_signals=(), sources=(), opportunities=opportunities, data_origin=data_origin,
    )


def test_competitor_action_creates_and_persists_a_material(api, monkeypatch) -> None:
    """Unlike Telegram's create_from_competitor_opportunity (draft shown in
    chat only), the web action must persist the draft as a real Artifact so
    it shows up in "Материалы"."""
    client, web_api, _, _, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
    competitor = _run(web_api.competitor_repository.add_competitor(workspace_id, "https://rival.example.com"))
    opportunity = _opportunity(competitor_id=competitor.id)
    _run(web_api.competitor_repository.save_intelligence(
        workspace_id, _intelligence(competitor.id, opportunities=(opportunity,)),
    ))

    monkeypatch.setattr(web_api.competitor_llm_provider, "generate_draft", lambda **kw: _fake_draft("Пост про конкурента."))

    response = client.post(
        f"/api/competitors/{competitor.id}/opportunities/opp-1/actions",
        json={"action": "post"},
    )

    assert response.status_code == 200
    body = response.json()
    assert "error" not in body
    assert body["material"]["artifact_type"] == "post"
    assert body["version"]["content"] == "Пост про конкурента."
    assert body["origin"]["kind"] == "competitor"
    assert body["origin"]["data_origin"] == "direct_fetch"

    materials = _run(web_api.artifact_repository.list_artifacts(workspace_id, limit=10))
    assert len(materials) == 1


def test_competitor_action_uses_correct_competitor_and_opportunity(api, monkeypatch) -> None:
    client, web_api, _, _, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
    competitor = _run(web_api.competitor_repository.add_competitor(workspace_id, "https://rival.example.com"))
    opportunity = _opportunity(
        competitor_id=competitor.id, key_thesis="Конкурент демпингует ценами на туры в Египет",
    )
    _run(web_api.competitor_repository.save_intelligence(
        workspace_id, _intelligence(competitor.id, opportunities=(opportunity,)),
    ))

    captured = {}

    def fake_generate_draft(**kwargs):
        captured.update(kwargs)
        return _fake_draft()

    monkeypatch.setattr(web_api.competitor_llm_provider, "generate_draft", fake_generate_draft)

    client.post(f"/api/competitors/{competitor.id}/opportunities/opp-1/actions", json={"action": "post"})

    assert "Конкурент демпингует ценами на туры в Египет" in captured["source_text"]


def test_cannot_use_another_workspaces_competitor_opportunity(api, monkeypatch) -> None:
    client, web_api, _, _, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))

    other = _run(web_api.partner_repository.provision_partner(
        222334111, "Other Agency 2", "other-agency-competitor-actions",
        business_name="Other", business_type="independent_agent",
        short_description="x", context={},
    ))
    other_competitor = _run(web_api.competitor_repository.add_competitor(
        other.workspace.id, "https://rival-other.example.com",
    ))
    opportunity = _opportunity(competitor_id=other_competitor.id)
    _run(web_api.competitor_repository.save_intelligence(
        other.workspace.id, _intelligence(other_competitor.id, opportunities=(opportunity,)),
    ))

    called = {"count": 0}

    def fake_generate_draft(**kwargs):
        called["count"] += 1
        return _fake_draft()

    monkeypatch.setattr(web_api.competitor_llm_provider, "generate_draft", fake_generate_draft)

    response = client.post(
        f"/api/competitors/{other_competitor.id}/opportunities/opp-1/actions",
        json={"action": "post"},
    )

    assert response.status_code == 200
    assert response.json()["error"] == "Конкурент не найден."
    assert called["count"] == 0


def test_competitor_action_unknown_opportunity_id_is_rejected(api) -> None:
    client, web_api, _, _, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
    competitor = _run(web_api.competitor_repository.add_competitor(workspace_id, "https://rival.example.com"))
    _run(web_api.competitor_repository.save_intelligence(
        workspace_id, _intelligence(competitor.id, opportunities=(_opportunity(competitor_id=competitor.id),)),
    ))

    response = client.post(
        f"/api/competitors/{competitor.id}/opportunities/does-not-exist/actions",
        json={"action": "post"},
    )

    assert response.status_code == 200
    assert response.json() == {"error": "Рекомендация недоступна.", "material": None}


def test_competitor_action_preserves_fallback_provenance(api, monkeypatch) -> None:
    """If the analysis came from Radar-signal fallback (not a fresh direct
    fetch), that must survive into the generation_note (Artifact provenance)
    and the response's origin block - never presented as fresh."""
    client, web_api, _, _, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
    competitor = _run(web_api.competitor_repository.add_competitor(workspace_id, "https://rival.example.com"))
    opportunity = _opportunity(competitor_id=competitor.id)
    _run(web_api.competitor_repository.save_intelligence(
        workspace_id,
        _intelligence(competitor.id, opportunities=(opportunity,), data_origin="radar_signal"),
    ))

    monkeypatch.setattr(web_api.competitor_llm_provider, "generate_draft", lambda **kw: _fake_draft())

    response = client.post(
        f"/api/competitors/{competitor.id}/opportunities/opp-1/actions",
        json={"action": "post"},
    )

    body = response.json()
    assert body["origin"]["data_origin"] == "radar_signal"

    version = _run(web_api.artifact_repository.get_current_artifact_version(
        workspace_id, body["material"]["id"],
    ))
    assert "radar_signal" in version.generation_note


def test_competitor_action_applies_saved_personal_style(api, monkeypatch) -> None:
    client, web_api, _, _, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
    competitor = _run(web_api.competitor_repository.add_competitor(workspace_id, "https://rival.example.com"))
    _run(web_api.competitor_repository.save_intelligence(
        workspace_id, _intelligence(competitor.id, opportunities=(_opportunity(competitor_id=competitor.id),)),
    ))
    _run(web_api.partner_repository.set_user_style_description(workspace_id, OWNER_ID, "Пишу с юмором."))

    captured = {}

    def fake_generate_draft(**kwargs):
        captured.update(kwargs)
        return _fake_draft()

    monkeypatch.setattr(web_api.competitor_llm_provider, "generate_draft", fake_generate_draft)

    client.post(f"/api/competitors/{competitor.id}/opportunities/opp-1/actions", json={"action": "post"})

    assert "Пишу с юмором." in captured["source_text"]
    assert "[PERSONAL STYLE - DATA]" in captured["source_text"]


def test_competitor_action_client_message_uses_client_message_artifact_type(api, monkeypatch) -> None:
    client, web_api, _, _, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
    competitor = _run(web_api.competitor_repository.add_competitor(workspace_id, "https://rival.example.com"))
    _run(web_api.competitor_repository.save_intelligence(
        workspace_id, _intelligence(competitor.id, opportunities=(_opportunity(competitor_id=competitor.id),)),
    ))

    monkeypatch.setattr(web_api.competitor_llm_provider, "generate_draft", lambda **kw: _fake_draft())

    response = client.post(
        f"/api/competitors/{competitor.id}/opportunities/opp-1/actions",
        json={"action": "client_message"},
    )

    assert response.json()["material"]["artifact_type"] == "client_message"


def test_competitor_action_rejects_unknown_action(api) -> None:
    client, web_api, _, _, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
    competitor = _run(web_api.competitor_repository.add_competitor(workspace_id, "https://rival.example.com"))
    _run(web_api.competitor_repository.save_intelligence(
        workspace_id, _intelligence(competitor.id, opportunities=(_opportunity(competitor_id=competitor.id),)),
    ))

    response = client.post(
        f"/api/competitors/{competitor.id}/opportunities/opp-1/actions",
        json={"action": "itinerary"},
    )

    assert response.json() == {"error": "Неизвестное действие.", "material": None}


def test_competitor_action_telemetry_never_contains_raw_material_text(api, monkeypatch) -> None:
    client, web_api, _, _, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
    competitor = _run(web_api.competitor_repository.add_competitor(workspace_id, "https://rival.example.com"))
    _run(web_api.competitor_repository.save_intelligence(
        workspace_id, _intelligence(competitor.id, opportunities=(_opportunity(competitor_id=competitor.id),)),
    ))

    secret_text = "СЕКРЕТНЫЙ-ТЕКСТ-КОНКУРЕНТНОГО-ЧЕРНОВИКА"
    monkeypatch.setattr(web_api.competitor_llm_provider, "generate_draft", lambda **kw: _fake_draft(secret_text))

    client.post(f"/api/competitors/{competitor.id}/opportunities/opp-1/actions", json={"action": "post"})

    events = _run(web_api.operational_event_repository.list_recent_events(limit=200))
    assert any(e.event_type == "action_selected" and e.module == "competitors" for e in events)
    assert any(e.event_type == "material_created_from_competitor" for e in events)
    for event in events:
        assert secret_text not in (event.safe_message or "")
        assert secret_text not in (event.metadata_json or "")


# ── Quality fix: signal/competitor -> material contract regressions ────────
#
# Production showed internal meta-commentary about source reliability and
# an AI-voiced trailing CTA leaking into a delivered, publication-ready
# post. Both endpoints must apply sanitize_draft_text to the raw draft
# before persisting/returning it - these tests exercise that end to end
# through the actual endpoint, not just the sanitizer unit tests
# (tests/test_draft_sanitizer.py covers the sanitizer itself in isolation).

_LEAKED_META_COMMENTARY = (
    "Раннее бронирование Турции подешевело на треть. "
    "Остальное в исходном тексте — шутка и личная оценка, на них лучше не опираться. "
    "Планируйте поездку заранее, пока действует цена."
)
_LEAKED_AI_SELF_OFFER = (
    "Раннее бронирование Турции подешевело на треть.\n"
    "Планируйте поездку заранее, пока действует цена.\n"
    "Могу сравнить варианты поездки, если нужно.\n"
    "#Турция #ОтпускМечты"
)


def test_signal_action_strips_internal_meta_commentary_from_final_material(api, monkeypatch) -> None:
    client, web_api, db_path, radar_db_path, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
    _make_signal(web_api, db_path, radar_db_path, workspace_id)

    monkeypatch.setattr(web_api.competitor_llm_provider, "analyze_source", lambda **kw: _fake_analysis())
    monkeypatch.setattr(
        web_api.competitor_llm_provider, "generate_draft",
        lambda **kw: _fake_draft(_LEAKED_META_COMMENTARY),
    )

    with patch("app.services.lead_radar._load_recommender", return_value=_fake_recommender()):
        response = client.post("/api/signals/1/actions", json={"action": "post"})

    content = response.json()["version"]["content"]
    assert "исходном тексте" not in content
    assert "лучше не опираться" not in content
    assert "Раннее бронирование Турции подешевело на треть." in content
    assert "Планируйте поездку заранее, пока действует цена." in content


def test_signal_action_strips_ai_self_offer_hidden_behind_hashtags(api, monkeypatch) -> None:
    client, web_api, db_path, radar_db_path, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
    _make_signal(web_api, db_path, radar_db_path, workspace_id)

    monkeypatch.setattr(web_api.competitor_llm_provider, "analyze_source", lambda **kw: _fake_analysis())
    monkeypatch.setattr(
        web_api.competitor_llm_provider, "generate_draft",
        lambda **kw: _fake_draft(_LEAKED_AI_SELF_OFFER),
    )

    with patch("app.services.lead_radar._load_recommender", return_value=_fake_recommender()):
        response = client.post("/api/signals/1/actions", json={"action": "post"})

    content = response.json()["version"]["content"]
    assert "Могу сравнить" not in content
    assert "#Турция #ОтпускМечты" in content
    assert "Раннее бронирование Турции подешевело на треть." in content


def test_competitor_action_strips_internal_meta_commentary_from_final_material(api, monkeypatch) -> None:
    client, web_api, _, _, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
    competitor = _run(web_api.competitor_repository.add_competitor(workspace_id, "https://rival.example.com"))
    _run(web_api.competitor_repository.save_intelligence(
        workspace_id, _intelligence(competitor.id, opportunities=(_opportunity(competitor_id=competitor.id),)),
    ))

    monkeypatch.setattr(
        web_api.competitor_llm_provider, "generate_draft",
        lambda **kw: _fake_draft(_LEAKED_AI_SELF_OFFER),
    )

    response = client.post(
        f"/api/competitors/{competitor.id}/opportunities/opp-1/actions",
        json={"action": "client_message"},
    )

    content = response.json()["version"]["content"]
    assert "Могу сравнить" not in content
    assert "#Турция #ОтпускМечты" in content


def test_signal_action_prompt_still_contains_factual_safety_constraints(api, monkeypatch) -> None:
    """The fix must not weaken factual safety: the constraint that
    disputed/unconfirmed claims must not be presented as fact stays in the
    generation prompt (spec.constraints) - only the FINAL post text is
    cleaned, not the model's instructions."""
    client, web_api, db_path, radar_db_path, workspace_id = api
    _run(web_api.partner_repository.bootstrap_owner_membership(OWNER_ID))
    _make_signal(web_api, db_path, radar_db_path, workspace_id)

    captured = {}

    def fake_generate_draft(**kwargs):
        captured.update(kwargs)
        return _fake_draft()

    monkeypatch.setattr(web_api.competitor_llm_provider, "analyze_source", lambda **kw: _fake_analysis())
    monkeypatch.setattr(web_api.competitor_llm_provider, "generate_draft", fake_generate_draft)

    with patch("app.services.lead_radar._load_recommender", return_value=_fake_recommender()):
        client.post("/api/signals/1/actions", json={"action": "post"})

    source_text = captured["source_text"]
    assert "не подавай их как факт" in source_text
    assert "не появляется в самом посте как" in source_text
