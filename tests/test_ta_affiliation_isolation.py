"""Travel Advantage / MWR Life tenant-isolation fix (see the isolation
audit): BusinessProfile.ta_affiliated is the one authoritative, fail-closed
signal gating TA/MWR content - never business_type, workspace_id, or role.

Covers the three BLOCKER areas end-to-end at the web_api.py level:
- /api/chat's knowledge_context (knowledge_service.retrieve() gating)
- competitor intelligence's ta_affiliated propagation into
  CompetitorIntelligenceService.analyze() (the service's own gating logic
  is unit-tested in tests/test_competitor_intelligence.py)
- the static chat.html markup shipping a neutral (non-TA) default

GET /api/knowledge's own gating is covered in tests/test_web_api_knowledge.py.

Requires the web-only dependencies (requirements-web.txt: fastapi,
uvicorn, markdown, argon2-cffi). Skips cleanly when they're not installed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("markdown")
pytest.importorskip("argon2")

import aiosqlite  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.chat_provider import ChatResult  # noqa: E402
from app.domain.competitor_intelligence import CompetitorIntelligence  # noqa: E402
from app.services.knowledge_service import KnowledgeBundle  # noqa: E402

from tests._web_auth_test_helpers import login_as  # noqa: E402

TA_OWNER_ID = 586249067
INDEPENDENT_ID = 700000002


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("JOURNAL_DB_PATH", str(tmp_path / "journal.sqlite3"))
    monkeypatch.setenv("PLANNER_OPENAI_API_KEY", "test-key")

    import sys
    sys.modules.pop("app.web_api", None)
    import app.web_api as web_api

    with TestClient(web_api.app, base_url="https://testserver") as client:
        # ensure_owner_workspace() is the one workspace ta_affiliated=True
        # is ever hardcoded for (see app.repositories.partner_repository) -
        # the same "workspace 1" referenced in the isolation audit.
        ta_ws, _ = _run(web_api.partner_repository.ensure_owner_workspace(TA_OWNER_ID))
        login_as(client, web_api, ta_ws.id, TA_OWNER_ID)
        yield client, web_api, ta_ws.id

    sys.modules.pop("app.web_api", None)


def _provision_independent(web_api, telegram_user_id: int, slug: str):
    return _run(web_api.partner_repository.provision_partner(
        telegram_user_id, "Independent Agent", slug,
        business_name="Мария Турагент", business_type="independent_agent",
        short_description="Подбираю туры под запрос клиента.",
        context={"specializations": ["Пляжный отдых"]},
    ))


async def _delete_business_profile(web_api, workspace_id: int) -> None:
    async with aiosqlite.connect(web_api.settings.journal_db_path) as db:
        await db.execute(
            "DELETE FROM partner_profiles WHERE workspace_id = ?", (workspace_id,),
        )
        await db.commit()


def _fake_generate(captured: dict):
    def _generate(**kwargs):
        captured.update(kwargs)
        return ChatResult(text="Ответ ассистента", usage=None)
    return _generate


def _fake_ta_bundle() -> KnowledgeBundle:
    item = SimpleNamespace(
        title="Member Bonus", content="Начисляется за первую покупку.",
        source_ref="TA KB", stable_key="ta.member_bonus",
    )
    return KnowledgeBundle(
        question="test", primary_items=(item,), related_items=(), facts=(),
        compliance_facts=(), examples=(), sources=(),
        potentially_ambiguous=False, ambiguity_reasons=(), missing_definitions=(),
    )


def _fake_intelligence(competitor_id: int, *, ta_comparison: tuple[str, ...]) -> CompetitorIntelligence:
    return CompetitorIntelligence(
        competitor_id=competitor_id, competitor_label="RivalCo", analyzed_at="now",
        positioning=(), products=(), destinations_and_categories=(), promotions=(),
        loyalty_mechanics=(), service_and_ux=(), strengths=(),
        travel_advantage_comparison=ta_comparison, fresh_signals=(),
        sources=(), opportunities=(),
    )


# ── static markup: fail-closed default, regardless of session ──────────────

def test_static_markup_ships_neutral_copy_not_ta_branded() -> None:
    """chat.html is served byte-identical to every workspace - the shipped
    default state must never be the TA/MWR wording; only a confirmed
    ta_affiliated=true session upgrades it client-side (applyTaAffiliatedUI()
    in chat.html), never trusting anything from the browser itself."""
    html = Path("app/templates/chat.html").read_text(encoding="utf-8")

    quick_action_block = html.split('id="qaKnowledge"', 1)[1].split("</button>", 1)[0]
    assert "Travel Advantage" not in quick_action_block

    nav_block = html.split('id="navKnowledge"', 1)[1].split(">", 1)[0]
    assert "Travel Advantage" not in nav_block

    knowledge_view_block = html.split('id="view-knowledge"', 1)[1].split("</section>", 1)[0]
    assert "Travel Advantage" not in knowledge_view_block
    assert "MWR" not in knowledge_view_block


# ── category A: ta_affiliated=true (workspace 1 / TA owner) ────────────────

def test_ta_affiliated_workspace_profile_reports_true(api) -> None:
    client, _, _ = api
    response = client.get("/api/profile")
    assert response.json()["business_profile"]["ta_affiliated"] is True


def test_ta_affiliated_workspace_chat_queries_and_includes_ta_knowledge(api) -> None:
    client, web_api, _ = api
    retrieve_mock = AsyncMock(return_value=_fake_ta_bundle())
    web_api.knowledge_service.retrieve = retrieve_mock

    captured: dict = {}
    web_api.chat_provider.generate = _fake_generate(captured)

    conversation_id = client.post("/api/conversations").json()["conversation"]["id"]
    response = client.post("/api/chat", json={
        "message": "Расскажи про Member Bonus", "conversation_id": conversation_id,
    })

    assert response.status_code == 200
    retrieve_mock.assert_awaited_once()
    assert "Member Bonus" in captured["knowledge_context"]


def test_ta_affiliated_workspace_competitor_analysis_is_called_with_ta_affiliated_true(api) -> None:
    client, web_api, workspace_id = api
    competitor = _run(web_api.competitor_repository.add_competitor(
        workspace_id, "https://rivalexample.com", label="RivalCo",
    ))

    captured_kwargs: dict = {}
    async def fake_analyze(comp, **kwargs):
        captured_kwargs.update(kwargs)
        return _fake_intelligence(comp.id, ta_comparison=("Travel Advantage — факт [источник: x]",))
    web_api.competitor_intelligence_service.analyze = fake_analyze
    web_api.chat_provider.generate = _fake_generate({})

    conversation_id = client.post("/api/conversations").json()["conversation"]["id"]
    response = client.post("/api/chat", json={
        "message": "Проанализируй конкурента rivalco", "conversation_id": conversation_id,
    })

    assert response.status_code == 200
    assert captured_kwargs.get("ta_affiliated") is True
    assert competitor.id  # sanity: competitor really exists in this workspace


# ── category B: ta_affiliated=false (independent workspace) ────────────────

def test_independent_workspace_profile_reports_false(api) -> None:
    client, web_api, _ = api
    provisioned = _provision_independent(web_api, INDEPENDENT_ID, "independent-profile-test")
    assert provisioned.profile.ta_affiliated is False

    with TestClient(web_api.app, base_url="https://testserver") as independent_client:
        login_as(
            independent_client, web_api, provisioned.workspace.id, INDEPENDENT_ID,
            email="independent-profile@example.com",
        )
        response = independent_client.get("/api/profile")

    assert response.json()["business_profile"]["ta_affiliated"] is False


def test_independent_workspace_chat_never_queries_ta_knowledge(api) -> None:
    client, web_api, _ = api
    provisioned = _provision_independent(web_api, INDEPENDENT_ID, "independent-chat-test")
    retrieve_mock = AsyncMock(return_value=_fake_ta_bundle())

    with TestClient(web_api.app, base_url="https://testserver") as independent_client:
        login_as(
            independent_client, web_api, provisioned.workspace.id, INDEPENDENT_ID,
            email="independent-chat@example.com",
        )
        web_api.knowledge_service.retrieve = retrieve_mock
        captured: dict = {}
        web_api.chat_provider.generate = _fake_generate(captured)

        conversation_id = independent_client.post("/api/conversations").json()["conversation"]["id"]
        response = independent_client.post("/api/chat", json={
            "message": "Расскажи про Member Bonus", "conversation_id": conversation_id,
        })

    assert response.status_code == 200
    retrieve_mock.assert_not_awaited()
    assert "Member Bonus" not in captured["knowledge_context"]


def test_independent_workspace_competitor_analysis_is_called_with_ta_affiliated_false(api) -> None:
    client, web_api, _ = api
    provisioned = _provision_independent(web_api, INDEPENDENT_ID, "independent-competitor-test")
    competitor = _run(web_api.competitor_repository.add_competitor(
        provisioned.workspace.id, "https://rivalexample.com", label="RivalCo",
    ))

    captured_kwargs: dict = {}
    async def fake_analyze(comp, **kwargs):
        captured_kwargs.update(kwargs)
        return _fake_intelligence(comp.id, ta_comparison=())
    web_api.competitor_intelligence_service.analyze = fake_analyze

    with TestClient(web_api.app, base_url="https://testserver") as independent_client:
        login_as(
            independent_client, web_api, provisioned.workspace.id, INDEPENDENT_ID,
            email="independent-competitor@example.com",
        )
        web_api.chat_provider.generate = _fake_generate({})

        conversation_id = independent_client.post("/api/conversations").json()["conversation"]["id"]
        response = independent_client.post("/api/chat", json={
            "message": "Проанализируй конкурента rivalco", "conversation_id": conversation_id,
        })

    assert response.status_code == 200
    assert captured_kwargs.get("ta_affiliated") is False
    assert competitor.id  # sanity: competitor really exists in this workspace


def test_independent_workspace_normal_features_still_work(api) -> None:
    """Tenant gating must not break ordinary functionality for an
    independent workspace: competitors/materials/history/chat all keep
    working, just without any TA/MWR content."""
    client, web_api, _ = api
    provisioned = _provision_independent(web_api, INDEPENDENT_ID, "independent-smoke-test")

    with TestClient(web_api.app, base_url="https://testserver") as independent_client:
        login_as(
            independent_client, web_api, provisioned.workspace.id, INDEPENDENT_ID,
            email="independent-smoke@example.com",
        )
        web_api.chat_provider.generate = _fake_generate({})

        competitors_response = independent_client.get("/api/competitors")
        assert competitors_response.status_code == 200
        assert "error" not in competitors_response.json()

        materials_response = independent_client.get("/api/materials")
        assert materials_response.status_code == 200
        assert "error" not in materials_response.json()

        conversation = independent_client.post("/api/conversations")
        assert conversation.status_code == 200
        conversation_id = conversation.json()["conversation"]["id"]

        chat_response = independent_client.post("/api/chat", json={
            "message": "Привет, расскажи, чем можешь помочь?",
            "conversation_id": conversation_id,
        })
        assert chat_response.status_code == 200
        assert "error" not in chat_response.json()

        history_response = independent_client.get("/api/conversations")
        assert history_response.status_code == 200


# ── fail-closed: BusinessProfile missing entirely ───────────────────────────

def test_missing_business_profile_behaves_as_not_ta_affiliated_in_chat(api) -> None:
    client, web_api, _ = api
    provisioned = _provision_independent(web_api, INDEPENDENT_ID, "no-profile-chat-test")
    _run(_delete_business_profile(web_api, provisioned.workspace.id))
    assert _run(web_api.partner_repository.get_business_profile(provisioned.workspace.id)) is None

    retrieve_mock = AsyncMock(return_value=_fake_ta_bundle())

    with TestClient(web_api.app, base_url="https://testserver") as no_profile_client:
        login_as(
            no_profile_client, web_api, provisioned.workspace.id, INDEPENDENT_ID,
            email="no-profile-chat@example.com",
        )
        web_api.knowledge_service.retrieve = retrieve_mock
        web_api.chat_provider.generate = _fake_generate({})

        conversation_id = no_profile_client.post("/api/conversations").json()["conversation"]["id"]
        response = no_profile_client.post("/api/chat", json={
            "message": "Привет", "conversation_id": conversation_id,
        })

    assert response.status_code == 200
    assert "error" not in response.json()
    retrieve_mock.assert_not_awaited()


def test_missing_business_profile_behaves_as_not_ta_affiliated_in_competitor_analysis(api) -> None:
    client, web_api, _ = api
    provisioned = _provision_independent(web_api, INDEPENDENT_ID, "no-profile-competitor-test")
    competitor = _run(web_api.competitor_repository.add_competitor(
        provisioned.workspace.id, "https://rivalexample.com", label="RivalCo",
    ))
    _run(_delete_business_profile(web_api, provisioned.workspace.id))
    assert _run(web_api.partner_repository.get_business_profile(provisioned.workspace.id)) is None

    captured_kwargs: dict = {}
    async def fake_analyze(comp, **kwargs):
        captured_kwargs.update(kwargs)
        return _fake_intelligence(comp.id, ta_comparison=())
    web_api.competitor_intelligence_service.analyze = fake_analyze

    with TestClient(web_api.app, base_url="https://testserver") as no_profile_client:
        login_as(
            no_profile_client, web_api, provisioned.workspace.id, INDEPENDENT_ID,
            email="no-profile-competitor@example.com",
        )
        web_api.chat_provider.generate = _fake_generate({})

        conversation_id = no_profile_client.post("/api/conversations").json()["conversation"]["id"]
        response = no_profile_client.post("/api/chat", json={
            "message": "Проанализируй конкурента rivalco", "conversation_id": conversation_id,
        })

    assert response.status_code == 200
    assert captured_kwargs.get("ta_affiliated") is False
    assert competitor.id  # sanity: competitor really exists in this workspace
