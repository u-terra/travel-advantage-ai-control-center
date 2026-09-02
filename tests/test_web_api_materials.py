"""GET /api/materials, GET /api/materials/{id} - read-only Artifact browse
for the web shell «Материалы». Same ArtifactRepository and same
workspace-scoped queries as Telegram's «📚 Мои материалы»
(app/handlers/materials.py: list_artifacts / get_artifact /
get_current_artifact_version).

Requires the web-only dependencies (requirements-web.txt: fastapi,
uvicorn, markdown). Skips cleanly when they're not installed.
"""

from __future__ import annotations

import asyncio

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("markdown")

from fastapi.testclient import TestClient  # noqa: E402


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


def test_empty_workspace_returns_empty_list(api) -> None:
    client, _, _ = api

    response = client.get("/api/materials")

    assert response.status_code == 200
    assert response.json() == {"materials": []}


def test_lists_real_saved_artifact(api) -> None:
    client, web_api, _ = api
    _run(web_api.artifact_repository.create_artifact_with_initial_version(
        web_api.WEB_WORKSPACE_ID, artifact_type="post", title="Пост про Азию",
        content="Готовый текст поста.",
    ))

    response = client.get("/api/materials")

    assert response.status_code == 200
    body = response.json()
    assert len(body["materials"]) == 1
    material = body["materials"][0]
    assert material["title"] == "Пост про Азию"
    assert material["artifact_type"] == "post"
    assert material["status"] == "draft"
    assert material["created_at"]
    assert material["updated_at"]


def test_material_detail_returns_current_version_content(api) -> None:
    client, web_api, _ = api
    artifact, version = _run(web_api.artifact_repository.create_artifact_with_initial_version(
        web_api.WEB_WORKSPACE_ID, artifact_type="faq", title="FAQ по бронированию",
        content="Полный текст FAQ.",
    ))

    response = client.get(f"/api/materials/{artifact.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["material"]["id"] == artifact.id
    assert body["material"]["title"] == "FAQ по бронированию"
    assert body["version"]["version_number"] == 1
    assert body["version"]["content"] == "Полный текст FAQ."


def test_material_detail_reflects_latest_version(api) -> None:
    client, web_api, _ = api
    artifact, _ = _run(web_api.artifact_repository.create_artifact_with_initial_version(
        web_api.WEB_WORKSPACE_ID, artifact_type="post", title="Пост",
        content="Версия 1.",
    ))
    _run(web_api.artifact_repository.add_artifact_version(
        web_api.WEB_WORKSPACE_ID, artifact.id, "Версия 2.",
    ))

    response = client.get(f"/api/materials/{artifact.id}")

    body = response.json()
    assert body["version"]["version_number"] == 2
    assert body["version"]["content"] == "Версия 2."


def test_unknown_material_id_has_no_500(api) -> None:
    client, _, _ = api

    response = client.get("/api/materials/999999")

    assert response.status_code == 200
    body = response.json()
    assert body["material"] is None
    assert body["version"] is None
    assert "error" in body


def test_materials_isolated_by_workspace(api) -> None:
    client, web_api, _ = api
    other = _run(web_api.partner_repository.provision_partner(
        222333777, "Other Agency", "other-agency-materials",
        business_name="Other Agency", business_type="independent_agent",
        short_description="Другое рабочее пространство.",
        context={},
    ))
    _run(web_api.artifact_repository.create_artifact_with_initial_version(
        other.workspace.id, artifact_type="post", title="Чужой пост",
        content="Чужой текст.",
    ))
    _run(web_api.artifact_repository.create_artifact_with_initial_version(
        web_api.WEB_WORKSPACE_ID, artifact_type="post", title="Мой пост",
        content="Мой текст.",
    ))

    response = client.get("/api/materials")

    titles = [item["title"] for item in response.json()["materials"]]
    assert titles == ["Мой пост"]


def test_material_detail_not_leaked_across_workspaces(api) -> None:
    client, web_api, _ = api
    other = _run(web_api.partner_repository.provision_partner(
        222333888, "Other Agency 2", "other-agency-materials-2",
        business_name="Other Agency 2", business_type="independent_agent",
        short_description="Другое рабочее пространство.",
        context={},
    ))
    foreign_artifact, _ = _run(web_api.artifact_repository.create_artifact_with_initial_version(
        other.workspace.id, artifact_type="post", title="Чужой пост",
        content="Чужой текст.",
    ))

    response = client.get(f"/api/materials/{foreign_artifact.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["material"] is None
    assert "error" in body
