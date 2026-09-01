"""Workspace-level long-term memory for the web Orchestrator.

Separate from conversation_state (per-session dialogue), BusinessProfile
(structured business facts) and personal_style (per-user tone/preferences).
This is a short, manually-curated working summary of the project the
workspace is running, scoped to the whole workspace rather than one
conversation or one user.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkspaceMemoryRecord:
    workspace_id: int
    summary: str
    created_at: str
    updated_at: str
