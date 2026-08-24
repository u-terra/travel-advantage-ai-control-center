from app.cards import source_analysis_card
from app.domain.content import SourceAnalysis


def analysis(**changes):
    values = dict(
        id=1, source_id=2, workspace_id=3, summary="Итог", key_facts=(),
        disputed_claims=(), audience_value="Польза", target_audiences=(),
        content_angles=(), recommended_formats=(), warnings=(), created_at="now",
    )
    values.update(changes)
    return SourceAnalysis(**values)


def test_minimal_card_hides_empty_sections_and_is_plain_text():
    card = source_analysis_card(analysis(summary="<b>Итог</b>"))
    assert "<b>Итог</b>" in card
    assert "💎 Что здесь ценного" in card
    # Пустые секции (нет key_facts/disputed_claims/warnings/content_angles) не
    # должны появляться в компактной карточке (Проблема 6).
    assert "Что нужно проверить" not in card
    assert "Как лучше подать" not in card
    # audience_value/target_audiences больше не дублируются в Telegram-карточке —
    # они остаются в SourceAnalysis/БД, но не раздувают пользовательский вывод.
    assert "Польза" not in card


def test_card_stays_below_limit_and_is_compact():
    card = source_analysis_card(analysis(
        summary="S" * 5000,
        key_facts=tuple("F" * 1000 for _ in range(20)),
        disputed_claims=("Проверить " + "D" * 1000,),
        audience_value="A" * 5000,
        target_audiences=tuple("T" * 1000 for _ in range(20)),
        content_angles=tuple("C" * 1000 for _ in range(20)),
        recommended_formats=tuple("R" * 1000 for _ in range(20)),
        warnings=("ВАЖНО " + "W" * 5000,),
    ))
    assert len(card) <= 3900
    assert card.startswith("🔎 Анализ источника")
    assert "🔍 Что нужно проверить / уточнить" in card
    assert "Проверить" in card


def test_card_sections_carry_disputed_claims_and_content_angles():
    card = source_analysis_card(analysis(
        key_facts=("Факт один",),
        disputed_claims=("Спорное утверждение",),
        content_angles=("Идея подачи",),
        recommended_formats=("post", "reels"),
    ))
    assert "💎 Что здесь ценного" in card
    assert "Факт один" in card
    assert "🔍 Что нужно проверить / уточнить" in card
    assert "Спорное утверждение" in card
    assert "✍️ Как лучше подать" in card
    assert "Идея подачи" in card
    assert "post" in card and "reels" in card
