"""POST /api/partners/discover, /api/partners/contact, /api/partners/outreach
- Web parity for the Telegram "🔎 Найти потенциальных партнёров" flow.

Reuses the exact same PartnerDiscoveryService / contact_discovery.find_contact
/ app.services.partner_outreach logic Telegram already exercises (see
tests/test_partner_discovery_handlers.py and tests/test_partner_contact_handlers.py)
- here we only verify the Web wiring (auth/CSRF/subscription gate, club_partner
gate, request/response shape, error states).

Requires the web-only dependencies (requirements-web.txt). Skips cleanly when
not installed.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("markdown")

from fastapi.testclient import TestClient  # noqa: E402

from app.domain.contacts import ContactCandidate, ContactConfidence, ContactType  # noqa: E402
from app.domain.prospects import EvidenceSource, ProspectCandidate, ProspectFit, ProspectType  # noqa: E402

from tests._web_auth_test_helpers import login_as  # noqa: E402

OWNER_ID = 586249067


def _run(coro):
    return asyncio.run(coro)


def _login(client, web_api):
    ws, _ = _run(web_api.partner_repository.ensure_owner_workspace(OWNER_ID))
    login_as(client, web_api, ws.id, OWNER_ID)
    return ws.id


@pytest.fixture
def api(tmp_path, monkeypatch):
    db_path = tmp_path / "journal.sqlite3"
    monkeypatch.setenv("JOURNAL_DB_PATH", str(db_path))
    monkeypatch.setenv("PLANNER_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "false")

    import sys
    sys.modules.pop("app.web_api", None)
    import app.web_api as web_api

    with TestClient(web_api.app, base_url="https://testserver") as client:
        yield client, web_api, db_path

    sys.modules.pop("app.web_api", None)


def _candidate_dict(name="Example Club", url="https://example-club.ru/"):
    return {
        "name": name, "url": url, "prospect_type": "travel_club",
        "query": "клуб путешественников", "evidence": "Сообщество гидов и путешественников.",
        "evidence_source": "page", "fit": "STRONG",
        "display_name": "Example Club", "reason": "Сообщество путешественников.",
    }


# ── POST /api/partners/discover ──────────────────────────────────────────

def test_discover_requires_club_partner_business_type(api) -> None:
    client, web_api, _ = api
    ws_id = _login(client, web_api)
    _run(web_api.partner_repository.update_business_profile(
        ws_id, 1, business_name="Test Agency", business_type="agency",
        short_description="Travel agency.", context={},
    ))

    response = client.post("/api/partners/discover")

    assert response.status_code == 200
    body = response.json()
    assert "error" in body
    assert body["candidates"] == []


def test_discover_returns_no_results_without_search_provider(api) -> None:
    client, web_api, _ = api
    _login(client, web_api)
    # WEB_SEARCH_ENABLED=false in the fixture -> web_search_service.provider is None.
    assert web_api.web_search_service.provider is None

    response = client.post("/api/partners/discover")

    assert response.status_code == 200
    body = response.json()
    assert "error" in body
    assert body["candidates"] == []


def test_discover_returns_enriched_candidates(api) -> None:
    client, web_api, _ = api
    _login(client, web_api)

    prospect = ProspectCandidate(
        name="Example Club | клуб путешественников", url="https://example-club.ru/",
        prospect_type=ProspectType.TRAVEL_CLUB, query="клуб путешественников",
        evidence="Сообщество гидов и путешественников со своей аудиторией.",
        evidence_source=EvidenceSource.PAGE, fit=ProspectFit.STRONG, rank_score=10,
    )

    async def _fake_find_candidates(self, *, rotation_seed=None, use_all_variants=False, **_kw):
        return [prospect]

    with patch.object(web_api, "web_search_service") as fake_service:
        fake_service.provider = object()
        with patch(
            "app.web_api.PartnerDiscoveryService.find_candidates", _fake_find_candidates,
        ):
            response = client.post("/api/partners/discover")

    assert response.status_code == 200
    body = response.json()
    assert "error" not in body
    assert len(body["candidates"]) == 1
    candidate = body["candidates"][0]
    assert candidate["url"] == "https://example-club.ru/"
    # LLM provider has no real API key in tests -> deterministic fallback enrichment.
    assert "display_name" in candidate
    assert "reason" in candidate


# ── POST /api/partners/contact ───────────────────────────────────────────

def test_contact_requires_club_partner_business_type(api) -> None:
    client, web_api, _ = api
    ws_id = _login(client, web_api)
    _run(web_api.partner_repository.update_business_profile(
        ws_id, 1, business_name="Test Agency", business_type="agency",
        short_description="Travel agency.", context={},
    ))

    response = client.post("/api/partners/contact", json={"candidate": _candidate_dict()})

    assert response.status_code == 200
    body = response.json()
    assert "error" in body
    assert body["contact"] is None


def test_contact_returns_person_when_found(api) -> None:
    client, web_api, _ = api
    _login(client, web_api)

    fake_contact = ContactCandidate(
        contact_type=ContactType.PERSON, display_name="Иванова Мария Сергеевна",
        project_name="Example Club", public_url="https://example-club.ru/",
        evidence_text="Основатель клуба путешественников.",
        evidence_url="https://example-club.ru/about",
        confidence=ContactConfidence.HIGH, role="основатель", provenance="page",
    )

    async def _fake_find_contact(*args, **kwargs):
        return fake_contact

    with patch("app.web_api.find_contact", _fake_find_contact):
        response = client.post("/api/partners/contact", json={"candidate": _candidate_dict()})

    assert response.status_code == 200
    body = response.json()
    assert "error" not in body
    assert body["contact"]["contact_type"] == "person"
    assert body["contact"]["display_name"] == "Иванова Мария Сергеевна"


def test_contact_rejects_malformed_candidate(api) -> None:
    client, web_api, _ = api
    _login(client, web_api)

    response = client.post("/api/partners/contact", json={"candidate": {"name": "x"}})

    assert response.status_code == 200
    body = response.json()
    assert "error" in body
    assert body["contact"] is None


# ── POST /api/partners/outreach ──────────────────────────────────────────

def _person_contact_dict():
    return {
        "contact_type": "person", "display_name": "Иванова Мария Сергеевна",
        "role": "основатель", "project_name": "Example Club",
        "public_url": "https://example-club.ru/",
        "evidence_text": "Основатель клуба путешественников.",
        "evidence_url": "https://example-club.ru/about",
        "confidence": "HIGH", "provenance": "page",
    }


def test_outreach_rejects_non_person_contact(api) -> None:
    client, web_api, _ = api
    _login(client, web_api)

    response = client.post("/api/partners/outreach", json={
        "contact": {"contact_type": "project", "project_name": "Example Club"},
    })

    assert response.status_code == 200
    body = response.json()
    assert "error" in body
    assert body["draft"] is None


def test_outreach_generates_fallback_draft_without_llm_key(api) -> None:
    client, web_api, _ = api
    _login(client, web_api)
    # No real OPENAI key configured for chat_provider in this fixture's chat_provider?
    # PLANNER_OPENAI_API_KEY is set to a non-empty test key, so the (mocked) LLM path
    # is exercised; force the deterministic fallback by making generate() raise.
    web_api.chat_provider.generate = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom"))

    response = client.post("/api/partners/outreach", json={"contact": _person_contact_dict()})

    assert response.status_code == 200
    body = response.json()
    assert "error" not in body
    assert body["draft"]
    assert "Иванова" not in body["draft"] or "Мария" in body["draft"]


def test_outreach_second_workspace_sees_claimed_by_other(api) -> None:
    client, web_api, db_path = api
    _login(client, web_api)
    web_api.chat_provider.generate = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom"))

    first = client.post("/api/partners/outreach", json={"contact": _person_contact_dict()})
    assert "error" not in first.json()

    # A second, different workspace tries the same contact (same evidence_url
    # + display_name) - must be blocked by the shared ContactClaimRepository,
    # the same cross-tenant guard Telegram already uses.
    from app.repositories.partner_repository import empty_business_context

    context = empty_business_context()
    provisioned = _run(web_api.partner_repository.provision_partner(
        999999001, "Other Partner", "other-partner",
        business_name="Other Partner", business_type="club_partner",
        short_description="Другой клуб.", context=context,
    ))
    other_client = TestClient(web_api.app, base_url="https://testserver")
    login_as(
        other_client, web_api, provisioned.workspace.id, 999999001,
        email="other@example.com",
    )

    second = other_client.post("/api/partners/outreach", json={"contact": _person_contact_dict()})
    body = second.json()
    assert "error" in body
    assert body["draft"] is None


# ── discover: zero results, single-flight, dropped request ───────────────

def test_discover_zero_results_is_plain_empty_list_not_error(api) -> None:
    """Legit "searched, nobody qualified" -> 200 + empty list, no error key.
    The UI must therefore tell this apart from the idle screen itself (see
    test_frontend_zero_results_do_not_reset_to_idle_screen)."""
    client, web_api, _ = api
    _login(client, web_api)

    async def _fake_find_candidates(self, *, rotation_seed=None, use_all_variants=False, **_kw):
        return []

    with patch.object(web_api, "web_search_service") as fake_service:
        fake_service.provider = object()
        with patch("app.web_api.PartnerDiscoveryService.find_candidates", _fake_find_candidates):
            response = client.post("/api/partners/discover")

    assert response.status_code == 200
    assert response.json() == {"candidates": []}


def test_discover_concurrent_requests_share_one_pipeline_run(api) -> None:
    """A repeat click / retry while a run is in flight must join it, not
    start a second full (LLM-billed) pipeline."""
    _, web_api, _ = api
    calls = 0
    release = asyncio.Event()

    async def _slow_find_candidates(self, *, rotation_seed=None, use_all_variants=False, **_kw):
        nonlocal calls
        calls += 1
        await release.wait()
        return []

    principal = type("P", (), {"workspace_id": 1})()

    async def scenario():
        with patch.object(web_api, "_web_is_club_partner", AsyncMock(return_value=True)), \
                patch.object(web_api, "web_search_service") as fake_service, \
                patch("app.web_api.PartnerDiscoveryService.find_candidates", _slow_find_candidates):
            fake_service.provider = object()
            first = asyncio.create_task(web_api.discover_partners(principal))
            await asyncio.sleep(0)
            second = asyncio.create_task(web_api.discover_partners(principal))
            await asyncio.sleep(0.01)
            release.set()
            return await asyncio.gather(first, second)

    results = _run(scenario())

    assert calls == 1
    assert results == [{"candidates": []}, {"candidates": []}]
    assert web_api._partner_discovery_inflight == {}


def test_discover_run_survives_dropped_request_and_retry_joins_it(api) -> None:
    """Browser drops the request (waiter cancelled): the search keeps
    running, a retry gets its result, and no second run is started."""
    _, web_api, _ = api
    calls = 0
    release = asyncio.Event()
    prospect = ProspectCandidate(
        name="Example Club", url="https://example-club.ru/",
        prospect_type=ProspectType.TRAVEL_CLUB, query="q", evidence="e",
        evidence_source=EvidenceSource.PAGE, fit=ProspectFit.STRONG, rank_score=10,
    )

    async def _slow_find_candidates(self, *, rotation_seed=None, use_all_variants=False, **_kw):
        nonlocal calls
        calls += 1
        await release.wait()
        return [prospect]

    principal = type("P", (), {"workspace_id": 1})()

    async def scenario():
        with patch.object(web_api, "_web_is_club_partner", AsyncMock(return_value=True)), \
                patch.object(web_api, "_web_is_ta_affiliated", AsyncMock(return_value=False)), \
                patch.object(web_api, "web_search_service") as fake_service, \
                patch("app.web_api.PartnerDiscoveryService.find_candidates", _slow_find_candidates):
            fake_service.provider = object()
            dropped = asyncio.create_task(web_api.discover_partners(principal))
            await asyncio.sleep(0.01)
            dropped.cancel()  # client disconnect
            await asyncio.gather(dropped, return_exceptions=True)
            assert 1 in web_api._partner_discovery_inflight  # run not killed

            retry = asyncio.create_task(web_api.discover_partners(principal))
            await asyncio.sleep(0.01)
            release.set()
            return await retry

    body = _run(scenario())

    assert calls == 1
    assert len(body["candidates"]) == 1


# ── frontend regression: zero results must not look like a reset ─────────

def _chat_html() -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parents[1] / "app" / "templates" / "chat.html").read_text(
        encoding="utf-8"
    )


def _discover_partners_js(html: str) -> str:
    start = html.index("let partnersSearchRunning")
    return html[start:html.index("async function findPartnerContact", start)]


def test_frontend_zero_results_do_not_reset_to_idle_screen() -> None:
    """Regression (2026-10-12): an empty candidate list used to call
    renderPartnersEmpty() - the initial intro screen - so a finished search
    with no results looked like the page had silently reset."""
    js = _discover_partners_js(_chat_html())

    assert "renderPartnersEmpty" not in js
    assert "renderPartnersNoResults()" in js


def test_frontend_blocks_duplicate_clicks_and_has_client_timeout() -> None:
    js = _discover_partners_js(_chat_html())

    assert "if (partnersSearchRunning) return;" in js
    assert "partnersDiscoverBtn.disabled = true" in js
    assert "partnersDiscoverBtn.disabled = false" in js  # restored in finally
    assert "AbortController" in js
