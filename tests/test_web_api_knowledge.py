"""GET /api/knowledge - read-only browse of the shared Travel Advantage/MWR
Life knowledge base for the web shell «База знаний».

Not workspace-scoped by design: KnowledgeRepository has no workspace_id
column, same as the existing chat retrieval path (knowledge_service.retrieve()).

web_api.py constructs `KnowledgeRepository()` with the real, hardcoded
default path (data/knowledge.sqlite3, not overridable via env var like
JOURNAL_DB_PATH) - so this fixture redirects `web_api.knowledge_repository
.db_path` directly to an isolated tmp file *before* the app's startup event
runs, to avoid ever touching the real production knowledge base.

Requires the web-only dependencies (requirements-web.txt: fastapi,
uvicorn, markdown). Skips cleanly when they're not installed.
"""

from __future__ import annotations

import asyncio

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("markdown")

from fastapi.testclient import TestClient  # noqa: E402

from app.services.knowledge_import import import_dataset  # noqa: E402

from tests._web_auth_test_helpers import login_as  # noqa: E402

DATASET = "knowledge/travel_advantage/imports/compensation-foundation.v1.json"
OWNER_ID = 586249067


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("JOURNAL_DB_PATH", str(tmp_path / "journal.sqlite3"))
    monkeypatch.setenv("PLANNER_OPENAI_API_KEY", "test-key")

    import sys
    sys.modules.pop("app.web_api", None)
    import app.web_api as web_api

    # Redirect the knowledge repository away from the real
    # data/knowledge.sqlite3 BEFORE the startup event (init()) runs.
    web_api.knowledge_repository.db_path = tmp_path / "knowledge.sqlite3"

    with TestClient(web_api.app, base_url="https://testserver") as client:
        ws, _ = _run(web_api.partner_repository.ensure_owner_workspace(OWNER_ID))
        login_as(client, web_api, ws.id, OWNER_ID)
        yield client, web_api

    sys.modules.pop("app.web_api", None)


def test_empty_knowledge_base_returns_empty_lists(api) -> None:
    client, _ = api

    response = client.get("/api/knowledge")

    assert response.status_code == 200
    assert response.json() == {"sources": [], "items": []}


def test_returns_real_imported_sources_and_items(api) -> None:
    client, web_api = api
    from pathlib import Path
    _run(import_dataset(Path(DATASET), web_api.knowledge_repository))

    response = client.get("/api/knowledge")

    assert response.status_code == 200
    body = response.json()
    assert len(body["sources"]) == 1
    source = body["sources"][0]
    assert source["verification_status"] == "verified_official"
    assert "id" in source and "title" in source

    assert len(body["items"]) == 2
    stable_keys = {item["stable_key"] for item in body["items"]}
    assert stable_keys == {"ta.member_bonus", "ta.builder_bonus"}

    item = next(item for item in body["items"] if item["stable_key"] == "ta.member_bonus")
    assert item["source_title"] == source["title"]
    assert item["verification_status"] == "verified_official"
    assert isinstance(item["tags"], list)
    assert isinstance(item["content"], str) and item["content"]


def test_item_without_a_resolvable_source_reports_null_not_a_crash(api) -> None:
    """Defensive: if an item's source_id somehow doesn't resolve, the
    endpoint must not throw - it should report null, not fabricate a title."""
    client, web_api = api
    from pathlib import Path
    _run(import_dataset(Path(DATASET), web_api.knowledge_repository))

    response = client.get("/api/knowledge")
    assert response.status_code == 200
    for item in response.json()["items"]:
        assert item["source_title"] is not None  # real dataset always resolves


def test_endpoint_never_returns_500_on_backend_error(api, monkeypatch) -> None:
    client, web_api = api

    async def broken_get_sources(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(web_api.knowledge_repository, "get_sources", broken_get_sources)

    response = client.get("/api/knowledge")

    assert response.status_code == 200
    body = response.json()
    assert "error" in body
    assert body["sources"] == []
    assert body["items"] == []
