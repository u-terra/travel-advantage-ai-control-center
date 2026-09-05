"""Help must document the signal/competitor -> material product chain (see
task notes): the user should never have to copy signal/competitor output
into the Assistant by hand - a button next to the signal or opportunity
creates the material directly, in the saved personal style, and the
disclaimer that freshness/provenance is preserved and unverified data is
never presented as a fact must be explicit.

Pure file-content checks - no web-only dependencies required.
"""

from __future__ import annotations

import re
from pathlib import Path

HELP_HTML = Path("app/templates/help.html").read_text(encoding="utf-8")
HELP_HTML_NORMALIZED = re.sub(r"\s+", " ", HELP_HTML)


def test_help_signals_section_explains_create_material_action() -> None:
    section = HELP_HTML_NORMALIZED.split('id="signals"', 1)[1].split('id="competitors"', 1)[0]
    assert "Подготовить пост" in section
    assert "Сообщение клиентам" in section
    assert "сохранённом стиле" in section


def test_help_competitors_section_explains_what_can_be_done_block() -> None:
    section = HELP_HTML_NORMALIZED.split('id="competitors"', 1)[1].split('id="materials"', 1)[0]
    assert "Что можно сделать" in section
    assert "Подготовить пост" in section
    assert "Сообщение клиентам" in section


def test_help_explains_freshness_and_facts_disclaimer_for_generated_materials() -> None:
    assert (
        "Свежесть источника сохраняется" in HELP_HTML_NORMALIZED
    )
    assert "не превращает неподтверждённые данные в факт" in HELP_HTML_NORMALIZED
