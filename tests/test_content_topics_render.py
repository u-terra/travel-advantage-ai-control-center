from __future__ import annotations

import dataclasses

import pytest

from app.domain.conversation_state import OfferItem, PendingOffer
from app.services.content_topics_render import (
    CONTENT_TOPICS_SELECT_PREFIX,
    build_content_topics_keyboard,
    content_topics_action_contract,
    render_content_topics_text,
)


def _offer(offer_id: int = 5) -> PendingOffer:
    return PendingOffer(
        id=offer_id, workspace_id=42, telegram_user_id=100, offer_type="content_topics",
        items=(
            OfferItem(id="1", label="Тема раз", payload={"angle": "История", "reason": "Актуально"}),
            OfferItem(id="2", label="Тема два", payload={"angle": "Еда", "reason": "Сезонно"}),
            OfferItem(id="3", label="Тема три", payload={"angle": "Природа", "reason": "Визуально"}),
        ),
        created_at="2026-08-28T10:00:00+00:00", expires_at=None, consumed_at=None,
    )


# ── render_content_topics_text (I: built from structured objects) ────────


def test_render_lists_every_item_label_and_angle_in_order():
    text = render_content_topics_text(_offer())
    assert text.index("Тема раз") < text.index("Тема два") < text.index("Тема три")
    assert "История" in text
    assert "Еда" in text
    assert "Природа" in text


def test_render_does_not_leak_reason_into_the_shown_text():
    """Only label/angle are meant for the list view - reason is selection
    context, not something to show every user up front."""
    text = render_content_topics_text(_offer())
    for reason in ("Актуально", "Сезонно", "Визуально"):
        assert reason not in text


# ── build_content_topics_keyboard (J: payload stays out of callback_data) ─


def test_keyboard_has_one_button_per_item_with_offer_and_item_id():
    keyboard = build_content_topics_keyboard(_offer(offer_id=5))
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    assert [button.callback_data for button in buttons] == [
        f"{CONTENT_TOPICS_SELECT_PREFIX}5:1",
        f"{CONTENT_TOPICS_SELECT_PREFIX}5:2",
        f"{CONTENT_TOPICS_SELECT_PREFIX}5:3",
    ]


def test_keyboard_callback_data_never_contains_title_angle_or_reason():
    keyboard = build_content_topics_keyboard(_offer())
    for row in keyboard.inline_keyboard:
        for button in row:
            for forbidden in ("Тема", "История", "Еда", "Природа", "Актуально"):
                assert forbidden not in button.callback_data


# ── content_topics_action_contract (N: slots only, no subject_ref) ───────


def test_action_contract_adapter_builds_expected_contract():
    contract = content_topics_action_contract(f"{CONTENT_TOPICS_SELECT_PREFIX}5:2")
    assert contract is not None
    assert contract.intent == "select_offer_item"
    assert contract.action == "select_content_topic"
    assert contract.subject_ref_type is None
    assert contract.subject_ref_id is None
    assert contract.slots == {"offer_id": 5, "item_id": "2"}
    assert contract.source == "button"
    assert contract.confidence == 1.0


def test_action_contract_is_frozen():
    contract = content_topics_action_contract(f"{CONTENT_TOPICS_SELECT_PREFIX}5:2")
    assert contract is not None
    assert dataclasses.is_dataclass(contract)
    with pytest.raises(dataclasses.FrozenInstanceError):
        contract.intent = "something_else"  # type: ignore[misc]


def test_action_contract_adapter_rejects_malformed_callback_data():
    for raw in (
        f"{CONTENT_TOPICS_SELECT_PREFIX}not-a-number:2",
        f"{CONTENT_TOPICS_SELECT_PREFIX}0:2",
        f"{CONTENT_TOPICS_SELECT_PREFIX}-1:2",
        f"{CONTENT_TOPICS_SELECT_PREFIX}5",
        f"{CONTENT_TOPICS_SELECT_PREFIX}5:",
        "unrelated_prefix:5:2",
        "",
    ):
        assert content_topics_action_contract(raw) is None
