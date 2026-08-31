from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CompetitorSourceEvidence:
    title: str
    url: str
    final_url: str
    discovered_at: str
    freshness: str | None
    summary: str
    key_facts: tuple[str, ...]


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
