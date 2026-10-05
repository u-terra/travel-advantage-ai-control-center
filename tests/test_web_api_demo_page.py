"""GET /demo - public marketing/demo page, reachable with no session.

Must never require auth, must never touch tenant/admin data, and must
not accidentally expose admin-only navigation. Also covers the launch-fix
follow-ups: the Telegram link built from settings.orchestravel_bot_username
instead of a hardcoded handle, and the static QR asset (see
tools/gen_qr.py) pointing at the production /demo URL.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("markdown")
pytest.importorskip("argon2")

from fastapi.testclient import TestClient  # noqa: E402

import app.web_api as web_api  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import gen_qr  # noqa: E402


def test_demo_page_is_public_and_renders():
    with TestClient(web_api.app, base_url="https://testserver") as client:
        response = client.get("/demo")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert "ORCHESTRAVEL" in body
    assert "Следит за рынком и конкурентами" in body
    assert "/admin" not in body
    assert '"/login"' not in body
    assert "ORCHESTRAVEL связывает эти возможности в один рабочий процесс" in body
    assert "Полноценная работа с компьютера" in body
    assert "адаптацией ORCHESTRAVEL под ваш бизнес" in body
    assert 'href="https://t.me/VladCRM"' in body
    assert 'href="/signup"' in body


def test_demo_page_has_no_session_cookie_set():
    with TestClient(web_api.app, base_url="https://testserver") as client:
        response = client.get("/demo")

    assert "set-cookie" not in {k.lower() for k in response.headers.keys()}


def test_demo_page_telegram_link_is_built_from_settings_not_hardcoded(monkeypatch):
    """No literal bot handle survives templating; the page reflects
    whatever settings.orchestravel_bot_username currently is."""
    import dataclasses

    patched = dataclasses.replace(
        web_api.settings, orchestravel_bot_username="some_other_test_bot"
    )
    monkeypatch.setattr(web_api, "settings", patched)

    with TestClient(web_api.app, base_url="https://testserver") as client:
        response = client.get("/demo")

    body = response.text
    assert "{{BOT_USERNAME}}" not in body
    assert "t.me/some_other_test_bot" in body
    assert "ta_control_center_vassian_bot" not in body


def test_demo_page_telegram_link_uses_configured_default_bot():
    with TestClient(web_api.app, base_url="https://testserver") as client:
        response = client.get("/demo")

    body = response.text
    expected = f"t.me/{web_api.settings.orchestravel_bot_username}"
    assert expected in body
    assert body.count(expected) >= 2  # hero CTA + footer


def test_demo_page_has_no_llm_or_api_calls():
    with TestClient(web_api.app, base_url="https://testserver") as client:
        response = client.get("/demo")

    body = response.text
    assert "<script" not in body.lower()
    assert "/api/" not in body
    assert "fetch(" not in body


def test_demo_page_references_qr_asset():
    with TestClient(web_api.app, base_url="https://testserver") as client:
        response = client.get("/demo")

    body = response.text
    assert 'src="/static/demo-qr.svg"' in body
    assert "Откройте ORCHESTRAVEL на телефоне" in body


def test_demo_qr_asset_is_served_as_svg():
    with TestClient(web_api.app, base_url="https://testserver") as client:
        response = client.get("/static/demo-qr.svg")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"
    assert response.text.lstrip().startswith("<svg")


def test_demo_qr_asset_encodes_the_production_demo_url():
    """Regression: the committed SVG must decode to exactly
    {public_base_url}/demo, and must be regenerated whenever
    ORCHESTRAVEL_PUBLIC_BASE_URL's default changes."""
    demo_url = f"{web_api.settings.orchestravel_public_base_url}/demo"
    expected_svg = gen_qr.make_qr_svg(demo_url)
    asset_path = Path(__file__).resolve().parents[1] / "app" / "static" / "demo-qr.svg"
    actual_svg = asset_path.read_text(encoding="utf-8")

    assert actual_svg == expected_svg
    assert demo_url == "https://app.orchestravel.ru/demo"
