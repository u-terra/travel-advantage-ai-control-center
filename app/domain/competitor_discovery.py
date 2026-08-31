"""Competitor Discovery Radar: candidates found from public market signals,
before a workspace owner decides to promote one into ``competitors``.

A ``CompetitorCandidate`` is never itself a competitor - see
app/repositories/competitor_repository.py for the promotion step
(``add_competitor``), which is the same call already used by the manual
"➕ Добавить конкурента" flow.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit


class CandidateClassification(str, Enum):
    DIRECT_COMPETITOR = "direct_competitor"
    POTENTIAL_COMPETITOR = "potential_competitor"
    MARKET_SIGNAL = "market_signal"


class CandidateStatus(str, Enum):
    NEW = "new"
    REVIEWED = "reviewed"
    ADDED = "added"
    IGNORED = "ignored"


class CandidateConfidence(str, Enum):
    HIGH = "высокая"
    MEDIUM = "средняя"


@dataclass(frozen=True)
class CompetitorCandidate:
    candidate_id: int
    workspace_id: int
    name: str
    canonical_domain: str
    discovered_url: str
    source_title: str
    source_url: str
    discovered_at: str
    description: str
    evidence: tuple[str, ...]
    confidence: CandidateConfidence
    classification: CandidateClassification
    why_it_matters: str
    status: CandidateStatus


def canonical_domain(url: str) -> str:
    """Same root-host stripping already used by
    app/services/competitor_intelligence.py's ``_candidate_urls`` for a
    saved competitor's own URL - reused here (not reimplemented) so
    ``nl.trip.com``/``www.trip.com``/``trip.com/?locale=...`` all resolve to
    the one canonical ``trip.com``, whether the URL is an existing
    competitor's or a freshly discovered candidate's."""
    host = (urlsplit(url.strip()).hostname or "").lower()
    labels = host.split(".")
    if len(labels) >= 3 and (labels[0] == "www" or len(labels[0]) <= 3):
        return ".".join(labels[1:])
    return host
