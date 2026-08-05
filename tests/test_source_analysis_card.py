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
    assert "Что важно:" not in card
    assert "Ценность для аудитории:\nПольза" in card


def test_card_stays_below_limit_truncates_items_and_keeps_warnings():
    card = source_analysis_card(analysis(
        summary="S" * 5000,
        key_facts=tuple("F" * 1000 for _ in range(20)),
        disputed_claims=("Проверить",),
        audience_value="A" * 5000,
        target_audiences=tuple("T" * 1000 for _ in range(20)),
        content_angles=tuple("C" * 1000 for _ in range(20)),
        recommended_formats=tuple("R" * 1000 for _ in range(20)),
        warnings=("ВАЖНО " + "W" * 5000,),
    ))
    assert len(card) <= 3900
    assert card.startswith("🔎 Анализ источника")
    assert "Предупреждения:" in card
    assert "ВАЖНО" in card
    assert "Что требует проверки:" in card
