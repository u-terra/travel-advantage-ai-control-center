"""Public self-service signup - POST /api/auth/signup (see app.web_api) -
end to end through the FastAPI app: new tenant creation, pending (unpaid)
access, reaching billing, and coexistence with the pre-existing
invite-only registration flow.

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
STRONG_PASSWORD = "correcthorsebattery-signup"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def api(tmp_path, monkeypatch):
    db_path = tmp_path / "journal.sqlite3"
    monkeypatch.setenv("JOURNAL_DB_PATH", str(db_path))
    monkeypatch.setenv("PLANNER_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("ROBOKASSA_MERCHANT_LOGIN", "orchestravel-test")
    monkeypatch.setenv("ROBOKASSA_PASSWORD1", "pw1-test-only")
    monkeypatch.setenv("ROBOKASSA_PASSWORD2", "pw2-test-only")
    monkeypatch.setenv("ROBOKASSA_IS_TEST", "true")

    import sys
    sys.modules.pop("app.web_api", None)
    import app.web_api as web_api

    # signup_rate_limiter is a module-level singleton (app.services.rate_limit) -
    # re-importing app.web_api does NOT reset it, since app.services.rate_limit
    # itself stays cached in sys.modules. Clear its state per test so one
    # test's signup attempts never count against another's rate limit.
    web_api.signup_rate_limiter._events.clear()

    with TestClient(web_api.app, base_url="https://testserver") as client:
        yield client, web_api

    sys.modules.pop("app.web_api", None)


def _signup(client, *, name="Иван Иванов", email="ivan@example.com",
            password=STRONG_PASSWORD, business_name="Морские приключения"):
    return client.post("/api/auth/signup", json={
        "name": name, "email": email, "password": password,
        "business_name": business_name,
    })


# ── happy path: full tenant, auto-login, pending access ─────────────────

def test_signup_creates_a_full_tenant_and_starts_a_session(api):
    client, web_api = api

    response = _signup(client)

    assert response.status_code == 200
    body = response.json()
    assert "error" not in body
    workspace_id = body["workspace_id"]

    workspace = _run(web_api.partner_repository.get_workspace(workspace_id))
    assert workspace is not None
    assert workspace.slug == "morskie-priklyucheniya"

    user = _run(web_api.web_auth_repository.get_user_by_email("ivan@example.com"))
    assert user is not None
    binding = _run(web_api.web_auth_repository.get_default_binding(user.id))
    assert binding is not None
    assert binding.workspace_id == workspace_id
    assert binding.telegram_user_id == -workspace_id

    membership = _run(web_api.partner_repository.get_membership(workspace_id, -workspace_id))
    assert membership is not None
    assert membership.role == "owner"
    assert membership.status == "active"

    # Auto-login: a session cookie is already set, no second login step.
    assert client.cookies.get("ta_session") is not None
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["workspace_id"] == workspace_id


def test_signup_grants_zero_product_access_until_paid(api):
    client, web_api = api
    response = _signup(client)
    workspace_id = response.json()["workspace_id"]

    subscription = _run(web_api.subscription_repository.get_for_workspace(workspace_id))
    assert subscription.status.value == "pending"

    me = client.get("/api/auth/me")
    assert me.json()["access_state"] == "pending"
    assert me.json()["access_granted"] is False

    # Not a fictitious grant anywhere - the real product surface is closed.
    materials = client.get("/api/materials")
    assert materials.status_code == 402


def test_signed_up_user_can_reach_billing_and_see_all_three_plans(api):
    client, _ = api
    _signup(client)

    billing_page = client.get("/billing")
    assert billing_page.status_code == 200

    status = client.get("/api/billing/status")
    assert status.status_code == 200
    body = status.json()
    assert body["billing_configured"] is True
    codes = {plan["code"] for plan in body["plans"]}
    assert codes == {"start", "standard", "full"}


def test_signed_up_user_home_redirects_to_subscription_inactive(api):
    client, _ = api
    _signup(client)

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/subscription-inactive"


# ── validation / duplicates / rate limit ─────────────────────────────────

def test_signup_rejects_duplicate_email(api):
    client, _ = api
    first = _signup(client, email="dup@example.com")
    assert "error" not in first.json()

    client.cookies.clear()
    second = _signup(client, email="dup@example.com", business_name="Другая компания")

    assert "error" in second.json()


def test_signup_duplicate_email_does_not_create_a_second_workspace(api):
    client, web_api = api
    _signup(client, email="dup2@example.com", business_name="Компания Один")
    workspaces_before = len(_run(_all_workspace_ids(web_api)))

    client.cookies.clear()
    _signup(client, email="dup2@example.com", business_name="Компания Два")

    workspaces_after = len(_run(_all_workspace_ids(web_api)))
    assert workspaces_after == workspaces_before


async def _all_workspace_ids(web_api):
    import aiosqlite
    async with aiosqlite.connect(web_api.settings.journal_db_path) as db:
        cursor = await db.execute("SELECT id FROM partner_workspaces")
        return await cursor.fetchall()


def test_signup_rejects_weak_password(api):
    client, _ = api
    response = _signup(client, password="short")
    assert "error" in response.json()


def test_signup_slug_collision_gets_a_safe_unique_suffix(api):
    client, web_api = api
    first = _signup(client, email="a@example.com", business_name="Vassian Travel")
    client.cookies.clear()
    second = _signup(client, email="b@example.com", business_name="Vassian Travel")

    ws1 = _run(web_api.partner_repository.get_workspace(first.json()["workspace_id"]))
    ws2 = _run(web_api.partner_repository.get_workspace(second.json()["workspace_id"]))
    assert ws1.slug != ws2.slug
    assert ws1.slug == "vassian-travel"
    assert ws2.slug == "vassian-travel-2"


def test_signup_rate_limited_after_too_many_attempts(api):
    """A fixed X-Forwarded-For makes the limiter key deterministic instead
    of depending on TestClient's own client.host default."""
    client, _ = api
    headers = {"X-Forwarded-For": "203.0.113.7"}

    for i in range(5):
        response = client.post(
            "/api/auth/signup", json={
                "name": "Test", "email": f"rl{i}@example.com",
                "password": STRONG_PASSWORD, "business_name": f"RL Co {i}",
            }, headers=headers,
        )
        assert "error" not in response.json(), response.json()
        client.cookies.clear()

    sixth = client.post(
        "/api/auth/signup", json={
            "name": "Test", "email": "rl-sixth@example.com",
            "password": STRONG_PASSWORD, "business_name": "RL Co 6",
        }, headers=headers,
    )
    assert "error" in sixth.json()


# ── tenant isolation ──────────────────────────────────────────────────────

def test_two_signed_up_workspaces_are_isolated(api):
    client, web_api = api
    a = _signup(client, email="tenant-a@example.com", business_name="Tenant A")
    workspace_a = a.json()["workspace_id"]

    client.cookies.clear()
    b = _signup(client, email="tenant-b@example.com", business_name="Tenant B")
    workspace_b = b.json()["workspace_id"]

    assert workspace_a != workspace_b
    # Session B can't see workspace A's billing order.
    order_a = _run(web_api.payment_order_repository.create_order(
        workspace_id=workspace_a, plan="standard", amount="990.00",
    ))
    response = client.get(f"/api/billing/orders/{order_a.id}")
    assert response.json()["order"] is None


# ── coexistence with the pre-existing invite-only flow ───────────────────

def test_invite_only_registration_still_works_alongside_signup(api):
    client, web_api = api
    ws, _ = _run(web_api.partner_repository.ensure_owner_workspace(OWNER_ID))
    login_as(client, web_api, ws.id, OWNER_ID)

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["workspace_id"] == ws.id


def test_signup_rolls_back_the_workspace_when_web_account_creation_fails(api, monkeypatch):
    """If anything fails AFTER provision_self_service_workspace() commits
    (e.g. web_auth_repository.create_user raising for some unrelated
    reason), the just-created workspace must not survive as an orphaned,
    unreachable tenant - see
    PartnerRepository.delete_freshly_provisioned_workspace."""
    client, web_api = api

    workspaces_before = _run(_all_workspace_ids(web_api))

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure after workspace creation")

    monkeypatch.setattr(web_api.web_auth_repository, "create_user", _boom)

    response = _signup(client, email="rollback@example.com", business_name="Rollback Co")

    assert "error" in response.json()
    workspaces_after = _run(_all_workspace_ids(web_api))
    assert len(workspaces_after) == len(workspaces_before)
    # No web account was left behind pointing at a deleted workspace.
    user = _run(web_api.web_auth_repository.get_user_by_email("rollback@example.com"))
    assert user is None


def test_old_cli_provisioned_partner_keeps_beta_access_not_pending(api):
    """provision_partner() (CLI/admin path) is untouched - a workspace
    provisioned that way still grandfathers in as 'beta' (full access),
    never 'pending'. Only provision_self_service_workspace() (this new
    signup) starts 'pending'."""
    client, web_api = api
    provisioned = _run(web_api.partner_repository.provision_partner(
        700000123, "CLI Partner", "cli-partner",
        business_name="CLI Partner", business_type="other",
        short_description="", context={},
    ))
    state = _run(web_api.subscription_repository.resolve_access_state(
        provisioned.workspace.id,
    ))
    assert state == "active"
