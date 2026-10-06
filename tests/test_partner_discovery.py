"""Regression fixtures for PartnerDiscoveryService built directly from the
Partner Discovery v0 live Yandex probe (2026-10-06): real STRONG/noise
classifications from that run, replayed here as fake SearchResult hits so
noise filtering/dedup/ranking never silently regress."""

from __future__ import annotations

import pytest

from app.domain.prospects import EvidenceSource, ProspectFit, ProspectType
from app.planner.fetch import PublicSourceFetchError
from app.services.partner_discovery import PartnerDiscoveryService
from app.services.web_search.base import SearchResponse, SearchResult


def _result(title, url, snippet, domain=None, rank=1):
    return SearchResult(
        title=title,
        url=url,
        snippet=snippet,
        domain=domain or url.split("/")[2],
        published_at=None,
        provider="yandex",
        rank=rank,
    )


class _FakeProvider:
    """Replays one fixed response per query, keyed by query text; no network."""

    def __init__(self, by_query: dict[str, list[SearchResult]]):
        self._by_query = by_query
        self.calls: list[str] = []

    def search(self, query, *, site=None, limit=5, search_type=None,
               allow_exceeding_configured_max=False):
        self.calls.append(query)
        results = self._by_query.get(query, [])
        return SearchResponse(query=query, results=results, provider="yandex")


# ── Live-probe fixtures ────────────────────────────────────────────────────

STRONG_MAGPUT_1 = _result(
    "Авторские туры - Магазин Путешествий",
    "https://magput.ru/tours/avtorskie-tury-magput",
    "Авторские туры от турагентства Магазин Путешествий, групповые туры с гидом",
)
STRONG_MAGPUT_2 = _result(
    "Гильдия гидов - Магазин Путешествий",
    "https://magput.ru/tours/gildiya-gidov-magput",
    "Наши гиды и экскурсоводы, авторские туры",
)
STRONG_AVENTURAMANIA = _result(
    "Авентурамания - туроператор",
    "https://aventuramania.ru/",
    "Лицензированный туристический оператор, авторские туры, в реестре туроператоров",
)
STRONG_KOVINOV = _result(
    "Дмитрий Ковинов - путешественник",
    "https://www.kovinov.com/",
    "Блог о путешествиях, 230+ туров, тревел-блог",
)
STRONG_CLUB_MIRY = _result(
    "Клуб «Миры»",
    "https://club-miry.ru/",
    "Клуб путешественников с 20-летней историей, сообщество путешественников",
)

NOISE_YOUTRAVEL = _result(
    "Youtravel.me - подбор туров",
    "https://youtravel.me/search?q=авторские+туры",
    "Более 500 туроператоров и турагентов на одной платформе",
    domain="youtravel.me",
)
NOISE_TRIPSTER = _result(
    "Tripster - экскурсии от местных гидов",
    "https://experience.tripster.ru/experience/search?q=гид",
    "Тысячи гидов и экскурсоводов, выбирайте лучшего",
    domain="tripster.ru",
)
NOISE_AVITO = _result(
    "Авито Услуги",
    "https://www.avito.ru/moskva/predlozheniya_uslug?q=турагент",
    "Объявления от турагентов и организаторов путешествий",
    domain="avito.ru",
)
NOISE_USLUGI_YANDEX = _result(
    "Яндекс Услуги - гиды",
    "https://uslugi.yandex.ru/213-moscow/category?text=гид",
    "Каталог гидов и экскурсоводов в вашем городе",
    domain="uslugi.yandex.ru",
)
NOISE_YANDEX_JOURNAL = _result(
    "Подборка трэвел-блогеров",
    "https://travel.yandex.ru/journal/podborka-trevel-blogerov",
    "Собрали лучших трэвел-блогеров рунета",
    domain="travel.yandex.ru",
)
NOISE_MD_TRAVEL_CLUB = _result(
    "Клуб Путешественников",
    "https://vk.ru/mdtravelclub",
    "Экскурсии для участников программы Московского долголетия",
    domain="vk.ru",
)
NOISE_MLM_CLUB = _result(
    "Клуб Путешественников Ближе к Солнцу",
    "https://k-solncy.ru/klub",
    "60% комиссии с каждого тура, единоразовый взнос, пассивный доход, реферальная система",
    domain="k-solncy.ru",
)


def _service(by_query, verify_ok=False):
    provider = _FakeProvider(by_query)
    service = PartnerDiscoveryService(provider)
    if not verify_ok:
        async def _fail_verify(self, result):
            return (result.snippet or "")[:400], EvidenceSource.SNIPPET
        service._verify = _fail_verify.__get__(service, PartnerDiscoveryService)
    return service, provider


@pytest.mark.asyncio
async def test_dedupes_same_domain_different_subject_pages_kept_separate():
    """magput.ru's two distinct subject pages must NOT collapse into one -
    canonical_domain() alone would merge them; the service must not."""
    by_query = {
        "турагент авторские туры": [STRONG_MAGPUT_1],
        "гид экскурсии путешествия": [STRONG_MAGPUT_2],
    }
    service, _ = _service(by_query)
    candidates = await service.find_candidates()
    urls = {c.url for c in candidates}
    assert STRONG_MAGPUT_1.url in urls
    assert STRONG_MAGPUT_2.url in urls


@pytest.mark.asyncio
async def test_exact_duplicate_url_deduped_once():
    by_query = {
        "турагент авторские туры": [STRONG_AVENTURAMANIA],
        "организатор путешествий группа": [STRONG_AVENTURAMANIA],
    }
    service, _ = _service(by_query)
    candidates = await service.find_candidates()
    assert sum(1 for c in candidates if c.url == STRONG_AVENTURAMANIA.url) == 1


@pytest.mark.asyncio
async def test_noise_domains_filtered_out():
    by_query = {
        "турагент авторские туры": [NOISE_YOUTRAVEL, NOISE_AVITO, STRONG_AVENTURAMANIA],
        "гид экскурсии путешествия": [NOISE_TRIPSTER, NOISE_USLUGI_YANDEX],
        "travel блог автор путешествий": [NOISE_YANDEX_JOURNAL],
    }
    service, _ = _service(by_query)
    candidates = await service.find_candidates()
    urls = {c.url for c in candidates}
    for noisy in (NOISE_YOUTRAVEL, NOISE_AVITO, NOISE_TRIPSTER,
                  NOISE_USLUGI_YANDEX, NOISE_YANDEX_JOURNAL):
        assert noisy.url not in urls
    assert STRONG_AVENTURAMANIA.url in urls


@pytest.mark.asyncio
async def test_government_program_marker_filtered_out():
    by_query = {"клуб путешественников сообщество": [NOISE_MD_TRAVEL_CLUB, STRONG_CLUB_MIRY]}
    service, _ = _service(by_query)
    candidates = await service.find_candidates()
    urls = {c.url for c in candidates}
    assert NOISE_MD_TRAVEL_CLUB.url not in urls
    assert STRONG_CLUB_MIRY.url in urls


@pytest.mark.asyncio
async def test_mlm_referral_marker_filtered_out():
    by_query = {"клуб путешественников сообщество": [NOISE_MLM_CLUB, STRONG_CLUB_MIRY]}
    service, _ = _service(by_query)
    candidates = await service.find_candidates()
    urls = {c.url for c in candidates}
    assert NOISE_MLM_CLUB.url not in urls
    assert STRONG_CLUB_MIRY.url in urls


@pytest.mark.asyncio
async def test_strong_candidates_ranked_and_typed_correctly():
    by_query = {
        "турагент авторские туры": [STRONG_AVENTURAMANIA],
        "travel блог автор путешествий": [STRONG_KOVINOV],
        "клуб путешественников сообщество": [STRONG_CLUB_MIRY],
    }
    service, _ = _service(by_query)
    candidates = await service.find_candidates()
    by_url = {c.url: c for c in candidates}

    aventuramania = by_url[STRONG_AVENTURAMANIA.url]
    assert aventuramania.prospect_type == ProspectType.TRAVEL_AGENT
    assert aventuramania.fit == ProspectFit.STRONG

    kovinov = by_url[STRONG_KOVINOV.url]
    assert kovinov.prospect_type == ProspectType.TRAVEL_CREATOR
    assert kovinov.fit == ProspectFit.STRONG

    club_miry = by_url[STRONG_CLUB_MIRY.url]
    assert club_miry.prospect_type == ProspectType.TRAVEL_CLUB
    assert club_miry.fit == ProspectFit.STRONG


@pytest.mark.asyncio
async def test_capped_at_five_results():
    results = [
        _result(f"Турагент {i}", f"https://example{i}.ru/", "Авторские туры, турагент")
        for i in range(8)
    ]
    by_query = {"турагент авторские туры": results}
    service, _ = _service(by_query)
    candidates = await service.find_candidates()
    assert len(candidates) <= 5


@pytest.mark.asyncio
async def test_queries_are_fixed_and_bounded():
    from app.services.partner_discovery import DISCOVERY_QUERIES

    service, provider = _service({})
    await service.find_candidates()
    assert provider.calls == list(DISCOVERY_QUERIES)


@pytest.mark.asyncio
async def test_search_provider_error_is_tolerated():
    class _ErrorProvider:
        def search(self, query, **kwargs):
            raise RuntimeError("network down")

    service = PartnerDiscoveryService(_ErrorProvider())
    candidates = await service.find_candidates()
    assert candidates == []


@pytest.mark.asyncio
async def test_page_verification_failure_falls_back_to_snippet_evidence():
    provider = _FakeProvider({"турагент авторские туры": [STRONG_AVENTURAMANIA]})
    service = PartnerDiscoveryService(provider)

    def _raise_fetch(url):
        raise PublicSourceFetchError("blocked by bot wall")

    import app.services.partner_discovery as mod
    original = mod.fetch_public_source_sync
    mod.fetch_public_source_sync = _raise_fetch
    try:
        candidates = await service.find_candidates()
    finally:
        mod.fetch_public_source_sync = original

    assert len(candidates) == 1
    assert candidates[0].evidence_source == EvidenceSource.SNIPPET
    assert candidates[0].evidence == STRONG_AVENTURAMANIA.snippet


@pytest.mark.asyncio
async def test_provenance_fields_present_on_every_candidate():
    by_query = {"турагент авторские туры": [STRONG_AVENTURAMANIA]}
    service, _ = _service(by_query)
    candidates = await service.find_candidates()
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.query == "турагент авторские туры"
    assert candidate.url == STRONG_AVENTURAMANIA.url
    assert candidate.evidence_source in (EvidenceSource.PAGE, EvidenceSource.SNIPPET)
