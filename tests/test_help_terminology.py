"""Terminology guard: ORCHESTRAVEL is not for independent travelers - its
audience is Travel Advantage partners, travel agents, travel agencies and
independent tour guides. User-facing copy must never call a
non-TA-affiliated user a "самостоятельный путешественник" ("independent
traveler") or bare "путешественник" ("traveler") - that phrasing appeared
in app/templates/help.html and has been replaced with "партнёр Travel
Advantage, турагент, турагентство или экскурсовод" (see the task notes for
this fix).

This only checks user-facing templates. The internal `independent` /
`ta_affiliated=False` vocabulary in Python/DB/tests (e.g. the
`independent_agent` business_type enum value) is unaffected and out of
scope here - it is never rendered as the bare English word "independent"
in user-facing copy, only as a Russian label.

Requires no web-only dependencies (pure file-content checks).
"""

from __future__ import annotations

from pathlib import Path

import pytest

TEMPLATES_DIR = Path("app/templates")
FORBIDDEN_PHRASES = [
    "самостоятельный путешественник",
    "самостоятельного путешественника",
    "путешественник",
]


def _template_files() -> list[Path]:
    return sorted(TEMPLATES_DIR.glob("*.html"))


@pytest.mark.parametrize("template_path", _template_files(), ids=lambda p: p.name)
def test_template_never_calls_a_user_an_independent_traveler(template_path: Path) -> None:
    html = template_path.read_text(encoding="utf-8")
    for phrase in FORBIDDEN_PHRASES:
        assert phrase not in html, (
            f"{template_path} still contains the forbidden phrase {phrase!r} - "
            "ORCHESTRAVEL's audience is Travel Advantage partners, travel agents "
            "and travel agencies, never 'independent travelers'."
        )


def test_help_profile_section_uses_the_correct_public_terminology() -> None:
    html = Path("app/templates/help.html").read_text(encoding="utf-8")
    assert "Партнёр Travel Advantage" in html or "партнёр Travel Advantage" in html
    assert "турагентство" in html
    assert "турагент" in html
    assert "экскурсовод" in html

