"""GET /api/history - read-only activity view for the web shell «История /
Артефакты»: real usage_ledger_repository events (already recorded by every
chat/competitor-analysis call - see record_llm_call()) plus a status
breakdown of real saved Artifacts. No chat-message history is fabricated -
there is nothing server-side to read for that (Assistant conversation only
lives in the browser's sessionStorage today).

Requires the web-only dependencies (requirements-web.txt: fastapi,
uvicorn, markdown). Skips cleanly when they're not installed.
"""

from __future__ import annotations

import asyncio

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("markdown")

from fastapi.testclient import TestClient  # noqa: E402

from app.domain.usage import UsageStatus  # noqa: E402


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

    with TestClient(web_api.app) as client:
        _run(web_api.partner_repository.ensure_owner_workspace(web_api.WEB_TELEGRAM_USER_ID))
        yield client, web_api, db_path

    sys.modules.pop("app.web_api", None)


def test_empty_workspace_returns_zero_counts_and_no_events(api) -> None:
    client, _, _ = api

    response = client.get("/api/history")

    assert response.status_code == 200
    body = response.json()
    assert body["usage_events"] == []
    assert body["artifact_status_counts"] == {
        "draft": 0, "review_required": 0, "ready": 0, "used": 0, "archived": 0,
    }


def test_returns_real_recorded_usage_event(api) -> None:
    client, web_api, _ = api
    _run(web_api.usage_ledger_repository.record(
        workspace_id=web_api.WEB_WORKSPACE_ID, telegram_user_id=None,
        module="web_chat", provider="openai", model="gpt-5.6-terra",
        input_tokens=100, output_tokens=50, total_tokens=150,
        estimated_cost_usd=0.01, status=UsageStatus.SUCCESS,
    ))

    response = client.get("/api/history")

    assert response.status_code == 200
    events = response.json()["usage_events"]
    assert len(events) == 1
    assert events[0]["module"] == "web_chat"
    assert events[0]["provider"] == "openai"
    assert events[0]["model"] == "gpt-5.6-terra"
    assert events[0]["status"] == "success"
    assert events[0]["occurred_at"]


def test_response_never_leaks_token_or_cost_fields_as_secrets(api) -> None:
    """Not a secrets concern by itself, but the endpoint contract is a
    small, explicit projection - assert it stays that way (module/provider/
    model/status/occurred_at only) rather than silently growing."""
    client, web_api, _ = api
    _run(web_api.usage_ledger_repository.record(
        workspace_id=web_api.WEB_WORKSPACE_ID, telegram_user_id=None,
        module="web_chat", provider="openai", model="gpt-5.6-terra",
        input_tokens=100, output_tokens=50, total_tokens=150,
        estimated_cost_usd=0.01, status=UsageStatus.SUCCESS,
    ))

    response = client.get("/api/history")
    event = response.json()["usage_events"][0]

    assert set(event.keys()) == {"occurred_at", "module", "provider", "model", "status"}


def test_artifact_status_counts_reflect_real_saved_artifacts(api) -> None:
    client, web_api, _ = api
    a, _ = _run(web_api.artifact_repository.create_artifact_with_initial_version(
        web_api.WEB_WORKSPACE_ID, artifact_type="post", title="A", content="x",
    ))
    b, _ = _run(web_api.artifact_repository.create_artifact_with_initial_version(
        web_api.WEB_WORKSPACE_ID, artifact_type="post", title="B", content="x",
    ))
    _run(web_api.artifact_repository.update_artifact_status(web_api.WEB_WORKSPACE_ID, b.id, "ready"))

    response = client.get("/api/history")

    counts = response.json()["artifact_status_counts"]
    assert counts["draft"] == 1
    assert counts["ready"] == 1
    assert counts["used"] == 0


def test_history_isolated_by_workspace(api) -> None:
    client, web_api, _ = api
    other = _run(web_api.partner_repository.provision_partner(
        222333999, "Other Agency", "other-agency-history",
        business_name="Other Agency", business_type="independent_agent",
        short_description="Другое рабочее пространство.",
        context={},
    ))
    _run(web_api.usage_ledger_repository.record(
        workspace_id=other.workspace.id, telegram_user_id=None,
        module="web_chat", provider="openai", model="gpt-5.6-terra",
        input_tokens=None, output_tokens=None, total_tokens=None,
        estimated_cost_usd=None, status=UsageStatus.SUCCESS,
    ))
    _run(web_api.artifact_repository.create_artifact_with_initial_version(
        other.workspace.id, artifact_type="post", title="Чужой", content="x",
    ))

    response = client.get("/api/history")

    body = response.json()
    assert body["usage_events"] == []
    assert body["artifact_status_counts"]["draft"] == 0


def test_endpoint_never_returns_500_on_backend_error(api, monkeypatch) -> None:
    client, web_api, _ = api

    async def broken_list(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(web_api.usage_ledger_repository, "list_for_workspace", broken_list)

    response = client.get("/api/history")

    assert response.status_code == 200
    body = response.json()
    assert "error" in body
    assert body["usage_events"] == []
