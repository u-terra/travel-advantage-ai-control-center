from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app.services.lead_radar import (
    LeadSignal,
    build_summary,
    build_workspace_signals,
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


# --- подпись lead_signal ---

def test_lead_signal_label():
    assert category_label("lead_signal") == "🎯 Вопрос клиента"


# --- Radar UX: карточка сигнала без технической кухни ------------------------

def test_summary_does_not_show_numeric_score():
    summary = build_summary([_signal(score=45.0)])
    assert "score" not in summary.lower()
    assert "45" not in summary


def test_summary_does_not_show_telegram_source_type_label():
    summary = build_summary([_signal(source_type="telegram")])
    assert "telegram" not in summary.lower()


def test_summary_does_not_show_rss_source_type_label():
    summary = build_summary([_signal(source_type="rss")])
    assert "rss" not in summary.lower()


def test_summary_shows_human_readable_why_label():
    summary = build_summary([_signal(action_reason="Активно обсуждают в чате клуба")])
    assert "Почему стоит обратить внимание:" in summary
    assert "Активно обсуждают в чате клуба" in summary


def test_summary_uses_neutral_fallback_when_action_reason_is_empty():
    # Не придумываем факты/статистику — только нейтральная формулировка,
    # если action_reason от action_recommender пуст.
    summary = build_summary([_signal(action_reason="", recommended_action="observe")])
    assert "Почему стоит обратить внимание:" in summary
    assert "активно обсуждается на рынке" in summary


def test_content_idea_card_has_editorial_angle():
    summary = build_summary([_signal(
        category="content_signal", recommended_action="content",
    )])
    assert "Как можно подать:" in summary


def test_observe_signal_card_has_no_forced_content_angle():
    summary = build_summary([_signal(
        category="market_signal", recommended_action="observe",
    )])
    assert "Как можно подать:" not in summary


def test_careful_reply_signal_card_has_no_forced_content_angle():
    summary = build_summary([_signal(
        category="lead_signal", recommended_action="careful_reply",
    )])
    assert "Как можно подать:" not in summary


# --- build_workspace_signals: freshness по смыслу категории + квоты 3/1/1 ---
# ai_score сюда намеренно не подмешиваем: он константа на категорию в
# продакшен-данных (lead=70/market=45/content=32) и не различает качество
# внутри категории — единственный осмысленный критерий сейчас — свежесть.

_ACTION_BY_CATEGORY = {
    "lead_signal": "careful_reply",
    "market_signal": "observe",
    "content_signal": "content",
}


def _fake_recommender():
    return SimpleNamespace(
        recommend_action=lambda row: {
            "recommended_action": _ACTION_BY_CATEGORY.get(row.get("ai_category"), "skip"),
            "action_reason": "reason",
        },
        action_label=lambda action: action,
    )


def _ts(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _fake_record(category: str, hours_ago: float, interpretation_id: int = 1, **overrides):
    base = dict(
        interpretation_id=interpretation_id,
        raw_created_at=_ts(hours_ago),
        source_type="rss",
        origin_type="publisher_post",
        ai_score=50.0,
        ai_category=category,
        ai_reason="reason",
        item_title=f"title {interpretation_id}",
        item_summary="summary",
        item_url=f"https://example.org/{interpretation_id}",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _build(records, *, limit=5):
    with patch(
        "app.services.lead_radar._load_recommender", return_value=_fake_recommender()
    ):
        return build_workspace_signals(SimpleNamespace(), records, limit=limit)


def test_lead_signal_older_than_72h_is_excluded():
    records = [_fake_record("lead_signal", hours_ago=73)]
    assert _build(records) == []


def test_lead_signal_within_72h_is_included():
    records = [_fake_record("lead_signal", hours_ago=71)]
    assert [s.id for s in _build(records)] == [1]


def test_market_signal_older_than_7_days_is_excluded():
    records = [_fake_record("market_signal", hours_ago=24 * 7 + 1)]
    assert _build(records) == []


def test_market_signal_within_7_days_is_included():
    records = [_fake_record("market_signal", hours_ago=24 * 7 - 1)]
    assert [s.id for s in _build(records)] == [1]


def test_content_signal_older_than_14_days_is_excluded():
    records = [_fake_record("content_signal", hours_ago=24 * 14 + 1)]
    assert _build(records) == []


def test_content_signal_within_14_days_is_included():
    records = [_fake_record("content_signal", hours_ago=24 * 14 - 1)]
    assert [s.id for s in _build(records)] == [1]


def test_quota_keeps_up_to_three_newest_lead_signals():
    records = [
        _fake_record("lead_signal", hours_ago=h, interpretation_id=i)
        for i, h in enumerate([1, 2, 3, 4], start=1)
    ]
    result = _build(records)
    # квота — 3, из четырёх свежих lead_signal остаются три самых новых
    assert [s.id for s in result] == [1, 2, 3]


def test_quota_is_three_one_one_across_categories():
    records = (
        [_fake_record("lead_signal", hours_ago=h, interpretation_id=i)
         for i, h in enumerate([1, 2, 3, 4], start=1)]
        + [_fake_record("market_signal", hours_ago=h, interpretation_id=i)
           for i, h in enumerate([1, 2], start=10)]
        + [_fake_record("content_signal", hours_ago=h, interpretation_id=i)
           for i, h in enumerate([1, 2], start=20)]
    )
    result = _build(records)
    assert [s.id for s in result] == [1, 2, 3, 10, 20]


def test_missing_category_is_not_backfilled_by_another():
    records = [_fake_record("market_signal", hours_ago=1, interpretation_id=10)]
    result = _build(records)
    assert [s.id for s in result] == [10]


def test_result_can_be_shorter_than_five_signals():
    records = [
        _fake_record("lead_signal", hours_ago=1, interpretation_id=1),
        _fake_record("lead_signal", hours_ago=2, interpretation_id=2),
    ]
    assert len(_build(records)) == 2
