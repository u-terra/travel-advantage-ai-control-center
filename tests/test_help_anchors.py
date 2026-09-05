"""Every in-page TOC link in Help must resolve to a real anchor, and every
top-level section must be reachable from the TOC (see task notes for the
beta-scope Help rewrite - the page was restructured and anchors must stay
in sync with the sidebar).

Pure file-content checks - no web-only dependencies required.
"""

from __future__ import annotations

import re
from pathlib import Path

HELP_HTML = Path("app/templates/help.html").read_text(encoding="utf-8")

_TOC_HREF_PATTERN = re.compile(r'<a href="#([\w-]+)">')
_ANCHOR_ID_PATTERN = re.compile(r'\bid="([\w-]+)"')


def _toc_block() -> str:
    return HELP_HTML.split('<ul class="toc" id="toc">', 1)[1].split("</ul>", 1)[0]


def test_every_toc_link_has_a_matching_anchor_id() -> None:
    toc_targets = _TOC_HREF_PATTERN.findall(_toc_block())
    assert toc_targets, "expected the Help TOC to list at least one link"

    all_ids = set(_ANCHOR_ID_PATTERN.findall(HELP_HTML))
    missing = [target for target in toc_targets if target not in all_ids]
    assert not missing, f"TOC links with no matching id=... anchor: {missing}"


def test_toc_covers_the_expected_new_structure() -> None:
    toc_targets = _TOC_HREF_PATTERN.findall(_toc_block())
    assert toc_targets == [
        "about",
        "audience",
        "capabilities",
        "quickstart",
        "scenarios",
        "voice-style",
        "files",
        "signals",
        "materials",
        "web-telegram",
        "why",
        "billing",
        "faq",
        "troubleshooting",
    ]
