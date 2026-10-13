"""Personal owner exception: the verified owner workspace has no tariff quotas
(materials, analyses, competitor and source slots) in web AND Telegram, while a
STANDARD workspace next to it stays limited and access protection is unchanged.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.subscription import SubscriptionPlan
from app.repositories.plan_usage_repository import PlanUsageRepository
from app.repositories.slot_guard import SlotLimitReached
from app.services import plan_limits
from app.services.plan_quota_service import PlanQuotaService
from tests.test_plan_slot_enforcement import Env


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


@pytest.fixture
def owner_and_other(env: Env, monkeypatch):
    owner = env.workspace(SubscriptionPlan.STANDARD, "owner-ws", 300)  # STANDARD, like production
    other = env.workspace(SubscriptionPlan.STANDARD, "other-ws", 301)
    monkeypatch.setattr(plan_limits, "OWNER_UNLIMITED_WORKSPACE_IDS", frozenset({owner}))
    return owner, other


def test_the_production_exception_is_exactly_the_verified_owner_workspace() -> None:
    # workspace 1 = u-terra@yandex.ru, Telegram 586249067 (verified read-only)
    assert plan_limits._PRODUCTION_OWNER_WORKSPACE_IDS == frozenset({1})


def test_no_exception_unless_a_test_enables_it() -> None:
    assert plan_limits.OWNER_UNLIMITED_WORKSPACE_IDS == frozenset()  # conftest default
    assert plan_limits.is_owner_unlimited_workspace(1) is False


def test_owner_has_no_material_or_analysis_quota_but_other_standard_does(
    env: Env, owner_and_other,
) -> None:
    owner, other = owner_and_other
    usage = PlanUsageRepository(env.db)
    run(usage.init())
    quota = PlanQuotaService(env.subs, usage)
    for ws in (owner, other):
        for _ in range(60):
            run(quota.record_material_created(ws))   # STANDARD limit is 40
        for _ in range(12):
            run(quota.record_competitor_analysis_completed(ws))  # limit is 10

    assert run(quota.check_material_quota(owner)).allowed is True
    assert run(quota.check_competitor_analysis_quota(owner)).allowed is True
    assert run(quota.check_material_quota(other)).allowed is False
    assert run(quota.check_competitor_analysis_quota(other)).allowed is False


def test_owner_slot_prechecks_are_open_but_other_standard_is_limited(
    env: Env, owner_and_other,
) -> None:
    owner, other = owner_and_other
    quota = PlanQuotaService(env.subs, PlanUsageRepository(env.db))

    assert run(quota.check_competitor_slot(owner, 11)).allowed is True
    assert run(quota.check_source_slot(owner, 33)).allowed is True
    assert run(quota.check_competitor_slot(other, 11)).allowed is False
    assert run(quota.check_source_slot(other, 33)).allowed is False


def test_owner_can_add_competitors_and_sources_beyond_the_standard_limits(
    env: Env, owner_and_other,
) -> None:
    owner, other = owner_and_other
    env.raw_competitors(owner, 11)   # the real 11 / 10
    env.raw_sources(owner, 33)       # the real 33 / 15
    env.raw_competitors(other, 11)
    env.raw_sources(other, 33)

    run(env.competitors.add_competitor(owner, "https://more-owner.example.com"))
    run(env.sources.add_source(owner, "https://more-owner.example.net/"))

    assert env.count("competitors", owner) == 12
    assert env.count("workspace_source_subscriptions", owner) == 34
    with pytest.raises(SlotLimitReached):
        run(env.competitors.add_competitor(other, "https://more-other.example.com"))
    with pytest.raises(SlotLimitReached):
        run(env.sources.add_source(other, "https://more-other.example.net/"))
    assert env.count("competitors", other) == 11  # the other workspace is untouched
    assert env.count("workspace_source_subscriptions", other) == 33


def test_owner_source_request_is_approved_at_a_full_limit(env: Env, owner_and_other) -> None:
    owner, other = owner_and_other
    env.raw_sources(owner, 15)
    env.raw_sources(other, 15)
    request_owner = run(env.sources.submit_source_request(owner, 300, "https://req-o.example.net/")).request
    request_other = run(env.sources.submit_source_request(other, 301, "https://req-p.example.net/")).request

    assert run(env.sources.approve_source_request(request_owner.id, owner)).status == "approved"
    with pytest.raises(SlotLimitReached):
        run(env.sources.approve_source_request(request_other.id, other))


def test_telegram_competitor_add_is_open_for_the_owner_and_limited_for_others(
    env: Env, owner_and_other,
) -> None:
    from app.handlers.competitors import receive_competitor_url

    owner, other = owner_and_other
    env.raw_competitors(owner, 11)
    env.raw_competitors(other, 11)
    quota = PlanQuotaService(env.subs, PlanUsageRepository(env.db))

    def _call(ws: int, url: str):
        message = SimpleNamespace(text=url, answer=AsyncMock())
        state = MagicMock(clear=AsyncMock())
        run(receive_competitor_url(message, state, env.competitors, SimpleNamespace(workspace_id=ws), quota))
        return message

    _call(owner, "https://tg-owner.example.com")
    blocked = _call(other, "https://tg-other.example.com")

    assert env.count("competitors", owner) == 12
    assert env.count("competitors", other) == 11
    assert "10 конкурентов" in blocked.answer.await_args.args[0]


def test_owner_exception_does_not_touch_the_access_gate(env: Env, owner_and_other) -> None:
    """Quotas are lifted, access protection is not: an expired owner
    subscription is still 'expired'."""
    owner, _ = owner_and_other
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with sqlite3.connect(env.db) as db:
        db.execute("UPDATE workspace_subscriptions SET paid_until=? WHERE workspace_id=?", (past, owner))

    assert run(env.subs.resolve_access_state(owner)) == "expired"
    assert plan_limits.is_owner_unlimited_workspace(owner) is True


def test_owner_subscription_row_is_not_modified_by_the_exception(env: Env, owner_and_other) -> None:
    owner, _ = owner_and_other
    before = run(env.subs.get_for_workspace(owner))
    quota = PlanQuotaService(env.subs, PlanUsageRepository(env.db))
    run(quota.check_material_quota(owner))
    run(env.competitors.add_competitor(owner, "https://touch.example.com"))

    assert run(env.subs.get_for_workspace(owner)) == before
    assert before.plan is SubscriptionPlan.STANDARD  # price/plan/payment untouched


# ── web: what the cabinet shows ─────────────────────────────────────────────

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("markdown")
pytest.importorskip("argon2")

from fastapi.testclient import TestClient  # noqa: E402

from tests._web_auth_test_helpers import login_as  # noqa: E402

OWNER_ID = 586249067


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("JOURNAL_DB_PATH", str(tmp_path / "journal.sqlite3"))
    monkeypatch.setenv("PLANNER_OPENAI_API_KEY", "test-key")
    import sys
    sys.modules.pop("app.web_api", None)
    import app.web_api as web_api

    with TestClient(web_api.app, base_url="https://testserver") as client:
        ws, _ = run(web_api.partner_repository.ensure_owner_workspace(OWNER_ID))
        login_as(client, web_api, ws.id, OWNER_ID)
        paid = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        run(web_api.subscription_repository.mark_paid(
            ws.id, external_payment_id="o", payment_provider="robokassa",
            paid_until=paid, plan=SubscriptionPlan.STANDARD,
        ))
        yield client, web_api, ws.id
    sys.modules.pop("app.web_api", None)


def test_cabinet_shows_owner_access_and_no_quota_panel_for_the_owner(api, monkeypatch) -> None:
    client, web_api, ws = api
    monkeypatch.setattr(plan_limits, "OWNER_UNLIMITED_WORKSPACE_IDS", frozenset({ws}))

    body = client.get("/api/billing/status").json()

    assert body["owner_access"] is True
    assert body["owner_access_label"] == "Доступ владельца — без тарифных ограничений"
    assert body["usage"] is None
    assert body["plan"] == "standard" and body["paid_until"]  # subscription data unchanged


def test_cabinet_keeps_the_quota_panel_for_a_regular_standard_workspace(api) -> None:
    client, web_api, ws = api  # exception disabled (conftest default)

    body = client.get("/api/billing/status").json()

    assert body["owner_access"] is False and body["owner_access_label"] is None
    assert body["usage"]["competitors_limit"] == 10


def test_owner_can_add_a_twelfth_competitor_over_http_but_a_regular_workspace_cannot(
    api, monkeypatch,
) -> None:
    client, web_api, ws = api
    import sqlite3 as _sq
    with _sq.connect(web_api.settings.journal_db_path) as db:
        for i in range(11):
            db.execute("INSERT INTO competitors (workspace_id, url, label, created_at) "
                       "VALUES (?, ?, ?, 'now')", (ws, f"https://o{i}.example.com", f"o{i}"))

    blocked = client.post("/api/competitors", json={"url": "https://x1.example.com"}).json()
    monkeypatch.setattr(plan_limits, "OWNER_UNLIMITED_WORKSPACE_IDS", frozenset({ws}))
    allowed = client.post("/api/competitors", json={"url": "https://x2.example.com"}).json()

    assert blocked["competitor"] is None and "10 конкурентов" in blocked["error"]
    assert allowed["competitor"] is not None


def test_billing_page_renders_the_owner_label() -> None:
    html = (Path(__file__).resolve().parents[1] / "app" / "templates" / "billing.html").read_text(
        encoding="utf-8")
    assert "Доступ владельца — без тарифных ограничений" in html
    assert "data.owner_access === true" in html
