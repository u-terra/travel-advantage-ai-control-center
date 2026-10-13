"""Focused tests for app.services.contact_discovery.find_contact - the
Partner Discovery v0 contact-discovery layer (core only, no Telegram UI).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import fields

from app.domain.contacts import ContactCandidate, ContactConfidence, ContactType
from app.domain.prospects import EvidenceSource, ProspectCandidate, ProspectFit, ProspectType
from app.planner.fetch import FetchedPublicSource, PublicSourceFetchError
from app.services import contact_discovery
from app.services.web_search.base import SearchResponse, SearchResult


def run(coro):
    return asyncio.run(coro)


def _prospect(url="https://example-club.ru/", name="Example Club") -> ProspectCandidate:
    return ProspectCandidate(
        name=name,
        url=url,
        prospect_type=ProspectType.TRAVEL_CLUB,
        query="пример запроса",
        evidence="пример evidence",
        evidence_source=EvidenceSource.PAGE,
        fit=ProspectFit.STRONG,
        rank_score=10,
    )


def _community_prospect(url="https://magput.ru/tours/gildiya-gidov-magput") -> ProspectCandidate:
    return ProspectCandidate(
        name="Гильдия гидов Magput",
        url=url,
        prospect_type=ProspectType.TRAVEL_CLUB,
        query="пример запроса",
        evidence="более 1000 экскурсоводов объединены в гильдию",
        evidence_source=EvidenceSource.PAGE,
        fit=ProspectFit.STRONG,
        rank_score=10,
    )


def _page(text: str, final_url: str | None = None) -> FetchedPublicSource:
    return FetchedPublicSource(
        url="https://example-club.ru/",
        final_url=final_url or "https://example-club.ru/",
        title="Example Club",
        text=text,
        content_type="text/html",
    )


class _FakeChatResult:
    def __init__(self, text: str):
        self.text = text


class _FakeConfig:
    api_key = "fake-key"


class _FakeLLM:
    """Replays a fixed sequence of response texts, one per call, in order.
    An entry that is an Exception instance is raised instead."""

    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls: list[str] = []
        self.config = _FakeConfig()

    def generate(self, *, message: str):
        self.calls.append(message)
        if not self._responses:
            raise AssertionError("no more fake LLM responses configured")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeChatResult(item)


def _result(title, url, snippet):
    return SearchResult(
        title=title, url=url, snippet=snippet, domain=url.split("/")[2],
        published_at=None, provider="yandex", rank=1,
    )


class _FakeSearchProvider:
    """Replays one fixed response list per query substring match (checked
    via the role word in the query), no network. Tracks every query asked."""

    def __init__(self, by_role: dict[str, list[SearchResult]]):
        self._by_role = by_role
        self.queries: list[str] = []

    def search(self, query, *, site=None, limit=5, search_type=None,
                allow_exceeding_configured_max=False):
        self.queries.append(query)
        for role, results in self._by_role.items():
            if role in query:
                return SearchResponse(query=query, results=results, provider="yandex")
        return SearchResponse(query=query, results=[], provider="yandex")


class _BrokenSearchProvider:
    def search(self, *args, **kwargs):
        raise RuntimeError("network down")


def _patch_fetch(monkeypatch, page: FetchedPublicSource | None, *, raises: bool = False):
    def _fake_fetch(url: str) -> FetchedPublicSource:
        if raises or page is None:
            raise PublicSourceFetchError("boom")
        return page

    monkeypatch.setattr(contact_discovery, "fetch_public_source_sync", _fake_fetch)


# ── A/B: official page extraction ────────────────────────────────────────────

def test_official_page_explicit_person_and_role_is_high(monkeypatch):
    prospect = _prospect()
    page = _page("Иван Иванов - основатель и руководитель проекта Example Club.")
    _patch_fetch(monkeypatch, page)
    llm = _FakeLLM([
        json.dumps({
            "found": True,
            "display_name": "Иван Иванов",
            "role": "основатель и руководитель",
            "evidence_text": "Иван Иванов - основатель и руководитель проекта Example Club.",
        }),
    ])

    contact = run(contact_discovery.find_contact(
        prospect, llm_provider=llm, web_search_provider=None,
    ))

    assert contact.contact_type == ContactType.PERSON
    assert contact.display_name == "Иван Иванов"
    assert contact.confidence == ContactConfidence.HIGH
    assert len(llm.calls) == 1  # no search-validation call needed


def test_official_page_high_confidence_skips_web_search(monkeypatch):
    prospect = _prospect()
    page = _page("Иван Иванов - основатель Example Club.")
    _patch_fetch(monkeypatch, page)
    llm = _FakeLLM([
        json.dumps({
            "found": True, "display_name": "Иван Иванов", "role": "основатель",
            "evidence_text": "Иван Иванов - основатель Example Club.",
        }),
    ])
    search_provider = _FakeSearchProvider({})

    run(contact_discovery.find_contact(
        prospect, llm_provider=llm, web_search_provider=search_provider,
    ))

    assert search_provider.queries == []


def test_page_mentions_person_but_no_role_is_rejected(monkeypatch):
    prospect = _prospect()
    page = _page("Иван Иванов много путешествовал в этом году.")
    _patch_fetch(monkeypatch, page)
    llm = _FakeLLM([
        json.dumps({"found": False}),
        json.dumps({"found": False}),
    ])
    search_provider = _FakeSearchProvider({})

    contact = run(contact_discovery.find_contact(
        prospect, llm_provider=llm, web_search_provider=search_provider,
    ))

    assert contact.contact_type == ContactType.PROJECT


def test_page_has_role_but_no_identifiable_person_is_rejected(monkeypatch):
    prospect = _prospect()
    page = _page("У проекта есть руководитель, который отвечает за всё.")
    _patch_fetch(monkeypatch, page)
    llm = _FakeLLM([
        json.dumps({"found": False}),
        json.dumps({"found": False}),
    ])
    search_provider = _FakeSearchProvider({})

    contact = run(contact_discovery.find_contact(
        prospect, llm_provider=llm, web_search_provider=search_provider,
    ))

    assert contact.contact_type == ContactType.PROJECT


# ── C/D: targeted search fallback ───────────────────────────────────────────

def test_no_person_on_page_triggers_targeted_search(monkeypatch):
    prospect = _prospect()
    page = _page("Общая информация о проекте без упоминания людей.")
    _patch_fetch(monkeypatch, page)
    llm = _FakeLLM([
        json.dumps({"found": False}),
        json.dumps({"found": False}),
    ])
    search_provider = _FakeSearchProvider({})

    run(contact_discovery.find_contact(
        prospect, llm_provider=llm, web_search_provider=search_provider,
    ))

    assert len(search_provider.queries) == 3
    assert all(prospect.name in q for q in search_provider.queries)


def test_targeted_results_explicit_person_role_project_is_medium(monkeypatch):
    prospect = _prospect()
    _patch_fetch(monkeypatch, None, raises=True)
    hit = _result(
        "Пётр Петров - основатель Example Club",
        "https://news.example/article",
        "Пётр Петров, основатель Example Club (example-club.ru), рассказал о проекте.",
    )
    llm = _FakeLLM([
        json.dumps({
            "found": True, "display_name": "Пётр Петров", "role": "основатель",
            "evidence_text": "Пётр Петров, основатель Example Club (example-club.ru), рассказал о проекте.",
            "evidence_url": "https://news.example/article",
        }),
    ])
    search_provider = _FakeSearchProvider({"основатель": [hit]})

    contact = run(contact_discovery.find_contact(
        prospect, llm_provider=llm, web_search_provider=search_provider,
    ))

    assert contact.contact_type == ContactType.PERSON
    assert contact.confidence == ContactConfidence.MEDIUM
    assert contact.evidence_url == "https://news.example/article"


def test_unrelated_names_in_snippets_are_rejected(monkeypatch):
    prospect = _prospect()
    _patch_fetch(monkeypatch, None, raises=True)
    hit = _result(
        "Новости туризма", "https://news.example/unrelated",
        "Сидор Сидоров рассказал про совершенно другой проект.",
    )
    llm = _FakeLLM([
        json.dumps({"found": False}),
        json.dumps({"found": False}),
    ])
    search_provider = _FakeSearchProvider({"основатель": [hit]})

    contact = run(contact_discovery.find_contact(
        prospect, llm_provider=llm, web_search_provider=search_provider,
    ))

    assert contact.contact_type == ContactType.PROJECT


def test_ambiguous_conflicting_people_are_rejected(monkeypatch):
    prospect = _prospect()
    _patch_fetch(monkeypatch, None, raises=True)
    hit_a = _result("Статья 1", "https://news.example/a", "Иван Иванов, основатель Example Club.")
    hit_b = _result("Статья 2", "https://news.example/b", "Пётр Петров, руководитель Example Club.")
    llm = _FakeLLM([
        json.dumps({"found": False}),
        json.dumps({"found": False}),  # LLM itself judges this conflicting -> not found
    ])
    search_provider = _FakeSearchProvider({"основатель": [hit_a], "руководитель": [hit_b]})

    contact = run(contact_discovery.find_contact(
        prospect, llm_provider=llm, web_search_provider=search_provider,
    ))

    assert contact.contact_type == ContactType.PROJECT


# ── E: professional-contact fallback for community/directory resources ──────

def test_community_prospect_runs_professional_search_after_owner_failure(monkeypatch):
    """Magput-like case: owner-contact flow (A-D) finds nobody, prospect
    self-describes as a guild ("Гильдия гидов Magput") - a second,
    separate search pass must run with professional (not owner) role
    words."""
    prospect = _community_prospect()
    page = _page("Общая информация о гильдии без упоминания конкретных людей.")
    _patch_fetch(monkeypatch, page)
    llm = _FakeLLM([
        json.dumps({"found": False}),  # step B
        json.dumps({"found": False}),  # step D (owner search)
        json.dumps({"found": False}),  # step E (professional search)
    ])
    search_provider = _FakeSearchProvider({})

    run(contact_discovery.find_contact(
        prospect, llm_provider=llm, web_search_provider=search_provider,
    ))

    # Match on the trailing role word, not substring - the project's own
    # name ("Гильдия гидов Magput") happens to contain "гид" too.
    professional_queries = [
        q for q in search_provider.queries
        if q.endswith(" гид") or q.endswith(" экскурсовод") or q.endswith(" автор тура")
    ]
    assert len(professional_queries) == 3
    assert all(prospect.name in q for q in professional_queries)
    # Owner queries (step C) still ran first, unchanged.
    assert any(q.endswith(" основатель") for q in search_provider.queries)


def test_ordinary_business_does_not_run_professional_search(monkeypatch):
    """Quinta-like case: an ordinary single-owner business (no community/
    directory marker in name/evidence/url) must NOT trigger step E, even
    after owner-contact flow fails."""
    prospect = _prospect(name="Quinta", url="https://www.quinta.ru/")
    page = _page("Общая информация о туроператоре без упоминания людей.")
    _patch_fetch(monkeypatch, page)
    llm = _FakeLLM([
        json.dumps({"found": False}),  # step B
        json.dumps({"found": False}),  # step D (owner search)
    ])
    search_provider = _FakeSearchProvider({})

    contact = run(contact_discovery.find_contact(
        prospect, llm_provider=llm, web_search_provider=search_provider,
    ))

    assert contact.contact_type == ContactType.PROJECT
    # Exactly the 3 owner queries - no professional-role queries at all.
    assert len(search_provider.queries) == 3
    assert not any("гид" in q or "экскурсовод" in q for q in search_provider.queries)


def test_guide_with_name_role_and_project_relation_in_one_result_is_accepted(monkeypatch):
    prospect = _community_prospect()
    _patch_fetch(monkeypatch, None, raises=True)
    hit = _result(
        "Профиль гида",
        "https://magput.ru/?viewprog=63638",
        "Фильченков Александр Михайлович - гид Гильдии гидов Magput. Путешествие, как искусство.",
    )
    llm = _FakeLLM([
        json.dumps({"found": False}),  # step D (owner search, no results configured)
        json.dumps({
            "found": True,
            "display_name": "Фильченков Александр Михайлович",
            "role": "гид",
            "evidence_text": "Фильченков Александр Михайлович - гид Гильдии гидов Magput.",
            "evidence_url": "https://magput.ru/?viewprog=63638",
        }),
    ])
    search_provider = _FakeSearchProvider({"гид": [hit]})

    contact = run(contact_discovery.find_contact(
        prospect, llm_provider=llm, web_search_provider=search_provider,
    ))

    assert contact.contact_type == ContactType.PERSON
    assert contact.display_name == "Фильченков Александр Михайлович"
    assert contact.role == "гид"
    assert contact.confidence == ContactConfidence.MEDIUM
    assert contact.evidence_url == "https://magput.ru/?viewprog=63638"


def test_professional_person_without_role_is_rejected(monkeypatch):
    prospect = _community_prospect()
    _patch_fetch(monkeypatch, None, raises=True)
    hit = _result(
        "Упоминание", "https://news.example/a", "Иван Иванов много путешествовал с гильдией.",
    )
    llm = _FakeLLM([
        json.dumps({"found": False}),
        json.dumps({"found": False}),
    ])
    search_provider = _FakeSearchProvider({"гид": [hit]})

    contact = run(contact_discovery.find_contact(
        prospect, llm_provider=llm, web_search_provider=search_provider,
    ))

    assert contact.contact_type == ContactType.PROJECT


def test_professional_role_without_name_is_rejected(monkeypatch):
    prospect = _community_prospect()
    _patch_fetch(monkeypatch, None, raises=True)
    hit = _result(
        "Упоминание", "https://news.example/a", "У гильдии много опытных гидов.",
    )
    llm = _FakeLLM([
        json.dumps({"found": False}),
        json.dumps({"found": False}),
    ])
    search_provider = _FakeSearchProvider({"гид": [hit]})

    contact = run(contact_discovery.find_contact(
        prospect, llm_provider=llm, web_search_provider=search_provider,
    ))

    assert contact.contact_type == ContactType.PROJECT


def test_professional_person_from_unrelated_project_is_rejected(monkeypatch):
    """Even with a plausible name+role pair, no stated connection to THIS
    resource must still be rejected - the LLM is instructed accordingly,
    and this pins the fail-closed outcome."""
    prospect = _community_prospect()
    _patch_fetch(monkeypatch, None, raises=True)
    hit = _result(
        "Другой проект", "https://news.example/other",
        "Иван Иванов - гид совершенно другого туристического клуба.",
    )
    llm = _FakeLLM([
        json.dumps({"found": False}),
        json.dumps({"found": False}),  # LLM itself judges no relation to Magput
    ])
    search_provider = _FakeSearchProvider({"гид": [hit]})

    contact = run(contact_discovery.find_contact(
        prospect, llm_provider=llm, web_search_provider=search_provider,
    ))

    assert contact.contact_type == ContactType.PROJECT


def test_professional_search_never_exceeds_three_queries(monkeypatch):
    prospect = _community_prospect()
    page = _page("Общая информация о гильдии без упоминания конкретных людей.")
    _patch_fetch(monkeypatch, page)
    llm = _FakeLLM([
        json.dumps({"found": False}),
        json.dumps({"found": False}),
        json.dumps({"found": False}),
    ])
    search_provider = _FakeSearchProvider({})

    run(contact_discovery.find_contact(
        prospect, llm_provider=llm, web_search_provider=search_provider,
    ))

    professional_queries = [
        q for q in search_provider.queries
        if q.endswith(" гид") or q.endswith(" экскурсовод") or q.endswith(" автор тура")
    ]
    assert len(professional_queries) <= 3


def test_professional_contact_card_has_no_phone_or_email(monkeypatch):
    """Same privacy guard as the owner flow - exercised end-to-end for a
    professional-contact result, not just the dataclass shape."""
    prospect = _community_prospect()
    _patch_fetch(monkeypatch, None, raises=True)
    hit = _result(
        "Профиль гида",
        "https://magput.ru/?viewprog=63638",
        "Фильченков Александр Михайлович - гид Гильдии гидов Magput.",
    )
    llm = _FakeLLM([
        json.dumps({"found": False}),
        json.dumps({
            "found": True,
            "display_name": "Фильченков Александр Михайлович",
            "role": "гид",
            "evidence_text": "Фильченков Александр Михайлович - гид Гильдии гидов Magput.",
            "evidence_url": "https://magput.ru/?viewprog=63638",
        }),
    ])
    search_provider = _FakeSearchProvider({"гид": [hit]})

    contact = run(contact_discovery.find_contact(
        prospect, llm_provider=llm, web_search_provider=search_provider,
    ))

    for field_value in (contact.display_name, contact.role, contact.evidence_text, contact.evidence_url):
        assert "@" not in (field_value or "")
        assert "+7" not in (field_value or "")


def test_existing_owner_flow_unaffected_by_professional_fallback(monkeypatch):
    """A community-looking prospect that DOES have a confident owner
    contact on its own page must still return that HIGH-confidence PERSON
    result, never reaching step E at all."""
    prospect = _community_prospect()
    page = _page("Иван Иванов - основатель Гильдии гидов Magput.")
    _patch_fetch(monkeypatch, page)
    llm = _FakeLLM([
        json.dumps({
            "found": True, "display_name": "Иван Иванов", "role": "основатель",
            "evidence_text": "Иван Иванов - основатель Гильдии гидов Magput.",
        }),
    ])
    search_provider = _FakeSearchProvider({})

    contact = run(contact_discovery.find_contact(
        prospect, llm_provider=llm, web_search_provider=search_provider,
    ))

    assert contact.contact_type == ContactType.PERSON
    assert contact.confidence == ContactConfidence.HIGH
    assert search_provider.queries == []


# ── URL normalization (evidence_url / search-result URL hygiene) ───────────

def test_amp_artifact_is_stripped_from_result_url():
    """Real production artifact: a search result URL for an AMP-enabled
    page came back as ".../gildiya-gidov-magput?amp&amp" - a dangling,
    valueless "amp" query token, not a real parameter."""
    dirty = "https://magput.ru/tours/gildiya-gidov-magput?amp&amp"

    assert contact_discovery._normalize_result_url(dirty) == (
        "https://magput.ru/tours/gildiya-gidov-magput"
    )


def test_normal_query_parameters_are_not_touched():
    """A real parameter that merely contains the substring "amp" (e.g. a
    utm tag) must survive untouched - only the exact, valueless "amp"
    artifact token is ever stripped."""
    clean = "https://example.com/page?utm_source=amp&id=5"

    assert contact_discovery._normalize_result_url(clean) == clean


def test_url_with_no_query_is_unchanged():
    url = "https://magput.ru/tours/gildiya-gidov-magput"

    assert contact_discovery._normalize_result_url(url) == url


def test_html_entity_ampersand_is_decoded():
    dirty = "https://example.com/page?a=1&amp;b=2"

    assert contact_discovery._normalize_result_url(dirty) == "https://example.com/page?a=1&b=2"


def test_evidence_url_from_dirty_search_result_is_normalized_end_to_end(monkeypatch):
    """The normalization must actually reach the final ContactCandidate,
    not just the standalone helper - the fake search provider returns the
    exact dirty production URL, and the LLM echoes it back verbatim (as a
    real LLM would, copying the URL it was shown) - the membership check
    against already-normalized results must still accept it, and the
    final evidence_url must be clean."""
    prospect = _community_prospect()
    _patch_fetch(monkeypatch, None, raises=True)
    dirty_url = "https://magput.ru/tours/gildiya-gidov-magput?amp&amp"
    clean_url = "https://magput.ru/tours/gildiya-gidov-magput"
    hit = _result(
        "Гильдия гидов Magput", dirty_url,
        "Ярмоленко Людмила Борисовна, экскурсовод Гильдии гидов Magput.",
    )
    llm = _FakeLLM([
        json.dumps({"found": False}),  # step D (owner search)
        json.dumps({
            "found": True,
            "display_name": "Ярмоленко Людмила Борисовна",
            "role": "экскурсовод",
            "evidence_text": "Ярмоленко Людмила Борисовна, экскурсовод Гильдии гидов Magput.",
            "evidence_url": clean_url,  # LLM copies the URL as shown to it - already normalized
        }),
    ])
    search_provider = _FakeSearchProvider({"гид": [hit]})

    contact = run(contact_discovery.find_contact(
        prospect, llm_provider=llm, web_search_provider=search_provider,
    ))

    assert contact.contact_type == ContactType.PERSON
    assert contact.evidence_url == clean_url
    assert "amp&amp" not in contact.evidence_url


# ── Safe fallback on failure ─────────────────────────────────────────────────

def test_llm_exception_falls_back_to_project(monkeypatch):
    prospect = _prospect()
    page = _page("Иван Иванов - основатель Example Club.")
    _patch_fetch(monkeypatch, page)
    llm = _FakeLLM([RuntimeError("boom"), RuntimeError("boom")])
    search_provider = _FakeSearchProvider({})

    contact = run(contact_discovery.find_contact(
        prospect, llm_provider=llm, web_search_provider=search_provider,
    ))

    assert contact.contact_type == ContactType.PROJECT
    assert contact.public_url == prospect.url


def test_llm_invalid_json_falls_back_to_project(monkeypatch):
    prospect = _prospect()
    page = _page("Иван Иванов - основатель Example Club.")
    _patch_fetch(monkeypatch, page)
    llm = _FakeLLM(["не json ответ", "ещё не json"])
    search_provider = _FakeSearchProvider({})

    contact = run(contact_discovery.find_contact(
        prospect, llm_provider=llm, web_search_provider=search_provider,
    ))

    assert contact.contact_type == ContactType.PROJECT


def test_fetch_failure_falls_back_safely(monkeypatch):
    prospect = _prospect()
    _patch_fetch(monkeypatch, None, raises=True)
    llm = _FakeLLM([json.dumps({"found": False})])
    search_provider = _FakeSearchProvider({})

    contact = run(contact_discovery.find_contact(
        prospect, llm_provider=llm, web_search_provider=search_provider,
    ))

    assert contact.contact_type == ContactType.PROJECT


def test_search_failure_falls_back_safely(monkeypatch):
    prospect = _prospect()
    page = _page("Нет упоминаний людей на странице.")
    _patch_fetch(monkeypatch, page)
    llm = _FakeLLM([json.dumps({"found": False})])

    contact = run(contact_discovery.find_contact(
        prospect, llm_provider=llm, web_search_provider=_BrokenSearchProvider(),
    ))

    assert contact.contact_type == ContactType.PROJECT


def test_no_llm_provider_falls_back_safely(monkeypatch):
    prospect = _prospect()

    contact = run(contact_discovery.find_contact(
        prospect, llm_provider=None, web_search_provider=None,
    ))

    assert contact.contact_type == ContactType.PROJECT


# ── Privacy / safety ─────────────────────────────────────────────────────────

def test_contact_candidate_has_no_phone_or_email_fields():
    field_names = {f.name for f in fields(ContactCandidate)}
    assert "phone" not in field_names
    assert "email" not in field_names


def test_targeted_search_never_exceeds_three_queries(monkeypatch):
    prospect = _prospect()
    page = _page("Нет упоминаний людей на странице.")
    _patch_fetch(monkeypatch, page)
    llm = _FakeLLM([json.dumps({"found": False}), json.dumps({"found": False})])
    search_provider = _FakeSearchProvider({})

    run(contact_discovery.find_contact(
        prospect, llm_provider=llm, web_search_provider=search_provider,
    ))

    assert len(search_provider.queries) <= 3
