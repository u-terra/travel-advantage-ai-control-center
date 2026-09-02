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

from tests._web_auth_test_helpers import login_as  # noqa: E402

OWNER_ID = 586249067


def _run(coro):
    return asyncio.run(coro)


def _login(client, web_api):
    ws, _ = _run(web_api.partner_repository.ensure_owner_workspace(OWNER_ID))
    login_as(client, web_api, ws.id, OWNER_ID)
    return ws.id


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

    with TestClient(web_api.app, base_url="https://testserver") as client:
        yield client, web_api, db_path

    sys.modules.pop("app.web_api", None)


def test_empty_workspace_returns_empty_list(api) -> None:
    client, web_api, _ = api
    _login(client, web_api)

    response = client.get("/api/competitors")

    assert response.status_code == 200
    assert response.json() == {"competitors": []}


def test_lists_real_saved_competitors_with_ui_fields_only(api) -> None:
    client, web_api, _ = api
    workspace_id = _login(client, web_api)
    competitor = _run(web_api.competitor_repository.add_competitor(
        workspace_id, "https://nl.trip.com/?locale=nl-nl", label="Trip.com",
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
    workspace_id = _login(client, web_api)
    competitor = _run(web_api.competitor_repository.add_competitor(
        workspace_id, "https://competitor.example.com", label="Example",
    ))
    _run(web_api.competitor_repository.save_intelligence(
        workspace_id, _intelligence(competitor.id, "2026-01-01T00:00:00+00:00"),
    ))

    response = client.get("/api/competitors")

    assert response.json()["competitors"][0]["last_analyzed_at"] == "2026-01-01T00:00:00+00:00"


def _full_intelligence(competitor_id: int, analyzed_at: str) -> CompetitorIntelligence:
    from app.domain.competitor_intelligence import (
        CompetitorSourceEvidence,
        ContentOpportunity,
    )

    return CompetitorIntelligence(
        competitor_id=competitor_id, competitor_label="Trip.com", analyzed_at=analyzed_at,
        positioning=("Позиционируется как OTA полного цикла.",),
        products=("Отели", "Авиабилеты"),
        destinations_and_categories=("Азия",),
        promotions=("Скидка 10% для новых пользователей",),
        loyalty_mechanics=("Программа Trip Coins",),
        service_and_ux=("Гибкая отмена бронирования",),
        strengths=("Широкий инвентарь отелей",),
        travel_advantage_comparison=("Travel Advantage — предлагает персональный сервис.",),
        fresh_signals=("Новость: запуск AI-планировщика поездок",),
        sources=(
            CompetitorSourceEvidence(
                title="Trip.com Blog", url="https://trip.com/blog",
                final_url="https://trip.com/blog", discovered_at=analyzed_at,
                freshness=None, summary="Обзор блога.", key_facts=("Факт 1",),
            ),
        ),
        opportunities=(
            ContentOpportunity(
                id="opp-1", competitor_id=competitor_id, topic="AI и технологии в travel",
                source_title="Trip.com Blog", source_url="https://trip.com/blog",
                freshness=None, key_thesis="Запуск AI-планировщика",
                audience_value="Помогает планировать поездки быстрее.",
                own_post_angle="Разбираем, как AI меняет планирование поездок.",
                travel_advantage_link=None,
            ),
        ),
    )


def test_intelligence_report_returns_saved_snapshot(api) -> None:
    client, web_api, _ = api
    workspace_id = _login(client, web_api)
    competitor = _run(web_api.competitor_repository.add_competitor(
        workspace_id, "https://nl.trip.com/?locale=nl-nl", label="Trip.com",
    ))
    _run(web_api.competitor_repository.save_intelligence(
        workspace_id,
        _full_intelligence(competitor.id, "2026-01-01T00:00:00+00:00"),
    ))

    response = client.get(f"/api/competitors/{competitor.id}/intelligence")

    assert response.status_code == 200
    body = response.json()
    assert body["competitor"] == {
        "id": competitor.id,
        "label": "Trip.com",
        "domain": "trip.com",
        "url": "https://nl.trip.com/?locale=nl-nl",
    }
    assert body["intelligence"]["analyzed_at"] == "2026-01-01T00:00:00+00:00"
    assert body["intelligence"]["positioning"] == ["Позиционируется как OTA полного цикла."]
    assert body["intelligence"]["sources"][0]["title"] == "Trip.com Blog"
    assert body["intelligence"]["opportunities"][0]["topic"] == "AI и технологии в travel"


def test_intelligence_report_returns_none_when_not_yet_analyzed(api) -> None:
    client, web_api, _ = api
    workspace_id = _login(client, web_api)
    competitor = _run(web_api.competitor_repository.add_competitor(
        workspace_id, "https://competitor.example.com", label="Example",
    ))

    response = client.get(f"/api/competitors/{competitor.id}/intelligence")

    assert response.status_code == 200
    body = response.json()
    assert body["competitor"]["id"] == competitor.id
    assert body["intelligence"] is None
    assert "error" not in body


def test_intelligence_report_unknown_competitor_id_has_no_500(api) -> None:
    client, web_api, _ = api
    _login(client, web_api)

    response = client.get("/api/competitors/999999/intelligence")

    assert response.status_code == 200
    body = response.json()
    assert body["intelligence"] is None
    assert "error" in body


def test_intelligence_report_not_leaked_across_workspaces(api) -> None:
    client, web_api, _ = api
    _login(client, web_api)
    other = _run(web_api.partner_repository.provision_partner(
        222333555, "Other Agency 2", "other-agency-2",
        business_name="Other Agency 2", business_type="independent_agent",
        short_description="Другое рабочее пространство.",
        context={},
    ))
    foreign_competitor = _run(web_api.competitor_repository.add_competitor(
        other.workspace.id, "https://not-mine.example.com", label="Not mine",
    ))
    _run(web_api.competitor_repository.save_intelligence(
        other.workspace.id,
        _full_intelligence(foreign_competitor.id, "2026-01-01T00:00:00+00:00"),
    ))

    response = client.get(f"/api/competitors/{foreign_competitor.id}/intelligence")

    assert response.status_code == 200
    body = response.json()
    assert body["intelligence"] is None
    assert "error" in body


def test_only_current_web_workspace_competitors_are_returned(api) -> None:
    client, web_api, _ = api
    workspace_id = _login(client, web_api)
    _run(web_api.competitor_repository.add_competitor(
        workspace_id, "https://mine.example.com", label="Mine",
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
