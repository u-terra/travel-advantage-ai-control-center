from __future__ import annotations

from dataclasses import dataclass


SOURCE_TYPES = ("text", "url", "lead_radar", "note")
SOURCE_STATUSES = ("new", "analyzed", "archived")
ARTIFACT_TYPES = (
    "post",
    "video_script",
    "stories",
    "client_message",
    "objection_reply",
    "faq",
    "content_plan_item",
    "other",
)
ARTIFACT_STATUSES = ("draft", "review_required", "ready", "used", "archived")


@dataclass(frozen=True)
class Source:
    id: int
    workspace_id: int
    source_type: str
    original_url: str | None
    original_text: str | None
    title: str
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Artifact:
    id: int
    workspace_id: int
    source_id: int | None
    artifact_type: str
    title: str
    status: str
    current_version_id: int | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ArtifactVersion:
    id: int
    artifact_id: int
    version_number: int
    content: str
    generation_note: str | None
    created_at: str
