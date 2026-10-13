"""Regression tests for the 13.10 "partner -> verified contact" incident.

What is REAL here: fetch.py wall detection, channel extraction, identity /
name checks, find_contact orchestration, contact_to_dict, the Telegram card
renderer. What is STUBBED: the HTTP fetch (a hand-written page modelled on the
real https://vershina-trip.ru/ checked read-only on the server), the LLM
(_FakeLLM replays fixed JSON) and the web search (_FakeSearchProvider).

Incident facts reproduced:
- vershina-trip.ru answered 200 with ~10k chars of text, a phone, Telegram,
  WhatsApp and a VK community, but was rejected as a "login wall / bot check"
  on `"captcha_signup": false` inside the site-builder config (11, 12, 13.10);
  the contact search therefore never read the site;
- every failure (blocked page, search error, unreadable model answer) ended in
  the same "person not found ... official channel" card;
- a person was attributed through the project NAME alone.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.domain.contacts import ContactType, SearchStatus
from app.domain.prospects import EvidenceSource, ProspectCandidate, ProspectFit, ProspectType
from app.handlers.partner_discovery import _render_contact_card
from app.planner.fetch import (
    FetchedPublicSource,
    PublicSourceFetchError,
    _extract_html_full,
    fetch_public_source_sync,
)
from app.services import contact_discovery
from app.services.contact_discovery import extract_channels, project_identity
from app.services.partner_outreach import contact_to_dict
from app.services.web_search.base import SearchResponse
from tests.test_contact_discovery import (
    _BrokenSearchProvider,
    _FakeLLM,
    _FakeSearchProvider,
    _patch_fetch,
    _result,
)
from tests.test_planner_fetch import _FakeResponse, _patch_open_returning, _patch_public_dns


def run(coro):
    return asyncio.run(coro)


SITE = "https://vershina-trip.ru/"


def _prospect(url=SITE, name="Вершина Трип") -> ProspectCandidate:
    return ProspectCandidate(
        name=name, url=url, prospect_type=ProspectType.TRAVEL_CLUB, query="q",
        evidence="авторские туры", evidence_source=EvidenceSource.PAGE,
        fit=ProspectFit.STRONG, rank_score=5,
    )


HOME_TEXT = (
    "Звоните, мы на связи +7 (977) 089-15-13\nВЕРШИНА ТРИП СОЗДАЕМ ПУТЕШЕСТВИЯ С ЛЮБОВЬЮ\n"
    "ЕКАТЕРИНА (владелец Вершины.Трип)\n"
    "Я счастлива, что вы здесь! Эта компания родилась из моей любви к миру.\n"
) + "Авторские групповые туры по России и миру. " * 20

HOME_LINKS = (
    ("tel:+79770891513", "+7 (977) 089-15-13"),
    ("https://t.me/vershina_vip", ""),
    ("https://vk.com/vershina.trip", ""),            # community: NOT a channel
    ("https://api.whatsapp.com/send/?phone=79770891513", ""),
    ("https://t.me/1", ""),                           # placeholder
    ("https://t.me/m/NmV-EiqCOTIy", ""),              # message permalink
    ("https://wa.me/79650536335", "Задать вопрос"),
    ("https://vk.com/im?media=&sel=-126562932", ""),
    ("https://vk.com/share.php?url=x", ""),
)


def _home(links=HOME_LINKS, text=HOME_TEXT) -> FetchedPublicSource:
    return FetchedPublicSource(
        url=SITE, final_url=SITE, title="Вершина Трип", text=text,
        content_type="text/html", links=links,
    )


# ── 1. the false "login wall" (root cause) ───────────────────────────────────

CAPTCHA_CONFIG = (
    '<script>window.__cfg={"site_options":{"captcha_signup":false,"captcha_orders":false}}</script>'
)


def test_rich_page_mentioning_captcha_in_config_is_content_not_a_wall(monkeypatch):
    _patch_public_dns(monkeypatch, hostname="vershina-trip.ru")
    body = (
        "<html><head><title>Вершина Трип</title>" + CAPTCHA_CONFIG + "</head><body><main><p>"
        + "Авторские групповые туры по России и миру, отзывы и ответы на вопросы. " * 40
        + '</p><a href="tel:+79770891513">+7 977 089-15-13</a></main></body></html>'
    ).encode("utf-8")
    _patch_open_returning(
        monkeypatch, _FakeResponse(headers={"Content-Type": "text/html; charset=utf-8"}, body=body),
    )

    page = fetch_public_source_sync("https://vershina-trip.ru/")

    assert "Авторские групповые туры" in page.text
    assert ("tel:+79770891513", "+7 977 089-15-13") in page.links


def test_thin_page_with_a_captcha_marker_is_still_a_wall(monkeypatch):
    _patch_public_dns(monkeypatch, hostname="vershina-trip.ru")
    body = (
        "<html><body><p>Подтвердите, что вы не робот.</p>"
        "<div class='g-recaptcha captcha'></div></body></html>"
    ).encode("utf-8")
    _patch_open_returning(
        monkeypatch, _FakeResponse(headers={"Content-Type": "text/html"}, body=body),
    )

    with pytest.raises(PublicSourceFetchError, match="login wall|bot"):
        fetch_public_source_sync("https://vershina-trip.ru/")


# ── 2. channels: confirmed, with source and ownership; a site is not one ─────

def test_channels_are_taken_from_the_page_with_source_and_project_ownership():
    channels = extract_channels(_home())
    by_kind = {}
    for ch in channels:
        by_kind.setdefault(ch.kind, []).append(ch)

    assert {c.value for c in by_kind["telegram"]} == {"https://t.me/vershina_vip"}
    assert {c.value for c in by_kind["whatsapp"]} == {"+79770891513", "+79650536335"}
    assert "+79770891513" in {c.value for c in by_kind["phone"]}
    assert any(c.kind == "vk" and "sel=-126562932" in c.value for c in channels)
    assert all(c.source_url == SITE for c in channels)
    # No person is named next to any of them -> project-level, not personal.
    assert {c.owner for c in channels} == {"project"}


def test_website_and_community_are_never_listed_as_channels():
    values = {c.value for c in extract_channels(_home())}
    assert "https://vk.com/vershina.trip" not in values
    assert SITE not in values
    assert "https://t.me/1" not in values            # placeholder handle
    assert not any("share.php" in v for v in values)


def test_channel_next_to_the_persons_name_is_marked_personal():
    page = _home(
        links=(("https://t.me/katya_travel", "Написать Екатерине"),
               ("https://t.me/vershina_vip", "")),
    )
    channels = extract_channels(page, person_name="Екатерина")
    owners = {c.value: c.owner for c in channels}
    assert owners["https://t.me/katya_travel"] == "person"
    assert owners["https://t.me/vershina_vip"] == "project"
    assert channels[0].owner == "person"             # personal first


def test_email_and_junk_filtering():
    page = FetchedPublicSource(
        url=SITE, final_url=SITE, title="t", content_type="text/html",
        text="Пишите: info@vershina-trip.ru. Картинка logo@2x.png, noreply@x.ru.",
        links=(("mailto:info@vershina-trip.ru?subject=hi", "Почта"),),
    )
    emails = [c.value for c in extract_channels(page) if c.kind == "email"]
    assert emails == ["info@vershina-trip.ru"]


# ── 3. find_contact: the site is read, the person has a source ───────────────

def test_owner_named_on_the_official_page_is_found_with_source_and_channels(monkeypatch):
    _patch_fetch(monkeypatch, _home())
    llm = _FakeLLM([json.dumps({
        "found": True, "display_name": "Екатерина Боронина", "role": "владелец",
        "evidence_text": "ЕКАТЕРИНА (владелец Вершины.Трип)",
    })])

    contact = run(contact_discovery.find_contact(
        _prospect(), llm_provider=llm, web_search_provider=_FakeSearchProvider({}),
    ))

    assert contact.contact_type == ContactType.PERSON
    assert contact.provenance == "official_page" and contact.evidence_url == SITE
    # The surname is NOT on the page - the model's addition is trimmed off.
    assert contact.display_name == "Екатерина"
    assert contact.search_status == SearchStatus.FOUND.value
    assert any(c.kind == "telegram" for c in contact.channels)
    assert all(c.owner == "project" for c in contact.channels)


def test_person_found_through_a_name_only_match_is_not_attributed(monkeypatch):
    # The page is unreachable; a search result names "Екатерина Боронина" as
    # owner of a project called "Вершина Трип" - but on an unrelated domain
    # with no handle of OUR project. Name alone is not identity.
    _patch_fetch(monkeypatch, None, raises=True)
    hit = _result(
        "Екатерина Боронина — владелец «Вершина Трип»", "https://other-city.example/about",
        "Екатерина Боронина, владелец туристической компании Вершина Трип, рассказала о бизнесе.",
    )
    llm = _FakeLLM([json.dumps({
        "found": True, "display_name": "Екатерина Боронина", "role": "владелец",
        "evidence_text": "Екатерина Боронина, владелец туристической компании Вершина Трип",
        "evidence_url": "https://other-city.example/about",
    })])

    contact = run(contact_discovery.find_contact(
        _prospect(), llm_provider=llm, web_search_provider=_FakeSearchProvider({"основатель": [hit]}),
    ))

    assert contact.contact_type == ContactType.PROJECT
    assert contact.display_name == "Вершина Трип"          # no foreign persona carried over


def test_search_evidence_carrying_the_projects_own_account_is_accepted(monkeypatch):
    # The project's own page links to vk.com/vershina.trip; a search result on
    # that account that names the owner IS tied to the project.
    _patch_fetch(monkeypatch, _home(text="Авторские туры. " * 40))   # page names nobody
    hit = _result(
        "Вершина Трип | VK", "https://vk.com/vershina.trip",
        "Вершина Трип. Владелец — Екатерина. Авторские туры.",
    )
    llm = _FakeLLM([
        json.dumps({"found": False}),                                   # page extraction
        json.dumps({
            "found": True, "display_name": "Екатерина", "role": "владелец",
            "evidence_text": "Владелец — Екатерина", "evidence_url": "https://vk.com/vershina.trip",
        }),
    ])

    contact = run(contact_discovery.find_contact(
        _prospect(), llm_provider=llm, web_search_provider=_FakeSearchProvider({"основатель": [hit]}),
    ))

    assert contact.contact_type == ContactType.PERSON
    assert contact.evidence_url == "https://vk.com/vershina.trip"
    assert contact.provenance == "targeted_search"
    assert project_identity(_prospect(), _home()) >= {"vershina.trip", "vershina_vip"}


# ── 4. "not found" vs "check did not finish" ─────────────────────────────────

def _status(monkeypatch, *, page, llm, search):
    if page is None:
        _patch_fetch(monkeypatch, None, raises=True)
    else:
        _patch_fetch(monkeypatch, page)
    return run(contact_discovery.find_contact(_prospect(), llm_provider=llm, web_search_provider=search))


def test_completed_search_with_nobody_is_not_found(monkeypatch):
    contact = _status(
        monkeypatch, page=_home(text="Авторские туры. " * 40),
        llm=_FakeLLM([json.dumps({"found": False}), json.dumps({"found": False})]),
        search=_FakeSearchProvider({}),
    )
    assert contact.contact_type == ContactType.PROJECT
    assert contact.search_status == SearchStatus.NOT_FOUND.value
    assert contact.incomplete_reasons == ()


def test_unreachable_project_page_is_incomplete_not_not_found(monkeypatch):
    contact = _status(
        monkeypatch, page=None,
        llm=_FakeLLM([json.dumps({"found": False})]), search=_FakeSearchProvider({}),
    )
    assert contact.search_status == SearchStatus.INCOMPLETE.value
    assert "page_unavailable" in contact.incomplete_reasons


def test_llm_failure_is_incomplete(monkeypatch):
    contact = _status(
        monkeypatch, page=_home(text="Авторские туры. " * 40),
        llm=_FakeLLM([RuntimeError("timeout"), RuntimeError("timeout")]),
        search=_FakeSearchProvider({}),
    )
    assert contact.search_status == SearchStatus.INCOMPLETE.value
    assert "llm_failed" in contact.incomplete_reasons


def test_unreadable_model_answer_is_incomplete(monkeypatch):
    contact = _status(
        monkeypatch, page=_home(text="Авторские туры. " * 40),
        llm=_FakeLLM(["это не JSON", "и это тоже"]), search=_FakeSearchProvider({}),
    )
    assert contact.search_status == SearchStatus.INCOMPLETE.value
    assert "llm_bad_answer" in contact.incomplete_reasons


def test_search_errors_are_incomplete(monkeypatch):
    broken = _status(
        monkeypatch, page=_home(text="Авторские туры. " * 40),
        llm=_FakeLLM([json.dumps({"found": False})]), search=_BrokenSearchProvider(),
    )

    class _NoneSearch:                      # WebSearchProvider contract: None on ANY error (429...)
        def search(self, *a, **k):
            return None

    returned_none = _status(
        monkeypatch, page=_home(text="Авторские туры. " * 40),
        llm=_FakeLLM([json.dumps({"found": False})]), search=_NoneSearch(),
    )
    for contact in (broken, returned_none):
        assert contact.search_status == SearchStatus.INCOMPLETE.value
        assert "search_failed" in contact.incomplete_reasons


def test_missing_search_or_llm_provider_is_incomplete(monkeypatch):
    no_search = _status(
        monkeypatch, page=_home(text="Авторские туры. " * 40),
        llm=_FakeLLM([json.dumps({"found": False})]), search=None,
    )
    assert "search_unavailable" in no_search.incomplete_reasons

    _patch_fetch(monkeypatch, None, raises=True)
    no_llm = run(contact_discovery.find_contact(
        _prospect(), llm_provider=None, web_search_provider=_FakeSearchProvider({}),
    ))
    assert no_llm.search_status == SearchStatus.INCOMPLETE.value
    assert "llm_unavailable" in no_llm.incomplete_reasons


# ── 5. what the user is shown ────────────────────────────────────────────────

def _project_dict(monkeypatch, **kw):
    return contact_to_dict(_status(monkeypatch, **kw))


def test_card_without_any_channel_says_channel_not_found_and_does_not_call_the_site_a_channel(monkeypatch):
    bare = _home(links=(), text="Авторские туры по России. " * 40)
    card = _render_contact_card(_project_dict(
        monkeypatch, page=bare,
        llm=_FakeLLM([json.dumps({"found": False}), json.dumps({"found": False})]),
        search=_FakeSearchProvider({}),
    ))
    assert "Канал связи не найден" in card
    assert "официальный канал" not in card
    assert "Это страница проекта, а не канал связи" in card
    assert "Проверка завершена" in card


def test_incomplete_card_does_not_claim_there_is_nobody(monkeypatch):
    card = _render_contact_card(_project_dict(
        monkeypatch, page=None, llm=_FakeLLM([json.dumps({"found": False})]),
        search=_FakeSearchProvider({}),
    ))
    assert "Проверка не завершена" in card and "страница проекта не открылась" in card
    assert "Это не значит, что контакта нет" in card


def test_card_lists_project_channels_with_their_source(monkeypatch):
    card = _render_contact_card(_project_dict(
        monkeypatch, page=_home(),
        llm=_FakeLLM([json.dumps({"found": False}), json.dumps({"found": False})]),
        search=_FakeSearchProvider({}),
    ))
    assert "Общие контакты проекта" in card
    assert "Telegram: https://t.me/vershina_vip (источник: https://vershina-trip.ru/)" in card
    assert "Личные контакты" not in card


def test_person_card_shows_the_source_of_the_link_and_a_missing_personal_channel(monkeypatch):
    _patch_fetch(monkeypatch, _home())
    llm = _FakeLLM([json.dumps({
        "found": True, "display_name": "Екатерина", "role": "владелец",
        "evidence_text": "ЕКАТЕРИНА (владелец Вершины.Трип)",
    })])
    contact = run(contact_discovery.find_contact(
        _prospect(), llm_provider=llm, web_search_provider=_FakeSearchProvider({}),
    ))
    card = _render_contact_card(contact_to_dict(contact))
    assert "Источник связи с проектом (официальный сайт проекта):" in card
    assert "https://vershina-trip.ru/" in card
    assert "Личный контакт этого человека не найден." in card


def test_contact_dict_carries_status_channels_and_reasons(monkeypatch):
    data = _project_dict(
        monkeypatch, page=None, llm=_FakeLLM([json.dumps({"found": False})]),
        search=_FakeSearchProvider({}),
    )
    assert data["search_status"] == "incomplete"
    assert data["incomplete_reasons"] == ["page_unavailable"]
    assert data["channels"] == []


def test_extract_html_full_collects_contact_links():
    html = (
        '<html><body><main><p>' + "текст " * 30 + '</p><a href="mailto:info@x.ru">Почта</a>'
        '<a href="tel:+79000000000">Звонок</a></main></body></html>'
    )
    links = _extract_html_full(html)[2]
    assert ("mailto:info@x.ru", "Почта") in links and ("tel:+79000000000", "Звонок") in links


def test_service_vk_links_do_not_become_project_identity():
    # Live check 13.10: the site links vk.com/club1 (VK's own page); "club1"
    # must not tie unrelated search results to the project.
    page = _home(links=HOME_LINKS + (("https://vk.com/club1", ""), ("https://vk.com/id5", "")))
    identity = project_identity(_prospect(), page)
    assert "club1" not in identity and "id5" not in identity
    assert {"vershina-trip.ru", "vershina.trip", "vershina_vip"} <= identity
