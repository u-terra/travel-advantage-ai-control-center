"""FinOps Monitor endpoint (/api/finops/status, app.services.finops).

Read-only ORCHESTRAVEL spend/limit status across OpenAI/Yandex/Beget.
Covers: an unavailable provider never breaks the overall response, and
no secret (API key) ever leaks into the response body.
"""

from __future__ import annotations

import asyncio

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("markdown")
pytest.importorskip("argon2")

from fastapi.testclient import TestClient  # noqa: E402

from tests._web_auth_test_helpers import login_as  # noqa: E402
from app.services.finops import FinOpsConfig, FinOpsService  # noqa: E402

ADMIN_ID = 111000111
ADMIN_EMAIL = "admin@example.com"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def api(tmp_path, monkeypatch):
    db_path = tmp_path / "journal.sqlite3"
    monkeypatch.setenv("JOURNAL_DB_PATH", str(db_path))
    monkeypatch.setenv("PLANNER_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("ORCHESTRAVEL_ADMIN_EMAILS", ADMIN_EMAIL)
    monkeypatch.delenv("ORCHESTRATION_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("YANDEX_SEARCH_API_KEY", raising=False)

    import sys
    sys.modules.pop("app.web_api", None)
    import app.web_api as web_api

    with TestClient(web_api.app, base_url="https://testserver") as client:
        admin_ws, _ = _run(web_api.partner_repository.ensure_owner_workspace(ADMIN_ID))
        yield web_api, admin_ws.id

    sys.modules.pop("app.web_api", None)


def _admin_client(web_api, admin_workspace_id) -> TestClient:
    client = TestClient(web_api.app, base_url="https://testserver")
    login_as(client, web_api, admin_workspace_id, ADMIN_ID, email=ADMIN_EMAIL)
    return client


def test_finops_status_reports_all_providers_and_unavailable_does_not_break_response(api) -> None:
    web_api, admin_ws = api
    client = _admin_client(web_api, admin_ws)

    resp = client.get("/api/finops/status")
    assert resp.status_code == 200
    body = resp.json()

    providers = {p["provider"]: p for p in body["providers"]}
    assert set(providers) == {"openai", "yandex", "beget"}
    # No credentials configured in this fixture -> all three read as
    # unavailable, but the endpoint still returns 200 with a full report
    # for every provider (one missing provider never breaks the others).
    for name, report in providers.items():
        assert report["status"] in ("ok", "warning", "unavailable")
        assert report["checked_at"]
        assert report["message"]
    assert providers["beget"]["status"] == "unavailable"
    assert providers["yandex"]["status"] == "unavailable"


def test_finops_status_requires_platform_admin(api) -> None:
    web_api, admin_ws = api
    anon_client = TestClient(web_api.app, base_url="https://testserver")

    resp = anon_client.get("/api/finops/status")
    assert resp.status_code in (401, 403, 404)


def test_finops_ok_provider_shape(monkeypatch) -> None:
    """A provider that is reachable reports status=ok without balance/limit
    it cannot actually read (see app/services/finops.py docstring)."""
    service = FinOpsService(FinOpsConfig(openai_api_key="", yandex_search_api_key=""))
    reports = service.check_all()
    assert len(reports) == 3
    for report in reports:
        assert report.status == "unavailable"
        assert report.balance is None
        assert report.usage is None
        assert report.limit is None


def test_finops_response_never_contains_api_key(api, monkeypatch) -> None:
    web_api, admin_ws = api
    client = _admin_client(web_api, admin_ws)

    secret = "sk-test-super-secret-openai-key-value"
    web_api._finops_service = FinOpsService(FinOpsConfig(
        openai_api_key=secret, yandex_search_api_key="yandex-secret-value",
    ))

    resp = client.get("/api/finops/status")
    assert secret not in resp.text
    assert "yandex-secret-value" not in resp.text
