"""Help must document "Мой стиль / Голос бренда" (see task notes): a plain-
language explanation of the feature plus the explicit disclaimer that a
pasted writing sample is used for MANNER only, never as a source of facts.

Pure file-content checks - no web-only dependencies required.
"""

from __future__ import annotations

import re
from pathlib import Path

HELP_HTML = Path("app/templates/help.html").read_text(encoding="utf-8")
HELP_HTML_NORMALIZED = re.sub(r"\s+", " ", HELP_HTML)


def test_help_has_a_voice_style_section_anchor_in_the_toc() -> None:
    assert '<a href="#voice-style">' in HELP_HTML
    assert 'id="voice-style"' in HELP_HTML


def test_help_voice_style_section_names_the_feature() -> None:
    assert "Мой стиль / Голос бренда" in HELP_HTML


def test_help_voice_style_section_has_the_facts_disclaimer() -> None:
    assert (
        "Пример используется для понимания манеры речи. Старые цены, даты, "
        "акции, отели и другие факты из примера не считаются актуальными."
    ) in HELP_HTML_NORMALIZED


def test_help_voice_style_section_gives_paste_examples() -> None:
    section = HELP_HTML.split('id="voice-style"', 1)[1]
    section = section.split('id="billing"', 1)[0]
    assert "example-card" in section
    assert section.count("example-card") >= 2
