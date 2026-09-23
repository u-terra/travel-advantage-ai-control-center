"""RoboKassa billing HTTP surface (app.web_api): payment creation,
ResultURL callback, and the "billing works even when the subscription is
expired" acceptance criterion - see the task notes. Mock RoboKassa only -
no real network calls or real payments anywhere in this file.

Requires the web-only dependencies (requirements-web.txt: fastapi,
uvicorn, markdown, argon2-cffi). Skips cleanly when they're not installed.
"""

from __future__ import annotations

import asyncio

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("markdown")
pytest.importorskip("argon2")

from fastapi.testclient import TestClient  # noqa: E402

from tests._web_auth_test_helpers import login_as  # noqa: E402

OWNER_ID = 586249067
PASSWORD1 = "pw1-test-only"
PASSWORD2 = "pw2-test-only"


def _run(coro):
    return asyncio.run(coro)


def _configure_robokassa_env(monkeypatch) -> None:
    monkeypatch.setenv("ROBOKASSA_MERCHANT_LOGIN", "orchestravel-test")
    monkeypatch.setenv("ROBOKASSA_PASSWORD1", PASSWORD1)
    monkeypatch.setenv("ROBOKASSA_PASSWORD2", PASSWORD2)
    monkeypatch.setenv("ROBOKASSA_IS_TEST", "true")
    monkeypatch.setenv("ORCHESTRAVEL_STANDARD_PRICE_RUB", "999.00")
    monkeypatch.setenv("ORCHESTRAVEL_SUBSCRIPTION_DAYS", "30")


@pytest.fixture
def api(tmp_path, monkeypatch):
    db_path = tmp_path / "journal.sqlite3"
    monkeypatch.setenv("JOURNAL_DB_PATH", str(db_path))
    monkeypatch.setenv("PLANNER_OPENAI_API_KEY", "test-key")
    _configure_robokassa_env(monkeypatch)

    import sys
    sys.modules.pop("app.web_api", None)
    import app.web_api as web_api

    with TestClient(web_api.app, base_url="https://testserver") as client:
        ws, _ = _run(web_api.partner_repository.ensure_owner_workspace(OWNER_ID))
        login_as(client, web_api, ws.id, OWNER_ID)
        yield client, web_api, ws.id

    sys.modules.pop("app.web_api", None)


@pytest.fixture
def unconfigured_api(tmp_path, monkeypatch):
    """No ROBOKASSA_* env at all - billing must degrade gracefully, never
    crash the app."""
    db_path = tmp_path / "journal.sqlite3"
    monkeypatch.setenv("JOURNAL_DB_PATH", str(db_path))
    monkeypatch.setenv("PLANNER_OPENAI_API_KEY", "test-key")
    for name in (
        "ROBOKASSA_MERCHANT_LOGIN", "ROBOKASSA_PASSWORD1", "ROBOKASSA_PASSWORD2",
        "ROBOKASSA_IS_TEST", "ORCHESTRAVEL_STANDARD_PRICE_RUB",
        "ORCHESTRAVEL_SUBSCRIPTION_DAYS",
    ):
        monkeypatch.delenv(name, raising=False)

    import sys
    sys.modules.pop("app.web_api", None)
    import app.web_api as web_api

    with TestClient(web_api.app, base_url="https://testserver") as client:
        ws, _ = _run(web_api.partner_repository.ensure_owner_workspace(OWNER_ID))
        login_as(client, web_api, ws.id, OWNER_ID)
        yield client, web_api, ws.id

    sys.modules.pop("app.web_api", None)


def _signed_result_form(web_api, order) -> dict:
    from app.services.robokassa import build_result_signature
    signature = build_result_signature(
        out_sum=order.amount, inv_id=order.id, password2=PASSWORD2,
    )
    return {"OutSum": order.amount, "InvId": str(order.id), "SignatureValue": signature}


# ── /api/billing/status and /api/billing/create-payment work even when expired ──

def test_billing_status_reachable_when_subscription_expired(api) -> None:
    client, web_api, workspace_id = api
    _run(web_api.subscription_repository.mark_expired(workspace_id))

    response = client.get("/api/billing/status")

    assert response.status_code == 200
    body = response.json()
    assert body["access_granted"] is False
    assert body["billing_configured"] is True
    assert body["is_test"] is True
    assert body["standard_price_rub"] == "999.00"


def test_create_payment_reachable_when_subscription_expired(api) -> None:
    client, web_api, workspace_id = api
    _run(web_api.subscription_repository.mark_expired(workspace_id))

    response = client.post("/api/billing/create-payment")

    assert response.status_code == 200
    body = response.json()
    assert "error" not in body
    assert body["payment_url"].startswith("https://auth.robokassa.ru/")


def test_expired_user_still_blocked_from_paid_product_api(api) -> None:
    """Billing is exempt from the subscription gate; the actual product
    surface is not - this must still hold with billing wired in."""
    client, web_api, workspace_id = api
    _run(web_api.subscription_repository.mark_expired(workspace_id))

    response = client.get("/api/materials")

    assert response.status_code == 402


# ── create-payment: CSRF, workspace/amount only from server-side ───────

def test_create_payment_requires_csrf(api) -> None:
    client, _, _ = api
    client.headers.pop("X-CSRF-Token", None)

    response = client.post("/api/billing/create-payment")

    assert response.status_code == 403


def test_create_payment_ignores_a_client_supplied_workspace_id(api) -> None:
    """workspace_id always comes from the session (principal), never from
    the request body - posting one is simply ignored, not honored."""
    client, web_api, workspace_id = api

    response = client.post("/api/billing/create-payment", json={"workspace_id": 999999})

    assert response.status_code == 200
    order_id = response.json()["order_id"]
    order = _run(web_api.payment_order_repository.get_order(order_id))
    assert order.workspace_id == workspace_id
    assert order.workspace_id != 999999


def test_create_payment_ignores_a_client_supplied_amount(api) -> None:
    """amount always comes from the server-side plan catalog
    (app.services.plans.PLAN_CATALOG) - a client can pick WHICH plan
    (a small server-approved enum), never an arbitrary amount field."""
    client, web_api, _ = api

    response = client.post(
        "/api/billing/create-payment", json={"amount": "1.00", "plan": "standard"},
    )

    assert response.status_code == 200
    assert response.json()["amount"] == "990.00"


def test_create_payment_rejects_an_unknown_plan(api) -> None:
    client, _, _ = api

    response = client.post("/api/billing/create-payment", json={"plan": "made-up-plan"})

    assert response.status_code == 200
    assert "error" in response.json()


def test_create_payment_start_plan_prices_from_the_catalog(api) -> None:
    client, _, _ = api

    response = client.post("/api/billing/create-payment", json={"plan": "start"})

    assert response.status_code == 200
    body = response.json()
    assert body["amount"] == "490.00"
    assert body["plan"] == "start"
    assert body["duration_days"] == 14


def test_create_payment_response_never_contains_secrets(api) -> None:
    client, _, _ = api
    response = client.post("/api/billing/create-payment")
    body_text = response.text
    assert PASSWORD1 not in body_text
    assert PASSWORD2 not in body_text


def test_billing_status_response_never_contains_secrets(api) -> None:
    client, _, _ = api
    response = client.get("/api/billing/status")
    body_text = response.text
    assert PASSWORD1 not in body_text
    assert PASSWORD2 not in body_text


def test_billing_gracefully_reports_not_configured(unconfigured_api) -> None:
    client, _, _ = unconfigured_api

    status = client.get("/api/billing/status")
    assert status.status_code == 200
    assert status.json()["billing_configured"] is False

    create = client.post("/api/billing/create-payment")
    assert create.status_code == 200
    assert "error" in create.json()


# ── POST /api/billing/robokassa/result - the security-critical callback ──

def test_valid_result_callback_returns_ok_and_activates_subscription(api) -> None:
    client, web_api, workspace_id = api
    create = client.post("/api/billing/create-payment")
    order_id = create.json()["order_id"]
    order = _run(web_api.payment_order_repository.get_order(order_id))

    response = client.post(
        "/api/billing/robokassa/result", data=_signed_result_form(web_api, order),
    )

    assert response.status_code == 200
    assert response.text == f"OK{order_id}"

    status = client.get("/api/billing/status")
    assert status.json()["access_granted"] is True
    assert status.json()["status"] == "active"
    assert status.json()["plan"] == "standard"


def test_valid_result_callback_unblocks_the_product_api_web_side(api) -> None:
    client, web_api, workspace_id = api
    _run(web_api.subscription_repository.mark_expired(workspace_id))
    assert client.get("/api/materials").status_code == 402

    create = client.post("/api/billing/create-payment")
    order = _run(web_api.payment_order_repository.get_order(create.json()["order_id"]))
    client.post("/api/billing/robokassa/result", data=_signed_result_form(web_api, order))

    assert client.get("/api/materials").status_code == 200


def test_valid_result_callback_also_activates_the_telegram_side(api) -> None:
    """The main acceptance criterion: ONE payment -> both channels. Proven
    at the exact call Telegram's AccessStateMiddleware makes
    (SubscriptionRepository.resolve_access_state) - not a duplicated,
    Telegram-specific activation path."""
    from app.services.access_state import ACTIVE

    client, web_api, workspace_id = api
    create = client.post("/api/billing/create-payment")
    order = _run(web_api.payment_order_repository.get_order(create.json()["order_id"]))
    client.post("/api/billing/robokassa/result", data=_signed_result_form(web_api, order))

    telegram_side_state = _run(
        web_api.subscription_repository.resolve_access_state(workspace_id)
    )
    assert telegram_side_state == ACTIVE


def test_result_callback_rejects_invalid_signature(api) -> None:
    client, web_api, workspace_id = api
    create = client.post("/api/billing/create-payment")
    order = _run(web_api.payment_order_repository.get_order(create.json()["order_id"]))

    response = client.post("/api/billing/robokassa/result", data={
        "OutSum": order.amount, "InvId": str(order.id), "SignatureValue": "0" * 32,
    })

    assert response.status_code == 400
    assert not response.text.startswith("OK")
    # A fresh workspace is already 'beta'-granted by default (unrelated to
    # billing - see SubscriptionRepository.init()'s backfill), so the real
    # assertion is that the rejected payment never touched plan/order
    # status, not that access_granted flipped to False.
    status = client.get("/api/billing/status")
    assert status.json()["plan"] != "standard"
    order_after = _run(web_api.payment_order_repository.get_order(order.id))
    assert order_after.status.value == "created"


def test_result_callback_rejects_tampered_out_sum(api) -> None:
    from app.services.robokassa import build_result_signature

    client, web_api, workspace_id = api
    create = client.post("/api/billing/create-payment")
    order = _run(web_api.payment_order_repository.get_order(create.json()["order_id"]))
    fake_amount = "1.00"
    signature = build_result_signature(out_sum=fake_amount, inv_id=order.id, password2=PASSWORD2)

    response = client.post("/api/billing/robokassa/result", data={
        "OutSum": fake_amount, "InvId": str(order.id), "SignatureValue": signature,
    })

    assert response.status_code == 400
    status = client.get("/api/billing/status")
    assert status.json()["plan"] != "standard"
    order_after = _run(web_api.payment_order_repository.get_order(order.id))
    assert order_after.status.value == "created"


def test_result_callback_rejects_unknown_inv_id(api) -> None:
    from app.services.robokassa import build_result_signature

    client, _, _ = api
    signature = build_result_signature(out_sum="999.00", inv_id=999999, password2=PASSWORD2)

    response = client.post("/api/billing/robokassa/result", data={
        "OutSum": "999.00", "InvId": "999999", "SignatureValue": signature,
    })

    assert response.status_code == 400


def test_result_callback_does_not_require_a_web_session_or_csrf(api) -> None:
    """RoboKassa's server can't present a session cookie or a CSRF header -
    a callback with NO auth state at all must still be processed purely on
    its own signature."""
    client, web_api, workspace_id = api
    create = client.post("/api/billing/create-payment")
    order = _run(web_api.payment_order_repository.get_order(create.json()["order_id"]))

    fresh_client = TestClient(web_api.app, base_url="https://testserver")
    response = fresh_client.post(
        "/api/billing/robokassa/result", data=_signed_result_form(web_api, order),
    )

    assert response.status_code == 200
    assert response.text == f"OK{order.id}"


def test_webhook_activates_the_specific_plan_that_was_paid_for(api) -> None:
    """create-payment(plan='full') -> ResultURL must activate exactly
    'full' with its own 1490.00/30-day terms, not the legacy single
    'standard' tariff and not whatever RoboKassaConfig.subscription_days
    happens to be."""
    client, web_api, workspace_id = api
    create = client.post("/api/billing/create-payment", json={"plan": "full"})
    order = _run(web_api.payment_order_repository.get_order(create.json()["order_id"]))
    assert order.plan == "full"
    assert order.amount == "1490.00"
    assert order.duration_days == 30

    response = client.post(
        "/api/billing/robokassa/result", data=_signed_result_form(web_api, order),
    )
    assert response.status_code == 200

    status = client.get("/api/billing/status").json()
    assert status["plan"] == "full"
    assert status["access_granted"] is True


def test_repeated_result_callback_does_not_extend_twice(api) -> None:
    client, web_api, workspace_id = api
    create = client.post("/api/billing/create-payment")
    order = _run(web_api.payment_order_repository.get_order(create.json()["order_id"]))
    form = _signed_result_form(web_api, order)

    first = client.post("/api/billing/robokassa/result", data=form)
    first_paid_until = client.get("/api/billing/status").json()["paid_until"]

    second = client.post("/api/billing/robokassa/result", data=form)
    second_paid_until = client.get("/api/billing/status").json()["paid_until"]

    assert first.status_code == 200 and second.status_code == 200
    assert first_paid_until == second_paid_until


def test_result_callback_never_logs_secrets(api, caplog) -> None:
    """log.warning() on a rejected callback must only ever carry a neutral
    reason code and InvId (see ResultOutcome) - never the signature or
    either password."""
    client, web_api, workspace_id = api
    create = client.post("/api/billing/create-payment")
    order = _run(web_api.payment_order_repository.get_order(create.json()["order_id"]))

    with caplog.at_level("WARNING"):
        client.post("/api/billing/robokassa/result", data={
            "OutSum": order.amount, "InvId": str(order.id), "SignatureValue": "0" * 32,
        })

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert PASSWORD1 not in log_text
    assert PASSWORD2 not in log_text
    assert "0" * 32 not in log_text


def test_result_callback_response_never_contains_secrets(api) -> None:
    client, web_api, workspace_id = api
    create = client.post("/api/billing/create-payment")
    order = _run(web_api.payment_order_repository.get_order(create.json()["order_id"]))

    response = client.post(
        "/api/billing/robokassa/result", data=_signed_result_form(web_api, order),
    )
    assert PASSWORD1 not in response.text
    assert PASSWORD2 not in response.text


# ── /api/billing/orders/{id}: session-scoped, no cross-workspace leak ──

def test_get_order_rejects_an_order_belonging_to_another_workspace(api) -> None:
    client, web_api, workspace_id = api
    other = _run(web_api.partner_repository.provision_partner(
        999888777, "Other Agency", "other-agency-billing",
        business_name="Other Agency", business_type="independent_agent",
        short_description="Другое рабочее пространство.", context={},
    ))
    foreign_order = _run(web_api.payment_order_repository.create_order(
        workspace_id=other.workspace.id, plan="standard", amount="999.00",
    ))

    response = client.get(f"/api/billing/orders/{foreign_order.id}")

    assert response.status_code == 200
    assert response.json()["order"] is None
    assert "error" in response.json()


def test_get_order_returns_own_workspaces_order(api) -> None:
    client, web_api, workspace_id = api
    create = client.post("/api/billing/create-payment")
    order_id = create.json()["order_id"]

    response = client.get(f"/api/billing/orders/{order_id}")

    assert response.status_code == 200
    assert response.json()["order"]["id"] == order_id
    assert response.json()["order"]["status"] == "created"


# ── billing pages are reachable at any subscription state ──────────────

def test_billing_page_reachable_when_subscription_expired(api) -> None:
    client, web_api, workspace_id = api
    _run(web_api.subscription_repository.mark_expired(workspace_id))

    response = client.get("/billing")

    assert response.status_code == 200
    assert "subscription-inactive" not in response.headers.get("location", "")


def test_billing_success_and_fail_pages_reachable_when_expired(api) -> None:
    client, web_api, workspace_id = api
    _run(web_api.subscription_repository.mark_expired(workspace_id))

    assert client.get("/billing/success").status_code == 200
    assert client.get("/billing/fail").status_code == 200


def test_billing_page_redirects_anonymous_visitor_to_login(api) -> None:
    client, _, _ = api
    client.cookies.clear()

    response = client.get("/billing", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
