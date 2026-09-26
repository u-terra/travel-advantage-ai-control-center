"""HTTP-level ORCHESTRAVEL v1 plan limits: billing status shape/usage,
competitor slot enforcement via POST /api/competitors, and the "quota
blocks the expensive provider call before it happens" contract for
competitor analysis.

Requires the web-only dependencies (requirements-web.txt) - skips cleanly
when they're not installed, same convention as the other test_web_api_*.py
files in this suite.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("markdown")
pytest.importorskip("argon2")

from fastapi.testclient import TestClient  # noqa: E402

from app.domain.competitor_intelligence import CompetitorIntelligence  # noqa: E402
from app.domain.subscription import SubscriptionPlan  # noqa: E402
from tests._web_auth_test_helpers import login_as  # noqa: E402

OWNER_ID = 586249067


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def api(tmp_path, monkeypatch):
    db_path = tmp_path / "journal.sqlite3"
    monkeypatch.setenv("JOURNAL_DB_PATH", str(db_path))
    monkeypatch.setenv("PLANNER_OPENAI_API_KEY", "test-key")

    import sys
    sys.modules.pop("app.web_api", None)
    import app.web_api as web_api

    with TestClient(web_api.app, base_url="https://testserver") as client:
        ws, _ = _run(web_api.partner_repository.ensure_owner_workspace(OWNER_ID))
        login_as(client, web_api, ws.id, OWNER_ID)
        yield client, web_api, ws.id

    sys.modules.pop("app.web_api", None)


def _set_plan(web_api, workspace_id: int, plan: SubscriptionPlan) -> None:
    paid_until = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
    _run(web_api.subscription_repository.mark_paid(
        workspace_id, external_payment_id=f"order-{workspace_id}",
        payment_provider="robokassa", paid_until=paid_until, plan=plan,
    ))


def _intelligence(competitor_id: int) -> CompetitorIntelligence:
    return CompetitorIntelligence(
        competitor_id=competitor_id, competitor_label="Test", analyzed_at="2026-01-01T00:00:00+00:00",
        positioning=(), products=(), destinations_and_categories=(), promotions=(),
        loyalty_mechanics=(), service_and_ux=(), strengths=(),
        travel_advantage_comparison=(), fresh_signals=(), sources=(), opportunities=(),
    )


# ── billing status: limits table + current usage ───────────────────────────


def test_billing_status_exposes_the_plan_limits_table(api) -> None:
    client, web_api, workspace_id = api

    response = client.get("/api/billing/status")

    assert response.status_code == 200
    plans = {plan["code"]: plan for plan in response.json()["plans"]}
    assert plans["start"]["limits"] == {
        "materials_limit": 15, "competitor_analyses_limit": 3, "window_days": 14,
        "competitor_slot_limit": 3, "source_slot_limit": 5,
    }
    assert plans["standard"]["limits"] == {
        "materials_limit": 40, "competitor_analyses_limit": 10, "window_days": 30,
        "competitor_slot_limit": 10, "source_slot_limit": 15,
    }
    assert plans["full"]["limits"] == {
        "materials_limit": 100, "competitor_analyses_limit": 30, "window_days": 30,
        "competitor_slot_limit": 25, "source_slot_limit": 40,
    }


def test_billing_status_has_no_usage_panel_for_beta_workspace(api) -> None:
    client, web_api, workspace_id = api  # ensure_owner_workspace grandfathers in as 'beta'

    response = client.get("/api/billing/status")

    assert response.json()["usage"] is None


def test_billing_status_shows_current_usage_for_a_real_plan(api) -> None:
    client, web_api, workspace_id = api
    _set_plan(web_api, workspace_id, SubscriptionPlan.STANDARD)
    _run(web_api.plan_quota_service.record_material_created(workspace_id))
    _run(web_api.plan_quota_service.record_material_created(workspace_id))
    _run(web_api.competitor_repository.add_competitor(workspace_id, "https://a.example.com"))

    response = client.get("/api/billing/status")

    usage = response.json()["usage"]
    assert usage == {
        "window_days": 30,
        "materials_used": 2, "materials_limit": 40,
        "competitor_analyses_used": 0, "competitor_analyses_limit": 10,
        "competitors_used": 1, "competitors_limit": 10,
        "sources_used": 0, "sources_limit": 15,
    }


# ── competitor slot limit over HTTP ─────────────────────────────────────────


def test_add_competitor_blocked_over_start_slot_limit(api) -> None:
    client, web_api, workspace_id = api
    _set_plan(web_api, workspace_id, SubscriptionPlan.START)  # limit 3

    for index in range(3):
        response = client.post("/api/competitors", json={"url": f"https://site{index}.example.com"})
        assert response.status_code == 200
        assert response.json()["competitor"] is not None

    blocked = client.post("/api/competitors", json={"url": "https://site4.example.com"})
    body = blocked.json()
    assert body["competitor"] is None
    assert "START" in body["error"]
    assert "3 конкурентов" in body["error"]

    competitors = _run(web_api.competitor_repository.list_for_workspace(workspace_id))
    assert len(competitors) == 3  # blocked add never persisted


def test_add_competitor_not_limited_on_beta_plan(api) -> None:
    client, web_api, workspace_id = api  # default beta/legacy plan

    for index in range(5):
        response = client.post("/api/competitors", json={"url": f"https://beta{index}.example.com"})
        assert response.json()["competitor"] is not None


# ── quota blocks the expensive provider call BEFORE it happens ─────────────


def test_competitor_analysis_quota_blocks_analyze_before_the_provider_call(api) -> None:
    client, web_api, workspace_id = api
    _set_plan(web_api, workspace_id, SubscriptionPlan.START)  # 3 analyses / 14 days
    competitor = _run(web_api.competitor_repository.add_competitor(
        workspace_id, "https://a.example.com",
    ))
    for _ in range(3):
        _run(web_api.plan_quota_service.record_competitor_analysis_completed(workspace_id))

    spy = AsyncMock(return_value=_intelligence(competitor.id))
    web_api.competitor_intelligence_service.analyze = spy

    response = client.post(f"/api/competitors/{competitor.id}/analyze")

    assert response.status_code == 200
    body = response.json()
    assert body["intelligence"] is None
    assert "START" in body["error"]
    spy.assert_not_awaited()  # the expensive LLM-backed call never happened


# ── request.action ("post" vs "client_message") drives material quota ──────


def _opportunity(competitor_id: int):
    from app.domain.competitor_intelligence import ContentOpportunity
    return ContentOpportunity(
        id="opp-1", competitor_id=competitor_id, topic="Конкурент снизил цены",
        source_title="Пост конкурента", source_url="https://example.com/post",
        freshness="сегодня", key_thesis="Конкурент демпингует",
        audience_value="Клиентам важна цена", own_post_angle="Наш сервис включает поддержку",
        travel_advantage_link=None,
    )


def test_opportunity_action_post_consumes_material_quota(api, monkeypatch) -> None:
    client, web_api, workspace_id = api
    _set_plan(web_api, workspace_id, SubscriptionPlan.STANDARD)
    competitor = _run(web_api.competitor_repository.add_competitor(
        workspace_id, "https://rival.example.com",
    ))
    opportunity = _opportunity(competitor.id)
    _run(web_api.competitor_repository.save_intelligence(
        workspace_id, _intelligence_with(competitor.id, opportunity),
    ))
    from app.services.llm.models import ContentDraft
    monkeypatch.setattr(
        web_api.competitor_llm_provider, "generate_draft",
        lambda **kwargs: ContentDraft(text="Черновик", warnings=()),
    )

    response = client.post(
        f"/api/competitors/{competitor.id}/opportunities/{opportunity.id}/actions",
        json={"action": "post"},
    )

    assert response.status_code == 200
    assert response.json()["material"] is not None
    from app.repositories.plan_usage_repository import MATERIAL_CREATED
    count = _run(web_api.plan_usage_repository.count_since(
        workspace_id, MATERIAL_CREATED, "1970-01-01T00:00:00+00:00",
    ))
    assert count == 1


def test_opportunity_action_client_message_never_consumes_material_quota(api, monkeypatch) -> None:
    """«Ответить клиенту» (action=client_message) creates a real Artifact
    too, but must never spend the material quota."""
    client, web_api, workspace_id = api
    _set_plan(web_api, workspace_id, SubscriptionPlan.STANDARD)
    competitor = _run(web_api.competitor_repository.add_competitor(
        workspace_id, "https://rival.example.com",
    ))
    opportunity = _opportunity(competitor.id)
    _run(web_api.competitor_repository.save_intelligence(
        workspace_id, _intelligence_with(competitor.id, opportunity),
    ))
    from app.services.llm.models import ContentDraft
    monkeypatch.setattr(
        web_api.competitor_llm_provider, "generate_draft",
        lambda **kwargs: ContentDraft(text="Черновик клиенту", warnings=()),
    )

    response = client.post(
        f"/api/competitors/{competitor.id}/opportunities/{opportunity.id}/actions",
        json={"action": "client_message"},
    )

    assert response.status_code == 200
    assert response.json()["material"] is not None  # the Artifact IS created
    from app.repositories.plan_usage_repository import MATERIAL_CREATED
    count = _run(web_api.plan_usage_repository.count_since(
        workspace_id, MATERIAL_CREATED, "1970-01-01T00:00:00+00:00",
    ))
    assert count == 0  # ...but it never spends the material quota


def _intelligence_with(competitor_id: int, opportunity):
    from app.domain.competitor_intelligence import CompetitorIntelligence
    return CompetitorIntelligence(
        competitor_id=competitor_id, competitor_label="RivalCo",
        analyzed_at="2026-01-01T00:00:00+00:00",
        positioning=(), products=(), destinations_and_categories=(), promotions=(),
        loyalty_mechanics=(), service_and_ux=(), strengths=(),
        travel_advantage_comparison=(), fresh_signals=(), sources=(),
        opportunities=(opportunity,), data_origin="direct_fetch",
    )


def test_competitor_analysis_allowed_under_quota_calls_provider_and_records(api) -> None:
    client, web_api, workspace_id = api
    _set_plan(web_api, workspace_id, SubscriptionPlan.START)
    competitor = _run(web_api.competitor_repository.add_competitor(
        workspace_id, "https://a.example.com",
    ))

    spy = AsyncMock(return_value=_intelligence(competitor.id))
    web_api.competitor_intelligence_service.analyze = spy

    response = client.post(f"/api/competitors/{competitor.id}/analyze")

    assert response.status_code == 200
    assert response.json()["intelligence"] is not None
    spy.assert_awaited_once()

    since = "1970-01-01T00:00:00+00:00"
    from app.repositories.plan_usage_repository import COMPETITOR_ANALYSIS_COMPLETED
    count = _run(web_api.plan_usage_repository.count_since(
        workspace_id, COMPETITOR_ANALYSIS_COMPLETED, since,
    ))
    assert count == 1
