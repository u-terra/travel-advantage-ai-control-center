"""Freshness of a source vs. time-sensitive claims.

Incident 2026-10-16: a draft about train discounts used a Tutu.ru article dated
03.11.2020 and presented "-30% for a birthday", "50 welcome points" and "up to
2%" as current conditions. The date was stored but never reached generation.

An old/undated source is not blocked and evergreen content is not touched; only
expiring (numeric/conditional) statements of a stale/undated source are held
back through the existing fail-closed path (disputed_claims -> sanitizer).
"""
from __future__ import annotations

import asyncio
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.repositories.workspace_signal_repository import WorkspaceSignalRepository
from app.services.article_pages import parse_published_at
from app.services.draft_sanitizer import sanitize_draft_text
from app.services.llm.models import ContentDraft
from app.services.material_orchestration import MaterialOrchestrationService
from app.services.source_freshness import (
    STALE_SOURCE_AGE_DAYS,
    apply_freshness_to_analysis,
    assess_source_freshness,
    extract_time_sensitive_claims,
    text_is_time_sensitive,
)
from tests.test_journal_handlers import analysis_payload, business_profile
from tests.test_signal_post_real_dispatcher import (
    _create_post_buttons,
    _fixture,
    _press,
    _press_find_signals,
    _recommender,
    _web_record,
)

NOW = datetime(2026, 10, 16, 8, 0, tzinfo=timezone.utc)

# ── fixtures modelled on the production incident ────────────────────────────

TUTU_TITLE = "Как купить дешёвые билеты на поезд в Петербург"
TUTU_SUMMARY = (
    "Материал перечисляет способы сэкономить на ж/д билетах в Санкт-Петербург, "
    "включая несколько тарифов и бонусных программ РЖД и Туту.ру."
)
TUTU_BODY = (
    "3 ноября 2020\n"
    "Поезд в Петербург можно взять дешевле, если знать несколько хитростей. "
    "У «Сапсана» бывает скидка 30% для именинника и до четырёх друзей в течение "
    "недели до или после дня рождения. "
    "В «Туту.ру Бонус» дают 50 приветственных баллов за регистрацию, а потом "
    "начисляют до 2% от суммы заказа. "
    "У РЖД тоже есть бонусная карта."
)
TUTU_PUBLISHED = "2020-11-03T00:00:00+00:00"

# Verbatim shape of production draft #66 (what the model wrote from that article).
DRAFT_66 = (
    "Петербург на поезде можно брать дешевле, если смотреть не только на обычный билет.\n\n"
    "У РЖД и Туту есть несколько вариантов, которые иногда заметно меняют итоговую сумму. "
    "Например, у «Сапсана» бывает скидка 30% для именинника и до четырёх друзей — "
    "в течение недели до или после дня рождения.\n\n"
    "Плюс можно копить баллы: в «Туту.ру Бонус» дают 50 приветственных баллов за "
    "регистрацию, а потом начисляют до 2% от суммы заказа. У РЖД тоже есть бонусная карта.\n\n"
    "Перед покупкой стоит сравнить расписание, тариф и условия по возрасту или событию."
)

ISTANBUL_TITLE = "Ура, подешевело: прямые рейсы из Москвы в Стамбул за 4800 рублей в один конец"
ISTANBUL_SUMMARY = (
    "Авиакомпании открыли осеннюю распродажу. Билет в одну сторону стоит от 4800 рублей, "
    "туда-обратно от 10600 рублей. Вылеты в конце года, багаж оплачивается отдельно, "
    "условия тарифа зависят от даты вылета."
)
ISTANBUL_DRAFT = (
    "Прямые рейсы из Москвы в Стамбул за 4800 рублей в одну сторону звучат заманчиво, "
    "но такие цены всегда стоит смотреть внимательно.\n\n"
    "Смотри даты вылета и что входит в тариф."
)

TRILOBITE_TITLE = "Как точно выкопать себе трилобита"
TRILOBITE_BODY = (
    "Окаменелости трилобитов находят в известняковых карьерах Ленинградской области. "
    "Нужны молоток, зубило и защитные очки. Лучше ехать весной, когда земля уже оттаяла, "
    "и брать с собой термос. Порода раскалывается по слоям, поэтому бить нужно вдоль "
    "плоскости напластования, а не поперёк. Находка весит около 200 граммов."
)
TRILOBITE_DRAFT = (
    "Трилобита можно найти в карьерах Ленинградской области.\n\n"
    "Нужны молоток, зубило и очки. Находка весит около 200 граммов."
)


def _iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


# ── 2. RSS RFC 822 ──────────────────────────────────────────────────────────


def test_parse_published_at_reads_rss_rfc822() -> None:
    assert parse_published_at("Wed, 06 May 2026 07:50:57 +0000") == "2026-05-06T07:50:57+00:00"
    assert parse_published_at("Fri, 01 May 2026 15:30:42 +0300") == "2026-05-01T12:30:42+00:00"


@pytest.mark.parametrize("raw", ["", None, "garbage", "Wed, 99 Foo 2026", "3 ноября 2020"])
def test_parse_published_at_unparseable_stays_none(raw) -> None:
    assert parse_published_at(raw) is None


def test_parse_published_at_iso_still_works() -> None:
    assert parse_published_at("2026-10-15T11:50:18+00:00") == "2026-10-15T11:50:18+00:00"
    assert parse_published_at("2020-11-03") == "2020-11-03T00:00:00+00:00"


# ── 3-4. age_days / time-sensitive detection ────────────────────────────────


def test_age_and_explicit_context_line() -> None:
    fresh = assess_source_freshness(TUTU_PUBLISHED, TUTU_BODY, now=NOW)
    assert fresh.age_days == 2173
    assert fresh.context_line == "Дата публикации источника: 2020-11-03, возраст: 2173 дня."

    assert assess_source_freshness(_iso(1), "x", now=NOW).context_line == (
        "Дата публикации источника: 2026-10-15, возраст: 1 день."
    )
    assert assess_source_freshness(_iso(11), "x", now=NOW).context_line.endswith("11 дней.")
    assert assess_source_freshness(_iso(22), "x", now=NOW).context_line.endswith("22 дня.")


def test_undated_source_is_explicitly_unknown_never_fresh() -> None:
    result = assess_source_freshness("", "x", now=NOW)
    assert result.published_date is None and result.age_days is None
    assert result.context_line == "Дата публикации источника: неизвестна."


def test_future_date_does_not_produce_negative_age() -> None:
    assert assess_source_freshness(_iso(-3), "x", now=NOW).age_days == 0


@pytest.mark.parametrize(
    "text",
    [
        "Билет стоит 4800 рублей", "цена от 4 800 ₽", "скидка 30%", "тариф «Эконом»",
        "акция действует до 31 декабря", "50 баллов за регистрацию", "кэшбэк на карту",
        "виза не нужна", "правила въезда изменились", "прямые рейсы", "новое расписание",
        "промокод на скидку", "цена $120",
    ],
)
def test_time_sensitive_markers(text: str) -> None:
    assert text_is_time_sensitive(text)


def test_evergreen_text_is_not_time_sensitive() -> None:
    assert not text_is_time_sensitive(TRILOBITE_TITLE, TRILOBITE_BODY)
    assert not text_is_time_sensitive("Тропа начинается у станции, идти около 13 км.")


def test_threshold_is_a_named_constant_and_applies_only_with_time_sensitive_content() -> None:
    assert STALE_SOURCE_AGE_DAYS == 90
    price = "Билет стоит 4800 рублей"
    assert not assess_source_freshness(_iso(STALE_SOURCE_AGE_DAYS), price, now=NOW).stale_time_sensitive
    assert assess_source_freshness(_iso(STALE_SOURCE_AGE_DAYS + 1), price, now=NOW).stale_time_sensitive
    # age alone never makes evergreen material stale
    assert not assess_source_freshness(_iso(5000), TRILOBITE_BODY, now=NOW).stale_time_sensitive


# ── 5. fail-closed handling of expiring claims ──────────────────────────────


def test_extracted_claims_are_short_number_centred_snippets() -> None:
    claims = extract_time_sensitive_claims(TUTU_TITLE, TUTU_SUMMARY, TUTU_BODY)
    joined = " | ".join(claims)
    assert "30%" in joined and "50 приветственных баллов" in joined and "2%" in joined
    assert all(len(c) <= 300 for c in claims)


def test_tutu_2020_claims_are_not_presented_as_current() -> None:
    """The production incident: 30% / 50 points / up to 2% must not survive."""
    freshness = assess_source_freshness(
        TUTU_PUBLISHED, TUTU_TITLE, TUTU_SUMMARY, TUTU_BODY, now=NOW,
    )
    assert freshness.stale_time_sensitive

    # Baseline: without the freshness policy the old facts pass the sanitizer.
    baseline = sanitize_draft_text(DRAFT_66, disputed_claims=analysis_payload().disputed_claims)
    assert "30%" in baseline and "50 приветственных" in baseline and "2%" in baseline

    analysis = apply_freshness_to_analysis(
        analysis_payload(), freshness, TUTU_TITLE, TUTU_SUMMARY, TUTU_BODY,
    )
    assert analysis.disputed_claims  # held back through the existing fail-closed path
    assert any("Устаревший или недатированный источник" in w for w in analysis.warnings)

    cleaned = sanitize_draft_text(DRAFT_66, disputed_claims=analysis.disputed_claims)
    assert "30%" not in cleaned
    assert "50 приветственных" not in cleaned
    assert "2%" not in cleaned
    # evergreen framing of the post survives; the source is not blocked
    assert "Петербург на поезде можно брать дешевле" in cleaned


def test_existing_disputed_claims_are_kept_and_not_duplicated() -> None:
    freshness = assess_source_freshness(TUTU_PUBLISHED, TUTU_BODY, now=NOW)
    original = analysis_payload(disputed_claims=("Спорное утверждение источника",))
    once = apply_freshness_to_analysis(original, freshness, TUTU_BODY)
    twice = apply_freshness_to_analysis(once, freshness, TUTU_BODY)
    assert once.disputed_claims[0] == "Спорное утверждение источника"
    assert twice.disputed_claims == once.disputed_claims
    assert twice.warnings == once.warnings


def test_undated_time_sensitive_source_is_handled_cautiously() -> None:
    freshness = assess_source_freshness("", TUTU_TITLE, TUTU_SUMMARY, TUTU_BODY, now=NOW)
    assert freshness.age_days is None and freshness.stale_time_sensitive
    analysis = apply_freshness_to_analysis(analysis_payload(), freshness, TUTU_BODY)
    cleaned = sanitize_draft_text(DRAFT_66, disputed_claims=analysis.disputed_claims)
    assert "30%" not in cleaned and "50 приветственных" not in cleaned
    assert any("дата публикации неизвестна" in w for w in analysis.warnings)


def test_fresh_istanbul_post_passes_untouched() -> None:
    freshness = assess_source_freshness(
        _iso(1), ISTANBUL_TITLE, ISTANBUL_SUMMARY, now=NOW,
    )
    assert freshness.time_sensitive and not freshness.stale_time_sensitive
    original = analysis_payload()
    assert apply_freshness_to_analysis(original, freshness, ISTANBUL_SUMMARY) is original
    assert "4800 рублей" in sanitize_draft_text(ISTANBUL_DRAFT, disputed_claims=original.disputed_claims)


def test_old_evergreen_trilobite_passes_untouched() -> None:
    freshness = assess_source_freshness(
        "2023-11-20T00:00:00+00:00", TRILOBITE_TITLE, TRILOBITE_BODY, now=NOW,
    )
    assert freshness.age_days > 900
    assert not freshness.time_sensitive and not freshness.stale_time_sensitive
    original = analysis_payload()
    assert apply_freshness_to_analysis(original, freshness, TRILOBITE_BODY) is original
    cleaned = sanitize_draft_text(TRILOBITE_DRAFT, disputed_claims=original.disputed_claims)
    assert "200 граммов" in cleaned


def test_missing_analysis_stays_missing() -> None:
    freshness = assess_source_freshness(TUTU_PUBLISHED, TUTU_BODY, now=NOW)
    assert apply_freshness_to_analysis(None, freshness, TUTU_BODY) is None


# ── generation spec: explicit meta field for the model ──────────────────────


def _spec(freshness, *, summary, body=""):
    return MaterialOrchestrationService().build_radar_generation_spec(
        42, business_profile(), title="Тема", summary=summary, source_type="rss",
        origin_type="publisher_post", url="https://example.org/1", category="",
        reason="r", analysis=analysis_payload(), body=body, source_freshness=freshness,
    )


def test_spec_carries_explicit_publication_meta_for_a_stale_source() -> None:
    freshness = assess_source_freshness(TUTU_PUBLISHED, TUTU_SUMMARY, TUTU_BODY, now=NOW)
    spec = _spec(freshness, summary=TUTU_SUMMARY, body=TUTU_BODY)

    assert spec.source_facts["source_publication"] == (
        "Дата публикации источника: 2020-11-03, возраст: 2173 дня."
    )
    assert spec.source_facts["source_age_days"] == 2173
    assert spec.source_facts["stale_time_sensitive_source"] is True
    assert spec.untrusted_source_content.startswith("Дата публикации источника: 2020-11-03")
    assert any("Источник устарел или не датирован" in c for c in spec.constraints)


def test_spec_without_stale_source_has_no_extra_constraint() -> None:
    freshness = assess_source_freshness(_iso(1), ISTANBUL_SUMMARY, now=NOW)
    spec = _spec(freshness, summary=ISTANBUL_SUMMARY)
    assert spec.source_facts["stale_time_sensitive_source"] is False
    assert not any("Источник устарел" in c for c in spec.constraints)
    assert spec.untrusted_source_content.startswith("Дата публикации источника: 2026-10-15")


def test_spec_without_freshness_is_exactly_as_before() -> None:
    spec = _spec(None, summary=ISTANBUL_SUMMARY)
    assert "source_publication" not in spec.source_facts
    assert not any("Источник устарел" in c for c in spec.constraints)


# ── 1. published_at reaches generation: Radar repository ────────────────────


def _make_radar_db(path: Path, *, with_published_column: bool) -> None:
    published = "published_at TEXT," if with_published_column else ""
    with sqlite3.connect(path) as db:
        db.execute(f"""CREATE TABLE lead_signals (
            id INTEGER PRIMARY KEY, source_id TEXT, created_at TEXT, source_type TEXT,
            origin_type TEXT, source_name TEXT, source_url TEXT, item_url TEXT,
            item_title TEXT, item_summary TEXT, {published}
            status TEXT DEFAULT 'new', ai_score REAL, ai_category TEXT, ai_reason TEXT,
            suggested_message TEXT, notes TEXT, llm_checked INTEGER DEFAULT 0,
            llm_checked_at TEXT, llm_signal_type TEXT, llm_score REAL,
            llm_relevance TEXT, llm_reason TEXT, llm_suggested_message TEXT)""")
        columns = "id, source_id, created_at, source_type, origin_type, source_name, item_url, item_title, item_summary"
        values = "1, 'vk1', '2026-10-16 05:00:36', 'vk_api', 'publisher_post', 'VK', 'https://vk.com/w=1', 'Рейсы', 'Прямые рейсы в Стамбул от 4800 рублей'"
        if with_published_column:
            columns += ", published_at"
            values += ", '2026-10-15T11:50:18+00:00'"
        db.execute(f"INSERT INTO lead_signals({columns}) VALUES ({values})")
        db.commit()


async def _interpretation_record(tmp_path: Path, *, with_published_column: bool):
    radar = tmp_path / "radar.db"
    _make_radar_db(radar, with_published_column=with_published_column)
    repo = WorkspaceSignalRepository(tmp_path / "journal.db", radar)
    rows = [{
        "id": 1, "workspace_id": 42, "radar_signal_id": 1, "source_id": "vk1",
        "usage_role_snapshot": None, "status": "new", "notes": "", "ai_score": 70.0,
        "ai_category": "content", "ai_reason": "r", "suggested_message": None,
        "created_at": "2026-10-16 05:00:36",
    }]
    return (await repo._attach_raw(rows))[0]


def test_radar_published_at_is_loaded_into_workspace_signal_record(tmp_path) -> None:
    record = asyncio.run(_interpretation_record(tmp_path, with_published_column=True))
    assert record.published_at == "2026-10-15T11:50:18+00:00"
    assert record.raw_created_at == "2026-10-16 05:00:36"  # collection time kept separate


def test_radar_db_without_published_column_means_unknown_not_error(tmp_path) -> None:
    record = asyncio.run(_interpretation_record(tmp_path, with_published_column=False))
    assert record.published_at == ""


def test_radar_published_at_reaches_source_facts(tmp_path) -> None:
    record = asyncio.run(_interpretation_record(tmp_path, with_published_column=True))
    freshness = assess_source_freshness(
        record.published_at, record.item_title, record.item_summary, now=NOW,
    )
    spec = _spec(freshness, summary=record.item_summary)
    assert spec.source_facts["source_publication"] == (
        "Дата публикации источника: 2026-10-15, возраст: 1 день."
    )


# ── 1. end to end through the real aiogram Dispatcher ───────────────────────


def _run_radar_button(*, title, summary, published_at, draft_text, body=None):
    async def scenario():
        dispatcher, bot, session, artifacts, llm, record = _fixture()
        record.item_title = title
        record.item_summary = summary
        if body is not None:
            record.body = body
        if published_at is not None:
            record.published_at = published_at
        llm.generate_draft.return_value = ContentDraft(draft_text, ())
        with patch("app.services.lead_radar._load_recommender", return_value=_recommender()):
            await _press_find_signals(dispatcher, bot)
            button = next(
                b for b in _create_post_buttons(session) if b.text.startswith("📝 Создать пост")
            )
            await _press(dispatcher, bot, button)
        return session, artifacts, llm

    return asyncio.run(scenario())


def _saved_text(artifacts) -> str:
    return artifacts.create_artifact_with_initial_version.await_args.kwargs["content"]


def test_dispatcher_old_tutu_article_gets_date_and_loses_expiring_claims() -> None:
    session, artifacts, llm = _run_radar_button(
        title=TUTU_TITLE, summary=TUTU_SUMMARY, body=TUTU_BODY,
        published_at=TUTU_PUBLISHED, draft_text=DRAFT_66,
    )
    # the model is told the date explicitly, in the analysis context and the generation request
    analysis_text = llm.analyze_source.call_args.kwargs["source_text"]
    assert re.match(
        r"Дата публикации источника: 2020-11-03, возраст: \d+ (?:день|дня|дней)\.\n", analysis_text,
    )
    generation_text = llm.generate_draft.call_args.kwargs["source_text"]
    assert "Дата публикации источника: 2020-11-03" in generation_text
    assert "Источник устарел или не датирован" in generation_text
    # expiring facts are cut from what the user sees and what is saved
    shown = "\n".join(session.sent_texts())
    saved = _saved_text(artifacts)
    for text in (shown, saved):
        assert "30%" not in text and "50 приветственных" not in text and "2%" not in text
    assert "Петербург на поезде можно брать дешевле" in saved
    assert "не подтверждена" in shown  # the existing safety note for withheld facts


def test_dispatcher_fresh_istanbul_post_keeps_its_price() -> None:
    published = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    session, artifacts, llm = _run_radar_button(
        title=ISTANBUL_TITLE, summary=ISTANBUL_SUMMARY, published_at=published,
        draft_text=ISTANBUL_DRAFT,
    )
    assert re.match(
        r"Дата публикации источника: \d{4}-\d{2}-\d{2}, возраст: 1 день\.\n",
        llm.analyze_source.call_args.kwargs["source_text"],
    )
    assert "Источник устарел" not in llm.generate_draft.call_args.kwargs["source_text"]
    assert "4800 рублей" in _saved_text(artifacts)


def test_dispatcher_old_evergreen_trilobite_is_not_touched() -> None:
    session, artifacts, llm = _run_radar_button(
        title=TRILOBITE_TITLE, summary=TRILOBITE_BODY, published_at="2023-11-20T00:00:00+00:00",
        draft_text=TRILOBITE_DRAFT,
    )
    assert "Источник устарел" not in llm.generate_draft.call_args.kwargs["source_text"]
    assert "200 граммов" in _saved_text(artifacts)


def test_dispatcher_undated_time_sensitive_source_is_cautious() -> None:
    session, artifacts, llm = _run_radar_button(
        title=TUTU_TITLE, summary=TUTU_SUMMARY, body=TUTU_BODY,
        published_at=None, draft_text=DRAFT_66,
    )
    assert llm.analyze_source.call_args.kwargs["source_text"].startswith(
        "Дата публикации источника: неизвестна.\n"
    )
    saved = _saved_text(artifacts)
    assert "30%" not in saved and "50 приветственных" not in saved


def test_dispatcher_web_post_hands_published_at_to_generation() -> None:
    """webpost: the stored page date is no longer dropped at the handoff."""
    async def scenario():
        dispatcher, bot, session, artifacts, llm, _record = _fixture()
        web_record = _web_record()
        web_record.title = TUTU_TITLE
        web_record.summary = TUTU_SUMMARY
        web_record.body = TUTU_BODY
        web_record.published_at = TUTU_PUBLISHED
        llm.generate_draft.return_value = ContentDraft(DRAFT_66, ())
        dispatcher["web_signal_repository"] = SimpleNamespace(
            get_for_workspace=AsyncMock(return_value=web_record)
        )
        await _press(dispatcher, bot, SimpleNamespace(callback_data="webpost:9"))
        return artifacts, llm

    artifacts, llm = asyncio.run(scenario())
    assert "Дата публикации источника: 2020-11-03" in llm.analyze_source.call_args.kwargs["source_text"]
    saved = _saved_text(artifacts)
    assert "30%" not in saved and "50 приветственных" not in saved
