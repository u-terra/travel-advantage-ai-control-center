"""F2D: deterministic Telegram rendering for a content_topics PendingOffer.

render_content_topics_text/build_content_topics_keyboard both build
straight from the already-structured PendingOffer/OfferItem objects
produced by ContentTopicsService - never from any prose the model may have
also produced. No parsing, no regex, nothing reconstructed from text (see
the F2D "structured objects -> prose -> parser" prohibition).

Deliberately not registered as an aiogram handler/router here: F2D ships no
trigger that calls ContentTopicsService yet (see the F2D report, "НЕ ДЕЛАТЬ
ТРИГГЕР"), so there is nothing for a callback_query handler to be attached
to. This module is ready for a future handler to import and use as-is.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.domain.action_contract import ActionContract, ActionContractValidationError
from app.domain.conversation_state import PendingOffer

CONTENT_TOPICS_SELECT_PREFIX = "content_topics_select:"

_NUMBER_EMOJI = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣")


def render_content_topics_text(offer: PendingOffer) -> str:
    """Builds the list-of-topics message straight from OfferItem objects."""
    lines = ["💡 Вот варианты тем для поста:", ""]
    for index, item in enumerate(offer.items, start=1):
        angle = item.payload.get("angle")
        lines.append(f"{index}. {item.label}")
        if isinstance(angle, str) and angle.strip():
            lines.append(f"   {angle.strip()}")
        lines.append("")
    lines.append("Выберите номер темы кнопкой ниже.")
    return "\n".join(lines)


def build_content_topics_keyboard(offer: PendingOffer) -> InlineKeyboardMarkup:
    """Inline 1️⃣/2️⃣/3️⃣ buttons - callback_data carries only offer_id/item_id,
    never title/angle/reason (F2D constraint)."""
    buttons = [
        [
            InlineKeyboardButton(
                text=_NUMBER_EMOJI[index] if index < len(_NUMBER_EMOJI) else str(index + 1),
                callback_data=f"{CONTENT_TOPICS_SELECT_PREFIX}{offer.id}:{item.id}",
            )
        ]
        for index, item in enumerate(offer.items)
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def content_topics_action_contract(raw_callback_data: str) -> ActionContract | None:
    """Deterministic callback_data -> ActionContract, same adapter pattern as
    app.handlers.menu._radar_content_action_contract. offer_id/item_id
    travel in slots, never as subject_ref: a PendingOffer is not a
    Conversation Subject (CONVERSATION_SUBJECT_REF_TYPES stays closed to
    {work_subject, competitor, artifact} - see app.domain.conversation_state).
    """
    raw = raw_callback_data.removeprefix(CONTENT_TOPICS_SELECT_PREFIX)
    parts = raw.split(":")
    if len(parts) != 2:
        return None
    raw_offer_id, item_id = parts
    if not raw_offer_id.isdigit() or int(raw_offer_id) <= 0 or not item_id:
        return None
    try:
        return ActionContract(
            intent="select_offer_item",
            action="select_content_topic",
            subject_ref_type=None,
            subject_ref_id=None,
            slots={"offer_id": int(raw_offer_id), "item_id": item_id},
            source="button",
            confidence=1.0,
        )
    except ActionContractValidationError:
        return None
