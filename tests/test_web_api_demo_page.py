"""GET /demo - public marketing/demo page, reachable with no session.

Must never require auth, must never touch tenant/admin data, and must
not accidentally expose admin-only navigation.
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("markdown")
pytest.importorskip("argon2")

from fastapi.testclient import TestClient  # noqa: E402

import app.web_api as web_api  # noqa: E402


def test_demo_page_is_public_and_renders():
    with TestClient(web_api.app, base_url="https://testserver") as client:
        response = client.get("/demo")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert "ORCHESTRAVEL" in body
    assert "Radar" in body
    assert "/admin" not in body
    assert '"/login"' not in body
    assert "Оркестратор связывает эти блоки" in body
    assert "Полноценная работа с компьютера" in body
    assert "адаптацией ORCHESTRAVEL под ваш бизнес" in body
    assert 'href="https://t.me/VladCRM"' in body
    assert 'href="/signup"' in body


def test_demo_page_has_no_session_cookie_set():
    with TestClient(web_api.app, base_url="https://testserver") as client:
        response = client.get("/demo")

    assert "set-cookie" not in {k.lower() for k in response.headers.keys()}
