from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Competitor:
    id: int
    workspace_id: int
    url: str
    label: str
    created_at: str
