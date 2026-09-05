"""Help must never bake in a commercial price - the single source of truth
is the env-configured price read by app/config.py and app/services/
billing_service.py. Help should only point the user to the "Подписка" page,
never quote a number (see task notes for the beta-scope Help rewrite).

Pure file-content checks - no web-only dependencies required.
"""

from __future__ import annotations

import re
from pathlib import Path

HELP_HTML = Path("app/templates/help.html").read_text(encoding="utf-8")

_CURRENCY_PATTERN = re.compile(r"\d[\d\s]*(?:₽|руб\.?|rub)", re.IGNORECASE)


def test_help_page_never_hardcodes_a_currency_amount() -> None:
    match = _CURRENCY_PATTERN.search(HELP_HTML)
    assert match is None, (
        f"help.html appears to hardcode a price ({match.group()!r}); the "
        "price must only be read from Billing/config, never written into Help."
    )


def test_help_billing_section_links_to_the_subscription_page_instead() -> None:
    section = HELP_HTML.split('id="billing"', 1)[1].split('id="faq"', 1)[0]
    assert 'href="/billing"' in section
