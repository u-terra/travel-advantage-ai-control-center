"""Partner Discovery v0, contact-discovery layer: within an already-found
``ProspectCandidate`` (app.domain.prospects), identify the one concrete,
publicly identifiable human contact to approach - or, failing that, fall
back to the project's own public channel.

A ``ContactCandidate`` is never a guess: it only restates what a public
page or public search result already states explicitly. See
app.services.contact_discovery's module docstring for the extraction
algorithm and its anti-hallucination rules.

v0 is stateless, same as ProspectCandidate: built fresh per request, never
written to a database. No phone/email/address/private-profile field exists
on this dataclass by design - see app.services.contact_discovery's
docstring for the privacy boundary this enforces.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ContactType(str, Enum):
    PERSON = "person"
    PROJECT = "project"


class ContactConfidence(str, Enum):
    """LOW is deliberately absent as a value ever returned to a caller -
    app.services.contact_discovery treats "low confidence" the same as
    "person not found" (falls back to ContactType.PROJECT) rather than
    exposing a LOW tier the UI would have to know not to trust."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"


class SearchStatus(str, Enum):
    """How the contact check ended. ``NOT_FOUND`` means every step ran to
    completion and nothing qualified; ``INCOMPLETE`` means a step could not
    run (page unreachable, LLM/search unavailable, unreadable model answer)
    - the absence of a person is then NOT a finding. Before this, both
    collapsed into the same 'person not found' fallback."""

    FOUND = "found"
    NOT_FOUND = "not_found"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class ContactChannel:
    """One way to reach someone that a public page publishes verbatim.

    ``owner`` says whose it is: ``"person"`` only when the page ties it to
    the named person (their name in the link text / right before the value),
    otherwise ``"project"`` (a general address of the project). A website or
    a community page is never a channel - see
    app.services.contact_discovery.extract_channels."""

    kind: str  # email | phone | telegram | whatsapp | form
    value: str
    source_url: str
    owner: str  # person | project


@dataclass(frozen=True)
class ContactCandidate:
    contact_type: ContactType
    display_name: str
    project_name: str
    public_url: str
    evidence_text: str
    evidence_url: str
    provenance: str
    role: str | None = None
    confidence: ContactConfidence | None = None
    # Additive fields (defaults keep every existing constructor valid).
    channels: tuple[ContactChannel, ...] = ()
    search_status: str = SearchStatus.FOUND.value
    incomplete_reasons: tuple[str, ...] = ()
