"""Help must explain why ORCHESTRAVEL is worth using over a pile of
separate tools (see task notes for the beta-scope Help rewrite): a "why"
section listing the unified capabilities, an explicit "does not replace a
CRM / booking system" disclaimer, the "no manual copy-pasting between
AI services" pitch, and a fair (not exaggerated) comparison against a
plain AI chat.

Pure file-content checks - no web-only dependencies required.
"""

from __future__ import annotations

import re
from pathlib import Path

HELP_HTML = Path("app/templates/help.html").read_text(encoding="utf-8")
HELP_HTML_NORMALIZED = re.sub(r"\s+", " ", HELP_HTML)


def test_help_has_a_why_section_anchor_in_the_toc() -> None:
    assert '<a href="#why">' in HELP_HTML
    assert 'id="why"' in HELP_HTML


def test_help_why_section_lists_the_unified_capabilities() -> None:
    section = HELP_HTML_NORMALIZED.split('id="why"', 1)[1].split('id="billing"', 1)[0]
    for capability in (
        "AI-ассистент",
        "документами",
        "персональный стиль",
        "сигналы рынка",
        "анализ конкурентов",
        "Web и Telegram",
    ):
        assert capability in section


def test_help_why_section_says_it_does_not_replace_crm_or_booking_system() -> None:
    assert (
        "ORCHESTRAVEL не пытается заменить CRM или систему бронирования. Он "
        "является интеллектуальным рабочим слоем поверх работы профессионала "
        "туризма."
    ) in HELP_HTML_NORMALIZED


def test_help_why_section_pitches_no_manual_copy_between_ai_services() -> None:
    assert (
        "Не нужно переносить информацию между несколькими AI-сервисами "
        "вручную."
    ) in HELP_HTML_NORMALIZED


def test_help_why_section_compares_fairly_against_a_plain_ai_chat() -> None:
    section = HELP_HTML_NORMALIZED.split('id="why"', 1)[1].split('id="billing"', 1)[0]
    assert "обычный AI-чат" in section
    assert "профиль бизнеса" in section
    assert "сигналами и конкурентами" in section
    # Must not overreach into claiming other AI services lack memory/files/
    # personalization outright - only compare via ORCHESTRAVEL's workflow.
    assert "не умеет" not in section
    assert "не поддерживает" not in section
