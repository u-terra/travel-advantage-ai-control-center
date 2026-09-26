"""ORCHESTRAVEL v1 plan limits table - app.services.plan_limits is the
single source of truth; this test locks in the exact numbers from the
product spec so nobody accidentally drifts them."""

from __future__ import annotations

from app.domain.content import ARTIFACT_TYPES
from app.services.plan_limits import (
    PLAN_LIMITS,
    get_plan_limits,
    is_quota_counted_material,
    plan_display_name,
)


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


# ── is_quota_counted_material: the real app.domain.content.ARTIFACT_TYPES ──


def test_domain_declares_the_expected_artifact_types() -> None:
    """Locks in the actual set found in app.domain.content.ARTIFACT_TYPES -
    if this ever drifts, is_quota_counted_material's exclusion list below
    needs a matching look, not a silent behavior change."""
    assert set(ARTIFACT_TYPES) == {
        "post", "video_script", "stories", "client_message",
        "objection_reply", "faq", "content_plan_item", "other",
    }


def test_post_is_quota_counted_material() -> None:
    assert is_quota_counted_material("post") is True


def test_other_declared_content_material_types_are_quota_counted() -> None:
    """Every real user-facing content material type counts, not just "post"
    - video_script/stories/objection_reply/faq/content_plan_item are not
    produced by any live code path today, but if/when one starts producing
    them, this predicate must already treat them as material without
    another edit here."""
    for artifact_type in ("video_script", "stories", "objection_reply", "faq", "content_plan_item"):
        assert is_quota_counted_material(artifact_type) is True


def test_client_message_is_never_quota_counted() -> None:
    """«Ответить клиенту» / assistant client-reply drafts never spend the
    material quota, per the product spec."""
    assert is_quota_counted_material("client_message") is False


def test_other_artifact_type_is_never_quota_counted() -> None:
    """"other" is app.handlers.text_review's save-already-reviewed-text
    Artifact - not a Content Factory/material-orchestration generation."""
    assert is_quota_counted_material("other") is False


def test_unknown_artifact_type_is_never_quota_counted() -> None:
    assert is_quota_counted_material("something_invented") is False
