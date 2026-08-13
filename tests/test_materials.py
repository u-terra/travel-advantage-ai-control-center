from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

from app.domain.content import Artifact, ArtifactVersion
from app.domain.partners import WorkspaceContext
from app.handlers.materials import _EMPTY, _NOT_FOUND, _UNAVAILABLE, open_material, show_materials
from app.keyboards import ARTIFACT_CHECK_PREFIX


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _Message:
    def __init__(self) -> None:
        self.answers: list[tuple[str, Any]] = []

    async def answer(self, text: str, reply_markup: Any = None, **kwargs: Any) -> None:
        self.answers.append((text, reply_markup))


class _Callback:
    def __init__(self, artifact_id: Any) -> None:
        self.data = f"artifact_open:{artifact_id}"
        self.message = _Message()
        self.answers: list[tuple[Any, dict]] = []

    async def answer(self, text: Any = None, **kwargs: Any) -> None:
        self.answers.append((text, kwargs))


def _context(workspace_id: int = 42) -> WorkspaceContext:
    return WorkspaceContext(100, workspace_id, "member", "active")


def _artifact(
    artifact_id: int = 7, workspace_id: int = 42, *,
    artifact_type: str = "post", title: str = "Пост про Италию",
    current_version_id: int = 99, created_at: str = "2026-08-01T10:00:00+00:00",
) -> Artifact:
    return Artifact(
        artifact_id, workspace_id, None, artifact_type, title, "draft",
        current_version_id, created_at, created_at,
    )


def _version(
    artifact_id: int = 7, version_id: int = 99, content: str = "Текст черновика."
) -> ArtifactVersion:
    return ArtifactVersion(version_id, artifact_id, 1, content, None, "2026-08-01T10:00:00+00:00")


def _repository(
    *, artifacts: list[Artifact] | None = None,
    artifact: Artifact | None = None, version: ArtifactVersion | None = None,
) -> Any:
    return AsyncMock(
        list_artifacts=AsyncMock(return_value=artifacts or []),
        get_artifact=AsyncMock(return_value=artifact),
        get_current_artifact_version=AsyncMock(return_value=version),
    )


def test_materials_list_is_scoped_to_caller_workspace() -> None:
    message = _Message()
    artifacts = [_artifact(1, 42, title="Пост A"), _artifact(2, 42, title="Пост B")]
    repository = _repository(artifacts=artifacts)

    _run(show_materials(message, _context(42), repository))

    repository.list_artifacts.assert_awaited_once_with(42, limit=10)
    text, markup = message.answers[0]
    button_texts = [button.text for row in markup.inline_keyboard for button in row]
    assert any("Пост A" in t for t in button_texts)
    assert any("Пост B" in t for t in button_texts)


def test_materials_list_empty_shows_friendly_message() -> None:
    message = _Message()
    repository = _repository(artifacts=[])

    _run(show_materials(message, _context(42), repository))

    assert message.answers[0][0] == _EMPTY


def test_materials_list_unavailable_without_workspace_context() -> None:
    message = _Message()
    repository = _repository(artifacts=[_artifact()])

    _run(show_materials(message, None, repository))

    assert message.answers[0][0] == _UNAVAILABLE
    repository.list_artifacts.assert_not_awaited()


def test_open_material_shows_current_version_and_review_action() -> None:
    callback = _Callback(7)
    artifact = _artifact(7, 42, title="Пост про Италию")
    version = _version(7, 99, content="Итоговый текст поста.")
    repository = _repository(artifact=artifact, version=version)

    _run(open_material(callback, _context(42), repository))

    repository.get_artifact.assert_awaited_once_with(42, 7)
    repository.get_current_artifact_version.assert_awaited_once_with(42, 7)
    text, markup = callback.message.answers[-1]
    assert "Итоговый текст поста." in text
    assert "Пост про Италию" in text
    buttons = [button for row in markup.inline_keyboard for button in row]
    assert any(
        button.callback_data == f"{ARTIFACT_CHECK_PREFIX}7" for button in buttons
    )


def test_open_material_fails_closed_for_foreign_workspace() -> None:
    """Запрос artifact_id из чужого workspace: get_artifact вернёт None (WHERE workspace_id=?)."""
    callback = _Callback(999)
    repository = _repository(artifact=None, version=_version())

    _run(open_material(callback, _context(42), repository))

    repository.get_artifact.assert_awaited_once_with(42, 999)
    repository.get_current_artifact_version.assert_not_awaited()
    assert callback.answers[0] == (_NOT_FOUND, {"show_alert": True})
    assert callback.message.answers == []


def test_open_material_fails_closed_without_workspace_context() -> None:
    callback = _Callback(7)
    repository = _repository(artifact=_artifact(), version=_version())

    _run(open_material(callback, None, repository))

    repository.get_artifact.assert_not_awaited()
    assert callback.answers[0] == (_NOT_FOUND, {"show_alert": True})


def test_open_material_rejects_malformed_callback_data() -> None:
    callback = _Callback("not-a-number")
    repository = _repository(artifact=_artifact(), version=_version())

    _run(open_material(callback, _context(42), repository))

    repository.get_artifact.assert_not_awaited()
    assert callback.answers[0] == (_NOT_FOUND, {"show_alert": True})
