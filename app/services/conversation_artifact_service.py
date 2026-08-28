"""F2B: minimal foundation for future artifact-revision operations.

A future reference resolver ("верни предыдущий", "сделай живее") will need
to: read the current artifact for a (workspace, user), read the version
before it, and append a new revision without rewriting history. None of
that exists as a convenient API today - this ships the three operations
ahead of any producer, the same "infrastructure ahead of producers"
precedent as ActionContract in F1. No handler wires this yet.

ArtifactRepository remains the single source of truth for artifacts and
their versions. ConversationStateRepository only ever supplies
current_artifact_id as an input here - this service never caches or
duplicates version data, and is not a second versioning system.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.content import Artifact, ArtifactVersion
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.conversation_state_repository import ConversationStateRepository


@dataclass(frozen=True)
class CurrentArtifact:
    artifact: Artifact
    version: ArtifactVersion


class ConversationArtifactService:
    def __init__(
        self,
        artifact_repository: ArtifactRepository,
        conversation_state_repository: ConversationStateRepository,
    ) -> None:
        self._artifacts = artifact_repository
        self._conversation_state = conversation_state_repository

    async def get_current(
        self, workspace_id: int, telegram_user_id: int,
    ) -> CurrentArtifact | None:
        """Resolves conversation_state.current_artifact_id (if any) to the
        actual Artifact + its current ArtifactVersion, read straight from
        ArtifactRepository - the only source of truth for both."""
        state = await self._conversation_state.get_state(workspace_id, telegram_user_id)
        if state is None or state.current_artifact_id is None:
            return None
        artifact = await self._artifacts.get_artifact(workspace_id, state.current_artifact_id)
        if artifact is None:
            return None
        version = await self._artifacts.get_current_artifact_version(workspace_id, artifact.id)
        if version is None:
            return None
        return CurrentArtifact(artifact=artifact, version=version)

    async def get_previous_version(
        self, workspace_id: int, artifact_id: int,
    ) -> ArtifactVersion | None:
        """The version immediately before the artifact's current one.

        None if the artifact does not exist, has no current version, or the
        current version is already the first one (version_number == 1).
        """
        current = await self._artifacts.get_current_artifact_version(workspace_id, artifact_id)
        if current is None or current.version_number <= 1:
            return None
        return await self._artifacts.get_artifact_version(
            workspace_id, artifact_id, current.version_number - 1,
        )

    async def add_revision_if_current(
        self,
        workspace_id: int,
        artifact_id: int,
        expected_current_version_id: int,
        content: str,
        generation_note: str | None = None,
    ) -> ArtifactVersion | None:
        """Appends a new version, CAS-guarded by expected_current_version_id.

        History is never rewritten: a future "revert to previous" operation
        must call this with the *previous* version's content to create a new
        version, never edit an old row in place. Thin pass-through to
        ArtifactRepository, kept here only so future producers share one
        name/call-shape instead of re-deriving the CAS pattern per call site.
        """
        return await self._artifacts.add_artifact_version_if_current(
            workspace_id, artifact_id, expected_current_version_id, content,
            generation_note=generation_note,
        )
