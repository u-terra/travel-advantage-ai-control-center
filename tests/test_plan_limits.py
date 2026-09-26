"""ORCHESTRAVEL v1 plan limits table - app.services.plan_limits is the
single source of truth; this test locks in the exact numbers from the
product spec so nobody accidentally drifts them."""

from __future__ import annotations

from app.services.plan_limits import PLAN_LIMITS, get_plan_limits, plan_display_name


def test_start_limits_match_spec() -> None:
    limits = PLAN_LIMITS["start"]
    assert limits.materials_limit == 15
    assert limits.competitor_analyses_limit == 3
    assert limits.window_days == 14
    assert limits.competitor_slot_limit == 3
    assert limits.source_slot_limit == 5


def test_standard_limits_match_spec() -> None:
    limits = PLAN_LIMITS["standard"]
    assert limits.materials_limit == 40
    assert limits.competitor_analyses_limit == 10
    assert limits.window_days == 30
    assert limits.competitor_slot_limit == 10
    assert limits.source_slot_limit == 15


def test_full_limits_match_spec() -> None:
    limits = PLAN_LIMITS["full"]
    assert limits.materials_limit == 100
    assert limits.competitor_analyses_limit == 30
    assert limits.window_days == 30
    assert limits.competitor_slot_limit == 25
    assert limits.source_slot_limit == 40


def test_beta_plan_has_no_enforced_limits() -> None:
    assert get_plan_limits("beta") is None


def test_unknown_plan_has_no_enforced_limits() -> None:
    assert get_plan_limits("some_future_plan") is None


def test_none_plan_has_no_enforced_limits() -> None:
    assert get_plan_limits(None) is None


def test_plan_display_names() -> None:
    assert plan_display_name("start") == "START"
    assert plan_display_name("standard") == "STANDARD"
    assert plan_display_name("full") == "FULL"
