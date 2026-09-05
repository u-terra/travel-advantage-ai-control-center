"""Help must present Web and Telegram as one workspace, one subscription -
never as two separate products (see task notes for the beta-scope Help
rewrite).

Pure file-content checks - no web-only dependencies required.
"""

from __future__ import annotations

import re
from pathlib import Path

HELP_HTML = Path("app/templates/help.html").read_text(encoding="utf-8")
HELP_HTML_NORMALIZED = re.sub(r"\s+", " ", HELP_HTML)


def test_help_has_a_web_telegram_section_anchor_in_the_toc() -> None:
    assert '<a href="#web-telegram">' in HELP_HTML
    assert 'id="web-telegram"' in HELP_HTML


def test_help_web_telegram_section_says_it_is_one_workspace() -> None:
    section = HELP_HTML_NORMALIZED.split('id="web-telegram"', 1)[1].split('id="why"', 1)[0]
    assert "не две подписки" in section
    assert "не два продукта" in section
    assert "Один workspace. Один профиль. Один сохранённый стиль. Одна подписка." in section


def test_help_web_telegram_section_explains_the_roles() -> None:
    section = HELP_HTML_NORMALIZED.split('id="web-telegram"', 1)[1].split('id="why"', 1)[0]
    assert "основной рабочий кабинет" in section
    assert "дополнительный" in section
