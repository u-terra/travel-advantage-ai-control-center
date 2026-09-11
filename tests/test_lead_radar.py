from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app.services.lead_radar import (
    DISPLAY_LIMIT,
    LeadSignal,
    build_summary,
    build_workspace_signals,
    category_label,
)
from app.services.lead_radar import (
    _CONTENT_TIER_DEFAULT,
    _CONTENT_TIER_PRODUCT_AD,
    _CONTENT_TIER_STRONG,
    _CONTENT_TIER_WEAK,
    _content_quality_rank,
    _is_allowed_row,
)


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


# --- build_workspace_signals: freshness по смыслу категории + квоты 3/3/5 ---
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


def test_quota_observe_keeps_up_to_three_newest_market_signals():
    # observe квота поднята с 1 до 3 — из четырёх свежих market_signal
    # остаются три самых новых, а не один.
    records = [
        _fake_record("market_signal", hours_ago=h, interpretation_id=i)
        for i, h in enumerate([1, 2, 3, 4], start=1)
    ]
    result = _build(records)
    assert [s.id for s in result] == [1, 2, 3]


def test_quota_content_keeps_up_to_five_newest_content_signals():
    # content квота поднята с 1 до 5 — из шести свежих content_signal
    # остаются пять самых новых, а не один. limit=DISPLAY_LIMIT передан явно,
    # чтобы тест проверял реальную квоту, а не случайно совпал с дефолтным
    # limit=5 хелпера _build().
    records = [
        _fake_record("content_signal", hours_ago=h, interpretation_id=i)
        for i, h in enumerate([1, 2, 3, 4, 5, 6], start=1)
    ]
    result = _build(records, limit=DISPLAY_LIMIT)
    assert [s.id for s in result] == [1, 2, 3, 4, 5]


def test_quota_is_three_three_five_across_categories():
    records = (
        [_fake_record("lead_signal", hours_ago=h, interpretation_id=i)
         for i, h in enumerate([1, 2, 3, 4], start=1)]
        + [_fake_record("market_signal", hours_ago=h, interpretation_id=i)
           for i, h in enumerate([1, 2, 3, 4], start=10)]
        + [_fake_record("content_signal", hours_ago=h, interpretation_id=i)
           for i, h in enumerate([1, 2, 3, 4, 5, 6], start=20)]
    )
    # limit=DISPLAY_LIMIT явно: это тест на реальный лимит показа (10), а не
    # на дефолтный limit=5 хелпера _build().
    result = _build(records, limit=DISPLAY_LIMIT)
    # careful_reply: 3 новейших из 4 (id 1-3); observe: 3 новейших из 4
    # (id 10-12); content: квота даёт 5 (id 20-24), но итоговый срез по
    # DISPLAY_LIMIT=10 обрезает самый старый из них — см. тест ниже.
    assert [s.id for s in result] == [1, 2, 3, 10, 11, 12, 20, 21, 22, 23]


def test_overall_cap_trims_last_content_item_when_all_quotas_are_full():
    """Сумма квот (3+3+5=11) намеренно больше DISPLAY_LIMIT (10) — это
    зафиксированный, а не случайный компромисс минимальной реализации (см.
    комментарий у _ACTION_QUOTA в app/services/lead_radar.py). Когда все три
    bucket'а заполнены до квоты одновременно, итоговый срез по DISPLAY_LIMIT
    обрезает ровно один элемент — самый старый допущенный content_signal,
    потому что content идёт последним по _ACTION_PRIORITY."""
    records = (
        [_fake_record("lead_signal", hours_ago=h, interpretation_id=i)
         for i, h in enumerate([1, 2, 3], start=1)]
        + [_fake_record("market_signal", hours_ago=h, interpretation_id=i)
           for i, h in enumerate([1, 2, 3], start=10)]
        + [_fake_record("content_signal", hours_ago=h, interpretation_id=i)
           for i, h in enumerate([1, 2, 3, 4, 5], start=20)]
    )
    assert len(records) == 11
    result = _build(records, limit=DISPLAY_LIMIT)
    assert len(result) == 10
    assert [s.id for s in result] == [1, 2, 3, 10, 11, 12, 20, 21, 22, 23]
    # id 24 — пятый (самый старый допустимый) content_signal — не поместился.
    assert 24 not in [s.id for s in result]


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


# --- DISPLAY_LIMIT: единая точка настройки для Telegram и Web -----------------

def test_display_limit_constant_is_ten():
    # Пин значения: случайное изменение константы должно быть осознанным,
    # а не побочным эффектом соседней правки.
    assert DISPLAY_LIMIT == 10


def test_telegram_and_web_call_sites_share_the_display_limit_constant():
    """Единая бизнес-логика Telegram и Web: оба вызова build_workspace_signals()
    для списка сигналов ОБЯЗАНЫ ссылаться на один и тот же импортированный
    lead_radar.DISPLAY_LIMIT, а не на повторённое число в двух местах —
    иначе значения могут незаметно разойтись при будущей правке одного файла."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    menu_src = (root / "app" / "handlers" / "menu.py").read_text(encoding="utf-8")
    web_api_src = (root / "app" / "web_api.py").read_text(encoding="utf-8")

    assert "build_workspace_signals(lead_radar_config, records, limit=DISPLAY_LIMIT)" in menu_src
    assert "build_workspace_signals(lead_radar_config, records, limit=DISPLAY_LIMIT)" in web_api_src


def test_authorization_lookups_still_use_limit_one():
    """limit=1 в проверке конкретной записи — это намеренная авторизационная
    проверка (см. app/handlers/menu.py и app/web_api.py), а не список для
    показа. Она не должна случайно расшириться вместе с DISPLAY_LIMIT."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    menu_src = (root / "app" / "handlers" / "menu.py").read_text(encoding="utf-8")
    web_api_src = (root / "app" / "web_api.py").read_text(encoding="utf-8")

    assert "build_workspace_signals(lead_radar_config, [record], limit=1)" in menu_src
    assert "build_workspace_signals(lead_radar_config, [record], limit=1)" in web_api_src


# --- Content-bucket ranking: quality tier, не только created_at DESC --------
# ai_score здесь ИГНОРИРУЕТСЯ намеренно (он константа на категорию, не оценка
# качества конкретной записи) — тесты ниже это явно проверяют.

def test_content_quality_rank_unit_values():
    # Сильный практический сигнал.
    assert _content_quality_rank("Как добраться из аэропорта Галеан", "") == _CONTENT_TIER_STRONG
    assert _content_quality_rank("Маршрут выходного дня по Карелии", "") == _CONTENT_TIER_STRONG
    assert _content_quality_rank("Что взять с собой в Китай", "") == _CONTENT_TIER_STRONG
    # Товарная реклама гаджета.
    assert _content_quality_rank(
        "Когда впереди новый маршрут",
        "Amazfit T-Rex 3 Pro отслеживает GPS-трек и высоту на любом рельефе",
    ) == _CONTENT_TIER_PRODUCT_AD
    # Абстрактный lifestyle без пользы.
    assert _content_quality_rank("Красота северного леса.", "") == _CONTENT_TIER_WEAK
    # Конкретная travel-тема без формального how-to — не проваливается в weak.
    assert _content_quality_rank(
        "Хотите увидеть камушки в горошек?",
        "Отправляйтесь на мыс Четырёх скал — одно из самых живописных и "
        "малоизвестных мест побережья",
    ) == _CONTENT_TIER_DEFAULT


def test_A_practical_guide_outranks_fresher_gadget_ad():
    records = [
        _fake_record(
            "content_signal", hours_ago=5, interpretation_id=1,
            item_title="Как добраться из аэропорта Галеан в центр Рио",
            item_summary="Сравниваем автобус, метро и такси",
        ),
        _fake_record(
            "content_signal", hours_ago=1, interpretation_id=2,
            item_title="Когда впереди новый маршрут",
            item_summary="Amazfit T-Rex 3 Pro отслеживает GPS-трек и высоту на любом рельефе",
        ),
    ]
    result = _build(records, limit=DISPLAY_LIMIT)
    assert [s.id for s in result] == [1, 2]


def test_B_packing_list_outranks_fresher_abstract_lifestyle_post():
    records = [
        _fake_record(
            "content_signal", hours_ago=5, interpretation_id=1,
            item_title="Что взять с собой в Китай", item_summary="",
        ),
        _fake_record(
            "content_signal", hours_ago=1, interpretation_id=2,
            item_title="Красота северного леса.", item_summary="",
        ),
    ]
    result = _build(records, limit=DISPLAY_LIMIT)
    assert [s.id for s in result] == [1, 2]


def test_C_weekend_route_outranks_abstract_lifestyle_post():
    records = [
        _fake_record(
            "content_signal", hours_ago=5, interpretation_id=1,
            item_title="Маршрут выходного дня по Карелии", item_summary="",
        ),
        _fake_record(
            "content_signal", hours_ago=1, interpretation_id=2,
            item_title="Красота северного леса.", item_summary="",
        ),
    ]
    result = _build(records, limit=DISPLAY_LIMIT)
    assert [s.id for s in result] == [1, 2]


def test_D_specific_place_topic_does_not_fall_below_product_ad():
    """"мыс Четырёх скал" не должен проваливаться только потому, что в нём
    нет формального "как добраться" — старее рекламы, но всё равно выше."""
    records = [
        _fake_record(
            "content_signal", hours_ago=10, interpretation_id=1,
            item_title="Хотите увидеть камушки в горошек?",
            item_summary=(
                "Отправляйтесь на мыс Четырёх скал — одно из самых "
                "живописных и малоизвестных мест побережья"
            ),
        ),
        _fake_record(
            "content_signal", hours_ago=1, interpretation_id=2,
            item_title="Когда впереди новый маршрут",
            item_summary="Amazfit T-Rex 3 Pro отслеживает GPS-трек и высоту на любом рельефе",
        ),
    ]
    result = _build(records, limit=DISPLAY_LIMIT)
    assert [s.id for s in result] == [1, 2]


def test_E_product_ad_still_appears_when_few_content_candidates():
    """Не жёсткий drop: если других content-кандидатов нет, реклама всё
    равно попадает в выдачу — просто не выигрывает за более высокий тир."""
    records = [
        _fake_record(
            "content_signal", hours_ago=1, interpretation_id=1,
            item_title="Когда впереди новый маршрут",
            item_summary="Amazfit T-Rex 3 Pro отслеживает GPS-трек",
        ),
    ]
    result = _build(records, limit=DISPLAY_LIMIT)
    assert [s.id for s in result] == [1]


def test_F_same_tier_ties_are_broken_by_freshness():
    records = [
        _fake_record(
            "content_signal", hours_ago=5, interpretation_id=1,
            item_title="Как добраться до вулкана", item_summary="",
        ),
        _fake_record(
            "content_signal", hours_ago=1, interpretation_id=2,
            item_title="Как доехать до водопада", item_summary="",
        ),
    ]
    result = _build(records, limit=DISPLAY_LIMIT)
    # Оба tier STRONG — внутри тира более свежий (id=2) идёт первым.
    assert [s.id for s in result] == [2, 1]


def test_G_observe_and_careful_reply_still_sort_by_freshness_only():
    """Content-ranking не должен утекать в другие bucket'ы: observe/
    careful_reply сортируются только по времени, даже если текст выглядел бы
    "сильным"/"рекламным" в content-ranking."""
    records = [
        _fake_record(
            "market_signal", hours_ago=5, interpretation_id=1,
            item_title="Смартфон нового поколения", item_summary="реклама гаджета",
        ),
        _fake_record(
            "market_signal", hours_ago=1, interpretation_id=2,
            item_title="Как добраться из аэропорта", item_summary="",
        ),
    ]
    result = _build(records, limit=DISPLAY_LIMIT)
    assert [s.id for s in result] == [2, 1]


def test_H_ai_score_does_not_affect_content_ordering():
    records = [
        _fake_record(
            "content_signal", hours_ago=5, interpretation_id=1,
            item_title="Как добраться до вулкана", item_summary="", ai_score=10.0,
        ),
        _fake_record(
            "content_signal", hours_ago=1, interpretation_id=2,
            item_title="Смартфон нового поколения", item_summary="реклама гаджета",
            ai_score=99.0,
        ),
    ]
    result = _build(records, limit=DISPLAY_LIMIT)
    # id=2 свежее и имеет намного более высокий ai_score, но реклама
    # (tier PRODUCT_AD) не должна обогнать гид (tier STRONG).
    assert [s.id for s in result] == [1, 2]
