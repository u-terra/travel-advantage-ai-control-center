"""GET /api/materials, GET /api/materials/{id} - read-only Artifact browse
for the web shell «Материалы». Same ArtifactRepository and same
workspace-scoped queries as Telegram's «📚 Мои материалы»
(app/handlers/materials.py: list_artifacts / get_artifact /
get_current_artifact_version).

PUT /api/materials/{id} and DELETE /api/materials/{id} add real edit/delete
capability on top of the same ArtifactRepository - edit reuses
add_artifact_version_if_current(), the exact optimistic-concurrency
versioning pattern Telegram's Safety Layer edit flow already uses
(app/handlers/text_review.py), no parallel storage; delete uses the new
delete_artifact() repository method (see tests/test_artifact_repository.py
for its own isolation/cascade coverage).

Requires the web-only dependencies (requirements-web.txt: fastapi,
uvicorn, markdown). Skips cleanly when they're not installed.
"""

from __future__ import annotations

import asyncio

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("markdown")

from fastapi.testclient import TestClient  # noqa: E402

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
        yield client, web_api, db_path, ws.id

    sys.modules.pop("app.web_api", None)


def test_empty_workspace_returns_empty_list(api) -> None:
    client, _, _, workspace_id = api

    response = client.get("/api/materials")

    assert response.status_code == 200
    assert response.json() == {"materials": []}


def test_lists_real_saved_artifact(api) -> None:
    client, web_api, _, workspace_id = api
    _run(web_api.artifact_repository.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост про Азию",
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
    client, web_api, _, workspace_id = api
    artifact, version = _run(web_api.artifact_repository.create_artifact_with_initial_version(
        workspace_id, artifact_type="faq", title="FAQ по бронированию",
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
    client, web_api, _, workspace_id = api
    artifact, _ = _run(web_api.artifact_repository.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост",
        content="Версия 1.",
    ))
    _run(web_api.artifact_repository.add_artifact_version(
        workspace_id, artifact.id, "Версия 2.",
    ))

    response = client.get(f"/api/materials/{artifact.id}")

    body = response.json()
    assert body["version"]["version_number"] == 2
    assert body["version"]["content"] == "Версия 2."


def test_unknown_material_id_has_no_500(api) -> None:
    client, _, _, workspace_id = api

    response = client.get("/api/materials/999999")

    assert response.status_code == 200
    body = response.json()
    assert body["material"] is None
    assert body["version"] is None
    assert "error" in body


def test_materials_isolated_by_workspace(api) -> None:
    client, web_api, _, workspace_id = api
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
        workspace_id, artifact_type="post", title="Мой пост",
        content="Мой текст.",
    ))

    response = client.get("/api/materials")

    titles = [item["title"] for item in response.json()["materials"]]
    assert titles == ["Мой пост"]


def test_material_detail_not_leaked_across_workspaces(api) -> None:
    client, web_api, _, workspace_id = api
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


# ── PUT /api/materials/{id}: edit = new version, same versioning model ──────

def test_edit_creates_a_new_version_not_a_parallel_record(api) -> None:
    client, web_api, _, workspace_id = api
    artifact, version = _run(web_api.artifact_repository.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост",
        content="Исходный текст.",
    ))

    response = client.put(
        f"/api/materials/{artifact.id}",
        json={"content": "Отредактированный текст.", "expected_version_id": version.id},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["version"]["version_number"] == 2
    assert body["version"]["content"] == "Отредактированный текст."

    # действительно версия того же artifact, а не новая параллельная запись
    versions = _run(web_api.artifact_repository.list_artifact_versions(
        workspace_id, artifact.id,
    ))
    assert [v.content for v in versions] == ["Исходный текст.", "Отредактированный текст."]
    materials = _run(web_api.artifact_repository.list_artifacts(workspace_id))
    assert len(materials) == 1


def test_edit_rejects_empty_content(api) -> None:
    client, web_api, _, workspace_id = api
    artifact, version = _run(web_api.artifact_repository.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост", content="Текст.",
    ))

    response = client.put(
        f"/api/materials/{artifact.id}",
        json={"content": "   ", "expected_version_id": version.id},
    )

    assert response.status_code == 200
    body = response.json()
    assert "error" in body
    assert _run(web_api.artifact_repository.list_artifact_versions(
        workspace_id, artifact.id,
    )) == [version]


def test_edit_with_stale_expected_version_id_fails_without_overwriting(api) -> None:
    """Optimistic concurrency: editing against a version_id that's no longer
    current must not silently overwrite whatever changed in between."""
    client, web_api, _, workspace_id = api
    artifact, version = _run(web_api.artifact_repository.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост", content="v1",
    ))
    _run(web_api.artifact_repository.add_artifact_version(
        workspace_id, artifact.id, "v2 (сохранена в другом месте)",
    ))

    response = client.put(
        f"/api/materials/{artifact.id}",
        json={"content": "Конфликтующая правка", "expected_version_id": version.id},
    )

    assert response.status_code == 200
    body = response.json()
    assert "error" in body
    assert body["material"] is None
    current = _run(web_api.artifact_repository.get_current_artifact_version(
        workspace_id, artifact.id,
    ))
    assert current.content == "v2 (сохранена в другом месте)"


def test_edit_unknown_material_has_no_500(api) -> None:
    client, _, _, workspace_id = api

    response = client.put(
        "/api/materials/999999",
        json={"content": "Текст", "expected_version_id": 1},
    )

    assert response.status_code == 200
    assert "error" in response.json()


def test_edit_is_isolated_by_workspace(api) -> None:
    client, web_api, _, workspace_id = api
    other = _run(web_api.partner_repository.provision_partner(
        222334111, "Other Agency 3", "other-agency-materials-3",
        business_name="Other Agency 3", business_type="independent_agent",
        short_description="Другое рабочее пространство.",
        context={},
    ))
    foreign_artifact, foreign_version = _run(
        web_api.artifact_repository.create_artifact_with_initial_version(
            other.workspace.id, artifact_type="post", title="Чужой", content="Чужой текст.",
        )
    )

    response = client.put(
        f"/api/materials/{foreign_artifact.id}",
        json={"content": "Взлом", "expected_version_id": foreign_version.id},
    )

    assert response.status_code == 200
    assert "error" in response.json()
    current = _run(web_api.artifact_repository.get_current_artifact_version(
        other.workspace.id, foreign_artifact.id,
    ))
    assert current.content == "Чужой текст."


def test_edit_endpoint_never_returns_500_on_backend_error(api, monkeypatch) -> None:
    client, web_api, _, workspace_id = api
    artifact, version = _run(web_api.artifact_repository.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост", content="Текст.",
    ))

    async def broken_get(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(web_api.artifact_repository, "get_artifact", broken_get)

    response = client.put(
        f"/api/materials/{artifact.id}",
        json={"content": "Новый текст", "expected_version_id": version.id},
    )

    assert response.status_code == 200
    assert "error" in response.json()


# ── DELETE /api/materials/{id}: real deletion, workspace-isolated ───────────

def test_delete_removes_the_material(api) -> None:
    client, web_api, _, workspace_id = api
    artifact, _ = _run(web_api.artifact_repository.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Удаляемый", content="Текст.",
    ))

    response = client.delete(f"/api/materials/{artifact.id}")

    assert response.status_code == 200
    assert response.json() == {"deleted": True}
    assert _run(web_api.artifact_repository.get_artifact(
        workspace_id, artifact.id,
    )) is None


def test_delete_unknown_material_has_no_500(api) -> None:
    client, _, _, workspace_id = api

    response = client.delete("/api/materials/999999")

    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] is False
    assert "error" in body


def test_delete_is_isolated_by_workspace(api) -> None:
    """A workspace must never be able to delete another workspace's
    material, even by guessing its numeric id."""
    client, web_api, _, workspace_id = api
    other = _run(web_api.partner_repository.provision_partner(
        222334222, "Other Agency 4", "other-agency-materials-4",
        business_name="Other Agency 4", business_type="independent_agent",
        short_description="Другое рабочее пространство.",
        context={},
    ))
    foreign_artifact, _ = _run(web_api.artifact_repository.create_artifact_with_initial_version(
        other.workspace.id, artifact_type="post", title="Чужой", content="Чужой текст.",
    ))

    response = client.delete(f"/api/materials/{foreign_artifact.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] is False
    assert "error" in body
    assert _run(web_api.artifact_repository.get_artifact(
        other.workspace.id, foreign_artifact.id,
    )) is not None


def test_delete_endpoint_never_returns_500_on_backend_error(api, monkeypatch) -> None:
    client, web_api, _, workspace_id = api
    artifact, _ = _run(web_api.artifact_repository.create_artifact_with_initial_version(
        workspace_id, artifact_type="post", title="Пост", content="Текст.",
    ))

    async def broken_delete(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(web_api.artifact_repository, "delete_artifact", broken_delete)

    response = client.delete(f"/api/materials/{artifact.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] is False
    assert "error" in body
