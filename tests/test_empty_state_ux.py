"""First-use / empty-state UX fix for the web cabinet (app/templates/chat.html)
and two onboarding UX defects (app/templates/onboarding.html).

No backend logic changed in this task - chat.html is served byte-identical
to every workspace regardless of ta_affiliated (see
tests/test_ta_affiliation_isolation.py for that gating), so these are
static-markup/behaviour checks on the shipped file itself: every empty
section gets a useful, non-technical explanation and (where a real,
already-existing mechanism exists) a CTA that reuses it - no second,
parallel mechanism, no fake data, no TA/MWR wording for the pieces this
touches.

Requires no web-only dependencies (pure file-content checks), but keeps
the same import-skip convention as the rest of the web test suite for
consistency.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

CHAT_HTML = Path("app/templates/chat.html").read_text(encoding="utf-8")
ONBOARDING_HTML = Path("app/templates/onboarding.html").read_text(encoding="utf-8")

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("markdown")
pytest.importorskip("argon2")

from fastapi.testclient import TestClient  # noqa: E402

from tests._web_auth_test_helpers import login_as  # noqa: E402

TA_OWNER_ID = 586249067
INDEPENDENT_ID = 700000004


def _run(coro):
    return asyncio.run(coro)


# ── Competitors: web-native add flow, no Telegram dependency ───────────────

def test_competitors_empty_state_has_a_working_add_cta() -> None:
    assert "Конкуренты ещё не добавлены" in CHAT_HTML
    assert "Telegram-боте" not in CHAT_HTML
    block = CHAT_HTML.split("function renderCompetitorsEmpty", 1)[1][:900]
    assert "renderAddCompetitorForm" in block


def test_competitors_add_form_posts_to_the_real_endpoint() -> None:
    """The compact add-competitor form must call the actual web endpoint,
    not just switch to Telegram instructions or a fake success state."""
    import re

    assert re.search(r'@app\.post\("/api/competitors"\)', "".join(
        Path("app/web_api.py").read_text(encoding="utf-8"),
    ))
    block = CHAT_HTML.split("function renderAddCompetitorForm", 1)[1][:2200]
    assert '"/api/competitors"' in block
    assert '"POST"' in block


# ── Signals: CTA reuses the existing "Ассистент" quick-action prompt ───────

def test_signals_empty_state_has_a_useful_cta() -> None:
    assert "Пока нет свежих сигналов" in CHAT_HTML
    assert "Спросить Ассистента об идеях" in CHAT_HTML


def test_signals_empty_cta_reuses_the_existing_quick_action_prompt() -> None:
    """No second mechanism: the CTA reads its prompt straight off the
    welcome screen's own #qaSignals quick-action card via goToAssistantWithPrompt()."""
    handler = CHAT_HTML.split("function renderSignalsEmpty", 1)[1][:800]
    assert "qaSignals" in handler
    assert "goToAssistantWithPrompt" in handler


# ── Knowledge: neutral, no fake upload mechanism, no TA/MWR wording ────────

def test_knowledge_empty_state_is_neutral_and_workspace_scoped() -> None:
    assert "Ваша база знаний пока пуста" in CHAT_HTML


def test_knowledge_empty_state_never_mentions_ta_or_invents_upload() -> None:
    block = CHAT_HTML.split('"Ваша база знаний пока пуста"', 1)[1][:220]
    assert "Travel Advantage" not in block
    assert "MWR" not in block
    assert "загруз" not in block.lower()  # no invented file-upload flow


# ── Materials: CTA reuses the existing Assistant material-creation prompt ──

def test_materials_empty_state_has_a_cta_pointing_to_the_assistant_flow() -> None:
    assert "Вы ещё не создавали материалы" in CHAT_HTML
    assert "Создать материал" in CHAT_HTML


def test_materials_empty_cta_reuses_the_existing_quick_action_prompt() -> None:
    block = CHAT_HTML.split('"Вы ещё не создавали материалы"', 1)[1][:600]
    assert "qaMaterial" in block
    assert "goToAssistantWithPrompt" in block


# ── History: CTA reuses the existing "Новый диалог" mechanism ──────────────

def test_history_empty_state_has_a_new_conversation_cta() -> None:
    assert "Здесь появятся ваши диалоги" in CHAT_HTML
    assert "Начать новый диалог" in CHAT_HTML


def test_history_empty_cta_reuses_start_new_conversation() -> None:
    block = CHAT_HTML.split('"Здесь появятся ваши диалоги"', 1)[1][:300]
    assert "startNewConversation" in block


# ── renderStateEmpty stays backward compatible (cta is optional) ──────────

def test_render_state_empty_cta_parameter_is_optional() -> None:
    signature = CHAT_HTML.split("function renderStateEmpty(", 1)[1].split(")", 1)[0]
    assert "cta" in signature
    body = CHAT_HTML.split("function renderStateEmpty(", 1)[1].split("\n}", 1)[0]
    assert "if (cta)" in body


# ── onboarding UX defect 1: no TA default for a non-affiliated workspace ──

def test_onboarding_never_defaults_to_ta_partner_when_not_affiliated() -> None:
    block = ONBOARDING_HTML.split("BUSINESS_TYPE_TO_WHO[business.business_type]", 1)[1][:600]
    assert "ta_affiliated" in block
    assert "independent_agent" in block


def test_onboarding_who_step_has_no_hardcoded_default_selection() -> None:
    """state.who starts unselected - only loadCurrentProfile() (fail-closed
    per the check above) can pre-select an option."""
    initial_state_block = ONBOARDING_HTML.split("const state = {", 1)[1].split("};", 1)[0]
    assert 'who: ""' in initial_state_block


# ── onboarding UX defect 2: natural phrasing for the style step ───────────

def test_onboarding_style_step_uses_the_natural_phrasing() -> None:
    assert "Как Ассистенту общаться с вами" in ONBOARDING_HTML
    assert "Как общаться Ассистенту" not in ONBOARDING_HTML


# ── the actual trigger condition: a fresh workspace really is empty ────────
#
# The bug report's "почти пустой продукт" is a real, expected state for a
# brand-new workspace (no fake data gets created to avoid it - see the
# task). These confirm the empty-state UI paths above are genuinely
# reachable for both tenant types, not just theoretically defined.

@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("JOURNAL_DB_PATH", str(tmp_path / "journal.sqlite3"))
    monkeypatch.setenv("PLANNER_OPENAI_API_KEY", "test-key")

    import sys
    sys.modules.pop("app.web_api", None)
    import app.web_api as web_api

    with TestClient(web_api.app, base_url="https://testserver") as client:
        ta_ws, _ = _run(web_api.partner_repository.ensure_owner_workspace(TA_OWNER_ID))
        login_as(client, web_api, ta_ws.id, TA_OWNER_ID)
        yield client, web_api

    sys.modules.pop("app.web_api", None)


def test_fresh_ta_workspace_starts_empty_across_the_cabinet(api) -> None:
    client, _ = api

    assert client.get("/api/competitors").json()["competitors"] == []
    assert client.get("/api/materials").json()["materials"] == []
    assert client.get("/api/conversations").json()["conversations"] == []
    assert client.get("/api/signals").json()["signals"] == []


def test_fresh_independent_workspace_starts_empty_across_the_cabinet(api) -> None:
    client, web_api = api
    provisioned = _run(web_api.partner_repository.provision_partner(
        INDEPENDENT_ID, "Independent Agent", "independent-empty-state-test",
        business_name="Мария Турагент", business_type="independent_agent",
        short_description="", context={},
    ))

    with TestClient(web_api.app, base_url="https://testserver") as independent_client:
        login_as(
            independent_client, web_api, provisioned.workspace.id, INDEPENDENT_ID,
            email="independent-empty@example.com",
        )

        assert independent_client.get("/api/competitors").json()["competitors"] == []
        assert independent_client.get("/api/materials").json()["materials"] == []
        assert independent_client.get("/api/conversations").json()["conversations"] == []
        assert independent_client.get("/api/signals").json()["signals"] == []
        assert independent_client.get("/api/knowledge").json() == {"sources": [], "items": []}
