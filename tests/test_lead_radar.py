from __future__ import annotations

from datetime import date

from app.services.lead_radar import (
    LeadSignal,
    build_summary,
    category_label,
)
from app.services.lead_radar import _is_allowed_row


def _fresh_row(**overrides) -> dict[str, object]:
    row: dict[str, object] = {
        "created_at": date.today().isoformat(),
        "ai_category": "market_signal",
        "item_url": "https://example.org/post/1",
        "item_title": "Куда поехать летом",
        "item_summary": "Обычный тревел-контент",
        "ai_reason": "релевантно",
        "source_type": "rss",
    }
    row.update(overrides)
    return row


def _signal(**overrides) -> LeadSignal:
    base = dict(
        id=1,
        created_at=date.today().isoformat(),
        source_type="rss",
        score=72.0,
        category="market_signal",
        title="Куда поехать летом",
        url="https://example.org/post/1",
        recommended_action="observe",
        action_label="Наблюдать",
        action_reason="релевантно",
    )
    base.update(overrides)
    return LeadSignal(**base)


# --- фильтрация noise ---

def test_noise_category_row_is_filtered_out():
    assert _is_allowed_row(_fresh_row(ai_category="noise")) is False


def test_non_noise_category_row_is_allowed():
    assert _is_allowed_row(_fresh_row(ai_category="market_signal")) is True


# --- подпись content_signal ---

def test_content_signal_label():
    assert category_label("content_signal") == "💡 Тема для контента"


def test_content_signal_label_in_summary():
    summary = build_summary([_signal(category="content_signal")])
    assert "💡 Тема для контента" in summary


# --- подпись market_signal ---

def test_market_signal_label():
    assert category_label("market_signal") == "👀 Наблюдать рынок"


def test_market_signal_label_in_summary():
    summary = build_summary([_signal(category="market_signal")])
    assert "👀 Наблюдать рынок" in summary


# --- нейтральный fallback для прочих категорий ---

def test_unknown_category_uses_neutral_fallback():
    assert category_label("something_else") == "🔹 Сигнал интереса"
    assert category_label("") == "🔹 Сигнал интереса"
