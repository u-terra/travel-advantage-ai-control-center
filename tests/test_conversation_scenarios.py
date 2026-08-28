"""Schema-validation for the F1 conversation regression-suite skeleton.

This does NOT execute the scenarios - there is no resolver/producer wired up
yet (see the Conversation Core Foundation F1 report). It only guarantees the
scenario files are well-formed, uniquely identified, and honestly marked as
not-yet-implemented, so the skeleton itself cannot silently rot or claim
false coverage.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCENARIOS_DIR = Path(__file__).parent / "conversation_scenarios"

_REQUIRED_KEYS = {"id", "initial_state", "turns", "status"}
_ONE_OF_KEYS = ("expected_actions", "expected_state", "expected_failure")
_VALID_STATUSES = {"foundation_pending"}
_VALID_TURN_ROLES = {"user", "assistant", "system"}


def _load_all() -> list[tuple[Path, dict[str, Any]]]:
    files = sorted(SCENARIOS_DIR.glob("*.json"))
    return [(path, json.loads(path.read_text(encoding="utf-8"))) for path in files]


def test_scenarios_directory_exists_and_is_non_empty() -> None:
    assert SCENARIOS_DIR.is_dir()
    files = list(SCENARIOS_DIR.glob("*.json"))
    assert len(files) >= 20


def test_every_scenario_file_is_valid_json_with_required_keys() -> None:
    for path, scenario in _load_all():
        missing = _REQUIRED_KEYS - scenario.keys()
        assert not missing, f"{path.name} is missing required keys: {missing}"
        assert any(key in scenario for key in _ONE_OF_KEYS), (
            f"{path.name} must declare at least one of {_ONE_OF_KEYS}"
        )


def test_scenario_ids_match_filenames_and_are_unique() -> None:
    seen: dict[str, Path] = {}
    for path, scenario in _load_all():
        scenario_id = scenario["id"]
        assert isinstance(scenario_id, str) and scenario_id.strip(), (
            f"{path.name} has an empty/invalid id"
        )
        assert path.stem == scenario_id, (
            f"{path.name} filename must match its id ({scenario_id!r})"
        )
        assert scenario_id not in seen, (
            f"duplicate scenario id {scenario_id!r}: {seen[scenario_id].name} and {path.name}"
        )
        seen[scenario_id] = path


def test_every_scenario_is_honestly_marked_foundation_pending() -> None:
    """F1 ships no resolver - no scenario may claim to already pass."""
    for path, scenario in _load_all():
        assert scenario["status"] in _VALID_STATUSES, (
            f"{path.name} has an unexpected status {scenario['status']!r} - "
            f"F1 must not claim any scenario as implemented"
        )


def test_every_scenario_has_at_least_one_turn() -> None:
    for path, scenario in _load_all():
        turns = scenario["turns"]
        assert isinstance(turns, list) and turns, f"{path.name} must have >=1 turn"
        for turn in turns:
            assert isinstance(turn, dict), f"{path.name} has a non-object turn"
            assert "role" in turn and "text" in turn, (
                f"{path.name} has a turn missing role/text: {turn!r}"
            )
            assert turn["role"] in _VALID_TURN_ROLES, (
                f"{path.name} has an unknown turn role {turn['role']!r}"
            )
            assert isinstance(turn["text"], str) and turn["text"].strip(), (
                f"{path.name} has a turn with empty text"
            )


def test_initial_state_is_a_json_object() -> None:
    for path, scenario in _load_all():
        assert isinstance(scenario["initial_state"], dict), (
            f"{path.name}: initial_state must be a JSON object"
        )


def test_expected_actions_when_present_is_a_list_of_objects() -> None:
    for path, scenario in _load_all():
        if "expected_actions" not in scenario:
            continue
        actions = scenario["expected_actions"]
        assert isinstance(actions, list) and actions, (
            f"{path.name}: expected_actions must be a non-empty list"
        )
        for action in actions:
            assert isinstance(action, dict), f"{path.name} has a non-object expected action"


def test_covers_the_full_foundation_audit_scenario_set() -> None:
    """Pins the skeleton to the 20 scenarios from the Foundation audit
    (Conversation Core Foundation report, section 13) - not a re-derivation,
    just a guard against silently dropping one during future edits."""
    expected_ids = {
        "scn_01_three_topics_select_third_then_post",
        "scn_02_topic_then_another",
        "scn_03_post_then_shorter",
        "scn_04_post_then_livelier",
        "scn_05_post_then_prefer_previous",
        "scn_06_return_previous_version",
        "scn_07_make_second_variant_shorter",
        "scn_08_yes_with_pending_question",
        "scn_09_yes_without_pending_question",
        "scn_10_competitor_strengths",
        "scn_11_competitor_to_action_plan",
        "scn_12_radar_show_more",
        "scn_13_radar_post_by_second_idea",
        "scn_14_check_and_improve_actually_improves",
        "scn_15_broken_url_external_source_error",
        "scn_16_new_task_during_content_thread",
        "scn_17_cancel_reset",
        "scn_18_restart_short_follow_up_still_resolves",
        "scn_19_two_messages_sent_quickly",
        "scn_20_two_users_same_workspace_previous_does_not_leak",
    }
    actual_ids = {scenario["id"] for _, scenario in _load_all()}
    missing = expected_ids - actual_ids
    assert not missing, f"missing scenarios: {missing}"
