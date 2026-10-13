"""Regression tests for the 13.10 "signal -> post" incident.

Real cases covered (external services are STUBBED where noted - the fetcher
and the LLM provider are fakes; everything between them is real code):

- t-j.ru catalog pages / Lonely Planet category page were stored as articles;
- the VK trail post was explained as "динамика цен на рынке";
- every idea got the same "Свяжите тему с вашим направлением..." line;
- Telegram radar generation sent 10062 chars to Content Factory (cap 6000);
- an undated web page read as brand new (collection time used as date).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from app.planner.fetch import FetchedPublicSource, _extract_html_full
from app.repositories.web_signal_repository import WebSignalRecord, WebSignalRepository
from app.services.article_pages import (
    extract_article_links,
    looks_like_catalog_page,
    url_looks_like_catalog,
)
from app.services.generation_request_builder import (
    build_budgeted_provider_request,
    build_provider_generation_request,
    signal_material_is_sufficient,
)
from app.services.lead_radar import LeadSignal, content_angle_hint
from app.services.material_orchestration import MaterialOrchestrationService
from app.services.signal_explanation import angle_for, reason_for, topic_kind
from app.services.signal_service import build_unified_feed
from app.services.web_signal_collector import WebSignalCollector
from tests.llm_fakes import FakeLLMProvider
from tests.test_material_orchestration import profile as business_profile
from tests.test_source_catalog_repository import setup, web_source
from tests.test_web_signal_collector import analysis, build, make_fetcher, run

TRAIL_TITLE = "Едем бродить из Питера по тропам Про Сестрорецкое болото я рассказывал недавно."
TRAIL_TEXT = (
    "Едем бродить из Питера по тропам. Про экотропу Петяярви: тропа начинается со станции "
    "Петтяярви, приозерского направления. Ехал электричкой от Девяткино, цена билета 250 руб, "
    "ехать 1.5 часа. Маршрут стандартный около 13 км. Основные точки — озеро Лебяжье, река Волчья "
    "и плотина Сосновской ГЭС."
)


# ── 1. catalog -> concrete article ───────────────────────────────────────────

ARTICLE_BODY = ("Куда поехать осенью без визы: подробный разбор по странам, ценам на билеты и "
                "сезонам. ") * 12


def _article(url: str, title: str, published: str = "2026-10-11T09:00:00+03:00"):
    return FetchedPublicSource(
        url=url, final_url=url, title=title, text=ARTICLE_BODY, content_type="text/html",
        published_at=published,
    )


def _catalog(url: str, article_urls: list[tuple[str, str]]):
    return FetchedPublicSource(
        url=url, final_url=url, title="Мир путешествий", text="\n".join(t for _, t in article_urls),
        content_type="text/html", links=tuple(article_urls),
    )


def test_catalog_url_shapes_from_the_incident_are_recognized():
    for url in (
        "https://t-j.ru/flows/world-travel/",
        "https://t-j.ru/flows/cost-of-travel/",
        "https://t-j.ru/flows/travel-diary/",
        "https://t-j.ru/flows/readers-travel/",
        "https://www.lonelyplanet.com/articles/category/travel-advice",
        "https://example.com/blog/?page=2",
    ):
        assert url_looks_like_catalog(url), url
    assert not url_looks_like_catalog("https://www.lonelyplanet.com/articles/best-time-to-visit-japan")
    assert not url_looks_like_catalog("https://t-j.ru/flows/travel/kak-poehat-v-gruziyu-osenyu-na-mashine/")


def test_catalog_page_yields_concrete_articles_not_a_catalog_summary(tmp_path: Path):
    catalog_url = "https://t-j.ru/flows/world-travel/"
    art1 = "https://t-j.ru/kak-poehat-v-gruziyu-osenyu-bez-turoperatora/"
    art2 = "https://t-j.ru/pyat-sposobov-sekonomit-na-otele-v-stambule/"
    pages = {
        catalog_url: _catalog(catalog_url, [
            (art1, "Как поехать в Грузию осенью без туроператора и не пожалеть"),
            (art2, "Пять способов сэкономить на отеле в Стамбуле этой осенью"),
            ("/flows/travel-diary/", "Дневник путешествий читателей Т—Ж"),
        ]),
        art1: _article(art1, "Как поехать в Грузию осенью"),
        art2: _article(art2, "Сэкономить на отеле в Стамбуле", published=""),
    }
    _, owner, catalog, signals = build(tmp_path, [web_source("tj", url=catalog_url)])
    provider = FakeLLMProvider(analysis=analysis(summary="Разбор поездки в Грузию осенью."))
    collector = WebSignalCollector(catalog, signals, provider, fetcher=make_fetcher(pages))

    run(collector.collect_for_workspace(owner))

    stored = run(signals.list_for_workspace(owner))
    urls = {r.item_url for r in stored}
    assert urls == {art1, art2}
    assert catalog_url not in urls
    by_url = {r.item_url: r for r in stored}
    # URL, content and the page-declared date are kept with the publication.
    assert by_url[art1].body.startswith("Куда поехать осенью")
    assert by_url[art1].published_at is not None and by_url[art1].published_at.startswith("2026-10-11")
    # No date on the page -> unknown, never invented.
    assert by_url[art2].published_at is None
    assert all(not r.is_fallback for r in stored)


def test_catalog_without_usable_article_links_stores_nothing(tmp_path: Path):
    catalog_url = "https://www.lonelyplanet.com/articles/category/travel-advice"
    pages = {catalog_url: _catalog(catalog_url, [("/articles/category/news", "News")])}
    _, owner, catalog, signals = build(tmp_path, [web_source("lp", url=catalog_url)])
    provider = FakeLLMProvider(analysis=analysis())
    collector = WebSignalCollector(catalog, signals, provider, fetcher=make_fetcher(pages))

    outcome = run(collector.collect_for_workspace(owner))

    assert run(signals.list_for_workspace(owner)) == []
    assert outcome.failed_source_ids == ("lp",)
    # The list itself is never sent to the LLM as if it were an article.
    provider.analyze_source.assert_not_called()


def test_listing_link_inside_a_catalog_is_not_followed_or_stored(tmp_path: Path):
    catalog_url = "https://t-j.ru/flows/world-travel/"
    sub = "https://t-j.ru/flows/cost-of-travel/"
    pages = {
        catalog_url: _catalog(catalog_url, [(sub, "Сколько стоит путешествовать по миру в 2026")]),
        sub: _catalog(sub, []),
    }
    _, owner, catalog, signals = build(tmp_path, [web_source("tj", url=catalog_url)])
    provider = FakeLLMProvider(analysis=analysis())
    collector = WebSignalCollector(catalog, signals, provider, fetcher=make_fetcher(pages))

    run(collector.collect_for_workspace(owner))

    assert run(signals.list_for_workspace(owner)) == []


def test_extract_article_links_prefers_headline_anchors_on_same_site():
    links = (
        ("https://other.example/post-about-trips-and-tours", "Статья на чужом сайте про поездки"),
        ("/short", "Коротко"),
        ("/kak-vybrat-otel-u-morya-bez-oshibok-v-sezon", "Как выбрать отель у моря без ошибок в сезон"),
        ("/tag/morya/", "Все материалы по теме отдыха на море"),
    )
    assert extract_article_links("https://site.example/flows/travel/", links) == [
        "https://site.example/kak-vybrat-otel-u-morya-bez-oshibok-v-sezon"
    ]


def test_fetch_parser_captures_links_and_declared_date_only():
    html = (
        "<html><head><title>T</title>"
        '<meta property="article:published_time" content="2026-10-10T08:00:00+03:00"></head>'
        "<body><main><p>" + "текст " * 30 + '</p><a href="/a/b-c-d-e-f">Заголовок статьи про тропы</a>'
        "</main></body></html>"
    )
    _text, _title, links, published = _extract_html_full(html)
    assert links == (("/a/b-c-d-e-f", "Заголовок статьи про тропы"),)
    assert published.startswith("2026-10-10")
    no_date = _extract_html_full("<html><body><main><p>" + "текст " * 30 + "</p></main></body></html>")
    assert no_date[3] == ""


def test_page_with_many_headline_links_and_no_paragraphs_is_a_catalog():
    links = tuple(
        (f"/material-{i}-pro-poezdki-i-sovety", f"Материал номер {i} про поездки и советы туристам")
        for i in range(9)
    )
    text = "\n".join(a for _, a in links)
    assert looks_like_catalog_page("https://site.example/news", text, links)
    assert not looks_like_catalog_page("https://site.example/news", ARTICLE_BODY, links[:3])


# ── 2. insufficient material -> clear refusal ────────────────────────────────

def test_headline_only_catalog_text_is_not_enough_material():
    assert not signal_material_is_sufficient("Explore travel advice stories", "Список статей.", "")
    assert not signal_material_is_sufficient("Тема", "Описание", "")


def test_stored_publication_body_counts_as_enough_material():
    assert signal_material_is_sufficient("Тема", "Коротко.", ARTICLE_BODY)


# ── 3. trail is not explained by price dynamics ──────────────────────────────

RECOMMENDER_REASON = "VK-пост: динамика цен на рынке — наблюдать за рынком"


def test_trail_post_is_not_explained_as_price_dynamics():
    reason = reason_for(RECOMMENDER_REASON, TRAIL_TITLE, TRAIL_TEXT, recommended_action="content",
                        source_label="VK-пост")
    assert "динамика цен" not in reason
    assert "маршрут" in reason.lower()


def test_real_price_news_keeps_the_recommender_reason():
    text = "Авиабилеты подорожали на 15% за месяц, цены на рейсы в Турцию растут."
    assert reason_for(RECOMMENDER_REASON, "Билеты подорожали", text) == RECOMMENDER_REASON


def test_unrelated_reason_is_left_untouched():
    reason = "VK-пост: гид или маршрут по направлению — взять как тему для контента"
    assert reason_for(reason, TRAIL_TITLE, TRAIL_TEXT) == reason


def test_different_ideas_get_different_angles():
    trail = angle_for(TRAIL_TITLE, TRAIL_TEXT)
    visa = angle_for("Новые правила въезда в Таиланд", "Для въезда нужна электронная форма и паспорт.")
    season = angle_for("Когда лучше ехать на Бали", "Лучшее время — сезон сухой погоды.")
    assert len({trail, visa, season}) == 3
    assert "Свяжите тему с вашим направлением" not in trail + visa + season


def test_content_angle_hint_depends_on_the_signal_and_never_crashes_without_one():
    signal = LeadSignal(
        id=1, created_at="2026-10-12", source_type="vk_api", score=45.0, category="market_signal",
        title=TRAIL_TITLE, url="https://vk.com/wall-1_2", recommended_action="content",
        action_label="Тема", action_reason="", summary=TRAIL_TEXT,
    )
    assert topic_kind(signal.title, signal.summary) == "route"
    assert content_angle_hint(signal) == angle_for(signal.title, signal.summary)
    assert content_angle_hint()  # legacy no-argument call stays valid


# ── 4. feed freshness: unknown date is not "fresh", archive is separate ──────

def _web_record(rid, *, published_at, fetched_at=None, stale=False, url=None, title=None):
    now = datetime.now(timezone.utc).isoformat()
    return WebSignalRecord(
        id=rid, workspace_id=1, source_id="s", source_name="Т—Ж",
        source_url="https://t-j.ru/", item_url=url or f"https://t-j.ru/kak-poehat-v-gruziyu-osenyu-{rid}/",
        title=title or f"Как поехать в Грузию осенью, часть {rid}", summary="Разбор поездки.",
        fetched_at=fetched_at or now, published_at=published_at, is_stale_dated=stale,
        created_at=now, updated_at=now, body=ARTICLE_BODY,
    )


def _feed(records):
    return build_unified_feed(
        [], [], records, limit=10, category_label=lambda c: c, why_text=lambda s: "",
        content_angle_hint=lambda signal=None: "hint",
    )


def test_undated_web_record_is_marked_unknown_and_ranked_after_dated_ones():
    fresh_date = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    feed = _feed([_web_record(1, published_at=None), _web_record(2, published_at=fresh_date)])
    assert [item.id for item in feed] == ["web:2", "web:1"]
    undated = feed[1]
    assert undated.freshness_state == "unknown" and undated.freshness_hours is None
    assert feed[0].freshness_state == "fresh"


def test_stale_year_material_is_shown_as_archive_after_everything_else():
    fresh_date = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    feed = _feed([
        _web_record(1, published_at=fresh_date, stale=True),
        _web_record(2, published_at=None),
        _web_record(3, published_at=fresh_date),
    ])
    assert [item.freshness_state for item in feed] == ["fresh", "unknown", "archive"]


def test_catalog_rows_stored_by_earlier_runs_no_longer_reach_the_feed():
    rows = [
        _web_record(1, published_at=None, url="https://t-j.ru/flows/world-travel/", title="Мир"),
        _web_record(2, published_at=None, url="https://www.lonelyplanet.com/articles/category/travel-advice",
                    title="Explore travel advice stories | Lonely Planet"),
    ]
    assert _feed(rows) == []


def test_web_feed_item_has_content_specific_reason_and_angle():
    row = _web_record(1, published_at=None)
    item = _feed([row])[0]
    assert item.action_reason and "Т—Ж" in item.action_reason
    assert item.content_hint


# ── 5. generation request fits Content Factory and keeps what matters ────────

def _radar_spec(summary: str, body: str = ""):
    return MaterialOrchestrationService().build_radar_generation_spec(
        10, business_profile(), title=TRAIL_TITLE, summary=summary, source_type="vk_api",
        origin_type="publisher_post", url="https://vk.com/wall-55554881_299593",
        category="market_signal", reason="reason", body=body,
    )


def test_oversized_signal_is_packed_under_the_content_factory_cap():
    spec = _radar_spec(TRAIL_TEXT * 12)  # ~4.5k chars of summary: the failing shape
    old = build_provider_generation_request(spec)  # default 11_000 limit (the bug)
    assert len(old.source_text) > 6000  # what Content Factory answered HTTP 400 to

    packed = build_budgeted_provider_request(spec, limit=6000)

    assert len(packed.source_text) <= 6000
    # The rules and the publication text survive intact (not sliced off).
    assert "[CONSTRAINTS - INTERNAL" in packed.source_text
    assert "самостоятельный готовый пост" in packed.source_text
    assert "[UNTRUSTED SOURCE CONTENT" in packed.source_text
    assert "Петяярви" in packed.source_text


def test_publication_body_reaches_the_provider_request():
    body = "Конкретная деталь статьи: ночной паром из Хельсинки идёт 2 часа 30 минут. " * 8
    spec = _radar_spec("Короткое резюме.", body=body)
    packed = build_budgeted_provider_request(spec, limit=6000)
    assert "ночной паром из Хельсинки" in packed.source_text
    assert len(packed.source_text) <= 6000


def test_radar_rules_do_not_suggest_internal_wording_to_the_model():
    joined = " ".join(_radar_spec("x").constraints)
    assert "пункты в сигнале не перечислены" not in joined
    assert "живо и разговорно" in joined
    assert "личный опыт" in joined


def test_catalog_article_links_come_from_main_block_not_navigation_menu():
    # Live check 13.10 (Lonely Planet category page): the header menu's
    # "/destinations/in/europe" hubs were being picked up as articles.
    html = (
        '<html><body><nav><a href="/destinations/in/europe">Top Europe destinations list</a></nav>'
        "<main><p>" + "текст " * 30 + '</p><a href="/articles/how-to-pack-for-a-week-in-japan">'
        "How to pack for a week in Japan</a></main></body></html>"
    )
    links = _extract_html_full(html)[2]
    assert links == (("/articles/how-to-pack-for-a-week-in-japan", "How to pack for a week in Japan"),)
