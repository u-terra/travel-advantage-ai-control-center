"""GET /api/competitors - read-only listing for the web Assistant shell.

Requires the web-only dependencies (requirements-web.txt: fastapi,
uvicorn, markdown) that app/web_api.py imports at module level. Skips
cleanly instead of failing the whole suite when they're not installed
(base requirements.txt does not include them).
"""

from __future__ import annotations

import asyncio

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("markdown")

from app.domain.competitor_intelligence import CompetitorIntelligence  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _intelligence(competitor_id: int, analyzed_at: str) -> CompetitorIntelligence:
    return CompetitorIntelligence(
        competitor_id=competitor_id, competitor_label="Test", analyzed_at=analyzed_at,
        positioning=(), products=(), destinations_and_categories=(), promotions=(),
        loyalty_mechanics=(), service_and_ux=(), strengths=(),
        travel_advantage_comparison=(), fresh_signals=(), sources=(), opportunities=(),
    )


@pytest.fixture
def api(tmp_path, monkeypatch):
    """Fresh app.web_api module bound to an isolated journal.sqlite3.

    web_api.py builds its repositories at import time from settings, so
    the DB path is redirected via JOURNAL_DB_PATH before import, and the
    module is imported fresh (not reused from sys.modules) so each test
    gets its own isolated database.
    """
    db_path = tmp_path / "journal.sqlite3"
    monkeypatch.setenv("JOURNAL_DB_PATH", str(db_path))
    monkeypatch.setenv("PLANNER_OPENAI_API_KEY", "test-key")

    import sys
    sys.modules.pop("app.web_api", None)
    import app.web_api as web_api

    with TestClient(web_api.app) as client:
        yield client, web_api, db_path

    sys.modules.pop("app.web_api", None)


def test_empty_workspace_returns_empty_list(api) -> None:
    client, web_api, _ = api

    response = client.get("/api/competitors")

    assert response.status_code == 200
    assert response.json() == {"competitors": []}


def test_lists_real_saved_competitors_with_ui_fields_only(api) -> None:
    client, web_api, _ = api
    _run(web_api.partner_repository.ensure_owner_workspace(web_api.WEB_TELEGRAM_USER_ID))
    competitor = _run(web_api.competitor_repository.add_competitor(
        web_api.WEB_WORKSPACE_ID, "https://nl.trip.com/?locale=nl-nl", label="Trip.com",
    ))

    response = client.get("/api/competitors")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "competitors": [
            {
                "id": competitor.id,
                "label": "Trip.com",
                "domain": "trip.com",
                "url": "https://nl.trip.com/?locale=nl-nl",
                "last_analyzed_at": None,
            }
        ]
    }


def test_last_analyzed_at_reflects_saved_intelligence_snapshot(api) -> None:
    client, web_api, _ = api
    _run(web_api.partner_repository.ensure_owner_workspace(web_api.WEB_TELEGRAM_USER_ID))
    competitor = _run(web_api.competitor_repository.add_competitor(
        web_api.WEB_WORKSPACE_ID, "https://competitor.example.com", label="Example",
    ))
    _run(web_api.competitor_repository.save_intelligence(
        web_api.WEB_WORKSPACE_ID, _intelligence(competitor.id, "2026-01-01T00:00:00+00:00"),
    ))

    response = client.get("/api/competitors")

    assert response.json()["competitors"][0]["last_analyzed_at"] == "2026-01-01T00:00:00+00:00"


def test_only_current_web_workspace_competitors_are_returned(api) -> None:
    client, web_api, _ = api
    _run(web_api.partner_repository.ensure_owner_workspace(web_api.WEB_TELEGRAM_USER_ID))
    _run(web_api.competitor_repository.add_competitor(
        web_api.WEB_WORKSPACE_ID, "https://mine.example.com", label="Mine",
    ))
    other = _run(web_api.partner_repository.provision_partner(
        222333444, "Other Agency", "other-agency",
        business_name="Other Agency", business_type="independent_agent",
        short_description="Другое рабочее пространство.",
        context={},
    ))
    _run(web_api.competitor_repository.add_competitor(
        other.workspace.id, "https://not-mine.example.com", label="Not mine",
    ))

    response = client.get("/api/competitors")

    labels = [item["label"] for item in response.json()["competitors"]]
    assert labels == ["Mine"]
