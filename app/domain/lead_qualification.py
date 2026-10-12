"""Partner Discovery 2.0: lead qualification shapes layered on top of the
existing stateless ``ProspectCandidate`` (app.domain.prospects).

Deliberately stateless, same as app.domain.prospects/contacts - these are
computed fresh per discovery run by app.services.lead_qualification and
never written to a database. Nothing here replaces ProspectCandidate; a
qualified lead is a ProspectCandidate (as a dict, same convention as
app.services.partner_outreach.candidate_to_dict) plus the fields below.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ScoreBreakdown:
    """Six components from the task's scoring model, each already clamped
    to its own max by the caller before this is built. ``total`` is the sum,
    capped at 100 defensively (components already can't exceed it)."""

    professional_relevance: int  # 0-25
    activity: int  # 0-15
    commercial_potential: int  # 0-20
    audience_quality: int  # 0-15
    contactability: int  # 0-10
    personal_hook_score: int  # 0-15

    @property
    def total(self) -> int:
        return min(
            100,
            self.professional_relevance
            + self.activity
            + self.commercial_potential
            + self.audience_quality
            + self.contactability
            + self.personal_hook_score,
        )


@dataclass(frozen=True)
class EvidenceClaim:
    """One fact about a lead, tied to the specific public source it came
    from. A claim with no source_url is never constructed by
    app.services.lead_qualification - see its module docstring."""

    claim: str
    source_url: str
    source_title: str = ""


@dataclass(frozen=True)
class QualifiedLead:
    category: str
    score: ScoreBreakdown
    confidence: float  # 0.0-1.0
    why_fit: tuple[str, ...]
    summary: str
    hook: str
    risk_flags: tuple[str, ...] = field(default_factory=tuple)
    evidence: tuple[EvidenceClaim, ...] = field(default_factory=tuple)

    @property
    def total_score(self) -> int:
        return self.score.total
