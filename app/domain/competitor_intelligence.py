from __future__ import annotations

from dataclasses import dataclass

# Where a CompetitorIntelligence/CompetitorSourceEvidence's underlying text
# actually came from - see app/services/competitor_intelligence.py. Kept as
# domain vocabulary (not service-private) because both the Telegram card
# (app/handlers/competitors.py) and the Web chat context
# (app/web_api.py._competitor_context) need to render a different, honest
# framing depending on which one it is - neither may ever present
# DATA_ORIGIN_RADAR_SIGNAL data as if it came from the competitor's own site.
DATA_ORIGIN_DIRECT_FETCH = "direct_fetch"
DATA_ORIGIN_RADAR_SIGNAL = "radar_signal"


@dataclass(frozen=True)
class CompetitorSourceEvidence:
    title: str
    url: str
    final_url: str
    discovered_at: str
    freshness: str | None
    summary: str
    key_facts: tuple[str, ...]
    # Default preserves positional/keyword construction in existing code and
    # tests (see tests/test_ta_affiliation_isolation.py's _fake_intelligence) -
    # every pre-existing call site meant a direct site fetch.
    origin: str = DATA_ORIGIN_DIRECT_FETCH


@dataclass(frozen=True)
class ContentOpportunity:
    id: str
    competitor_id: int
    topic: str
    source_title: str
    source_url: str
    freshness: str | None
    key_thesis: str
    audience_value: str
    own_post_angle: str
    travel_advantage_link: str | None


@dataclass(frozen=True)
class CompetitorIntelligence:
    competitor_id: int
    competitor_label: str
    analyzed_at: str
    positioning: tuple[str, ...]
    products: tuple[str, ...]
    destinations_and_categories: tuple[str, ...]
    promotions: tuple[str, ...]
    loyalty_mechanics: tuple[str, ...]
    service_and_ux: tuple[str, ...]
    strengths: tuple[str, ...]
    travel_advantage_comparison: tuple[str, ...]
    fresh_signals: tuple[str, ...]
    sources: tuple[CompetitorSourceEvidence, ...]
    opportunities: tuple[ContentOpportunity, ...]
    # Same default-for-backward-compat rule as CompetitorSourceEvidence.origin
    # above - summarizes the whole snapshot's provenance in one place so
    # callers don't have to inspect every source individually to decide how
    # to frame the result to the user.
    data_origin: str = DATA_ORIGIN_DIRECT_FETCH
