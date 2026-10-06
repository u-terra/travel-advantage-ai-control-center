"""Partner Discovery v0: a public, travel-related subject (person or
project) surfaced from public web search, before any outreach is ever sent.

A ``ProspectCandidate`` is never a claim about the subject's intent - it is
only PUBLIC FIT: what is observably true about their public page/profile.
Nothing here encodes "wants a partnership", "looking for income", or
"interested in Travel Advantage" - that distinction is enforced in
app/services/partner_discovery.py's scoring/evidence text, not in this
module, but the dataclass shape itself deliberately has no field for it.

v0 is stateless (see app/services/partner_discovery.py's own docstring):
these are built fresh per request and never written to a database.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ProspectType(str, Enum):
    TRAVEL_AGENT = "travel_agent"
    TOUR_ORGANIZER = "tour_organizer"
    GUIDE = "guide"
    TRAVEL_CREATOR = "travel_creator"
    TRAVEL_CLUB = "travel_club"
    OTHER = "other_travel_project"


class ProspectFit(str, Enum):
    STRONG = "STRONG"
    POSSIBLE = "POSSIBLE"


class EvidenceSource(str, Enum):
    """Where ``ProspectCandidate.evidence`` actually came from - shown to
    the user verbatim (see the task's own card example: "Источник:
    публичная страница" vs "поисковая выдача"), never silently upgraded."""

    PAGE = "page"
    SNIPPET = "snippet"


@dataclass(frozen=True)
class ProspectCandidate:
    name: str
    url: str
    prospect_type: ProspectType
    query: str
    evidence: str
    evidence_source: EvidenceSource
    fit: ProspectFit
    # Deterministic ranking score (higher = stronger keyword match) - an
    # internal ordering aid, never shown to the user as-is.
    rank_score: int
