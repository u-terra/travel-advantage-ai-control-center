"""Failure handling of partner discovery (2026-10-12): an unfinished LLM
qualification is a FAILURE (not "no candidates"), the web keeps the finished
outcome per workspace for re-delivery, and one transient Yandex network
failure per query is retried inside the search budget.

Thresholds / hard exclusions / ranking / candidate cap are untouched - these
tests only cover how failures are reported and retried.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.domain.partners import WorkspaceContext
from app.domain.prospects import EvidenceSource, ProspectCandidate, ProspectFit, ProspectType
from app.handlers.partner_discovery import on_find_partners
from app.services.lead_qualification import QualificationUnavailable, qualify_candidates
from app.services.partner_discovery import PartnerDiscoveryService
from app.services.web_search import yandex_provider as yp
from app.services.web_search.base import SearchResponse, SearchResult
from app.services.web_search.yandex_provider import YandexSearchConfig, YandexSearchProvider


def _run(coro):
    return asyncio.run(coro)


@dataclass
class _Cfg:
    api_key: str = "test-key"


@dataclass
class _Result:
    text: str


class _LLM:
    def __init__(self, *, text="[]", error: Exception | None = None, delay: float = 0.0):
        self.config = _Cfg()
        self._text, self._error, self._delay = text, error, delay
        self.calls = 0

    def generate(self, *, message: str):
        self.calls += 1
        if self._delay:
            time.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return _Result(self._text)


def _cand(url="https://example-agency.ru/"):
    return {
        "name": "Example", "url": url, "prospect_type": "travel_agent",
        "query": "q", "evidence": "Турагентство, авторские туры.",
        "evidence_source": "page", "fit": "STRONG", "rank_score": 5,
        "display_name": "Example", "reason": "Турагентство.",
    }


def _low_score_answer(candidate):
    return json.dumps([{
        "professional_relevance": 5, "activity": 0, "commercial_potential": 0,
        "audience_quality": 0, "contactability": 0, "personal_hook_score": 0,
        "why_fit": ["x"], "hook": "", "risk_flags": [], "confidence": 0.9,
        "evidence": [],
    }])


# ── qualification: failure vs. successful empty result ───────────────────

@pytest.mark.parametrize("llm", [
    _LLM(error=TimeoutError("The read operation timed out")),
    _LLM(text="not json at all"),
])
def test_require_llm_raises_instead_of_using_fallback(llm) -> None:
    with pytest.raises(QualificationUnavailable):
        _run(qualify_candidates([_cand()], llm, require_llm=True))


def test_require_llm_without_provider_raises() -> None:
    with pytest.raises(QualificationUnavailable) as info:
        _run(qualify_candidates([_cand()], None, require_llm=True))
    assert info.value.reason == "no_provider"


def test_llm_timeout_is_bounded_and_reported() -> None:
    llm = _LLM(text="[]", delay=0.5)

    async def scenario():
        started = time.monotonic()
        with pytest.raises(QualificationUnavailable):
            await qualify_candidates([_cand()], llm, require_llm=True, llm_timeout_seconds=0.05)
        return time.monotonic() - started  # the caller is released at the timeout

    assert _run(scenario()) < 0.4


def test_successful_empty_result_is_not_an_error() -> None:
    candidate = _cand()
    llm = _LLM(text=_low_score_answer(candidate))
    result = _run(qualify_candidates([candidate], llm, require_llm=True))
    assert result == []  # completed check, nobody qualified


def test_default_behaviour_without_require_llm_still_falls_back() -> None:
    result = _run(qualify_candidates([_cand()], _LLM(error=RuntimeError("boom"))))
    assert isinstance(result, list)  # legacy callers unchanged, no raise


# ── Telegram ──────────────────────────────────────────────────────────────

class _Message:
    def __init__(self):
        self.text = "x"
        self.answers = []
        self.chat = SimpleNamespace(id=1)

    async def answer(self, text, **kwargs):
        self.answers.append(text)


class _State:
    async def get_data(self):
        return {}

    async def update_data(self, **kwargs):
        pass


def _prospect():
    return ProspectCandidate(
        name="Example", url="https://example-agency.ru/", prospect_type=ProspectType.TRAVEL_AGENT,
        query="q", evidence="Турагентство.", evidence_source=EvidenceSource.PAGE,
        fit=ProspectFit.STRONG, rank_score=5,
    )


def _telegram(llm):
    message = _Message()
    repo = SimpleNamespace(
        get_business_profile=AsyncMock(return_value=SimpleNamespace(
            business_type="club_partner", ta_affiliated=False)),
    )
    with patch(
        "app.handlers.partner_discovery.PartnerDiscoveryService.find_candidates",
        AsyncMock(return_value=[_prospect()]),
    ):
        _run(on_find_partners(
            message, _State(), WorkspaceContext(100, 42, "owner", "active"), repo,
            SimpleNamespace(provider=object()), llm,
            partner_discovery_v2_enabled=True,
        ))
    return message.answers[0]


def test_telegram_qualification_failure_is_not_reported_as_no_candidates() -> None:
    text = _telegram(_LLM(error=TimeoutError("The read operation timed out")))
    assert "сбой" in text
    assert "не нашлось" not in text


def test_telegram_successful_empty_result_says_no_candidates() -> None:
    text = _telegram(_LLM(text=_low_score_answer(_cand())))
    # enrichment shares the same fake LLM; both answers are the low-score JSON,
    # enrichment falls back deterministically, qualification completes.
    assert "не нашлось" in text
    assert "сбой" not in text


# ── Web: error status, stored result, isolation ──────────────────────────

@pytest.fixture
def web_api(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("markdown")
    monkeypatch.setenv("JOURNAL_DB_PATH", str(tmp_path / "journal.sqlite3"))
    monkeypatch.setenv("PLANNER_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "false")
    import sys
    sys.modules.pop("app.web_api", None)
    import app.web_api as module
    module._partner_discovery_results.clear()
    yield module
    sys.modules.pop("app.web_api", None)


def _principal(workspace_id):
    return SimpleNamespace(workspace_id=workspace_id)


def _v2_settings(web_api):
    return SimpleNamespace(
        partner_discovery_v2_enabled=True, partner_min_score=65,
        partner_min_confidence=0.60, partner_max_results=5,
    )


def _web_patches(web_api, find, llm):
    from contextlib import ExitStack
    stack = ExitStack()
    stack.enter_context(patch.object(web_api, "_web_is_club_partner", AsyncMock(return_value=True)))
    stack.enter_context(patch.object(web_api, "_web_is_ta_affiliated", AsyncMock(return_value=False)))
    fake_service = stack.enter_context(patch.object(web_api, "web_search_service"))
    fake_service.provider = object()
    stack.enter_context(patch.object(web_api, "settings", _v2_settings(web_api)))
    stack.enter_context(patch.object(web_api, "chat_provider", llm))
    stack.enter_context(patch("app.web_api.PartnerDiscoveryService.find_candidates", find))
    return stack


def test_web_qualification_failure_returns_error_not_empty_success(web_api) -> None:
    async def find(self, **_kw):
        return [_prospect()]

    with _web_patches(web_api, find, _LLM(error=TimeoutError("The read operation timed out"))):
        body = _run(web_api.discover_partners(_principal(1)))

    assert body["error_code"] == "qualification_unavailable"
    assert "сбой" in body["error"]
    assert body["candidates"] == []


def test_web_successful_empty_result_has_no_error(web_api) -> None:
    async def find(self, **_kw):
        return [_prospect()]

    with _web_patches(web_api, find, _LLM(text=_low_score_answer(_cand()))):
        body = _run(web_api.discover_partners(_principal(1)))

    assert body == {"candidates": []}


def test_web_result_is_redelivered_after_dropped_client_without_new_pipeline(web_api) -> None:
    calls = 0
    release = asyncio.Event()

    async def find(self, **_kw):
        nonlocal calls
        calls += 1
        await release.wait()
        return [_prospect()]

    async def scenario():
        with _web_patches(web_api, find, _LLM(text=_low_score_answer(_cand()))):
            dropped = asyncio.create_task(web_api.discover_partners(_principal(1)))
            await asyncio.sleep(0.01)
            dropped.cancel()
            await asyncio.gather(dropped, return_exceptions=True)
            running = await web_api.get_partner_discovery_result(_principal(1))
            release.set()
            while 1 in web_api._partner_discovery_inflight:
                await asyncio.sleep(0.01)
            done = await web_api.get_partner_discovery_result(_principal(1))
            again = await web_api.get_partner_discovery_result(_principal(1))
            return running, done, again

    running, done, again = _run(scenario())

    assert running == {"status": "running"}
    assert done == {"status": "done", "candidates": []}
    assert again == done  # repeated reads never start/pay for anything
    assert calls == 1


def test_web_error_status_is_also_stored(web_api) -> None:
    async def find(self, **_kw):
        return [_prospect()]

    async def scenario():
        with _web_patches(web_api, find, _LLM(error=TimeoutError("t"))):
            await web_api.discover_partners(_principal(1))
            return await web_api.get_partner_discovery_result(_principal(1))

    stored = _run(scenario())
    assert stored["status"] == "done"
    assert stored["error_code"] == "qualification_unavailable"


def test_web_stored_result_is_isolated_per_workspace(web_api) -> None:
    async def find(self, **_kw):
        return [_prospect()]

    async def scenario():
        with _web_patches(web_api, find, _LLM(error=TimeoutError("t"))):
            await web_api.discover_partners(_principal(1))
            return (
                await web_api.get_partner_discovery_result(_principal(1)),
                await web_api.get_partner_discovery_result(_principal(2)),
            )

    own, other = _run(scenario())
    assert own["status"] == "done"
    assert other == {"status": "none"}


def test_web_explicit_new_search_after_finished_run_starts_new_pipeline(web_api) -> None:
    calls = 0

    async def find(self, **_kw):
        nonlocal calls
        calls += 1
        return []

    async def scenario():
        with _web_patches(web_api, find, _LLM()):
            await web_api.discover_partners(_principal(1))
            await web_api.discover_partners(_principal(1))

    _run(scenario())
    assert calls == 2


def test_web_stored_result_expires(web_api) -> None:
    web_api._partner_discovery_results[1] = (
        time.monotonic() - web_api._PARTNER_RESULT_TTL_S - 1, {"candidates": []},
    )

    async def scenario():
        with patch.object(web_api, "_web_is_club_partner", AsyncMock(return_value=True)):
            return await web_api.get_partner_discovery_result(_principal(1))

    assert _run(scenario()) == {"status": "none"}


def test_web_run_over_budget_is_reported_as_failure(web_api) -> None:
    async def find(self, **_kw):
        await asyncio.sleep(5)
        return []

    async def scenario():
        with _web_patches(web_api, find, _LLM()), \
                patch.object(web_api, "_PARTNER_RUN_BUDGET_S", 0.05):
            return await web_api.discover_partners(_principal(1))

    body = _run(scenario())
    assert body["error_code"] == "budget_exceeded"
    assert body["candidates"] == []


def test_time_budget_is_consistent_with_client_and_nginx(web_api) -> None:
    from pathlib import Path
    html = (Path(web_api.__file__).resolve().parents[1] / "app" / "templates" / "chat.html").read_text(
        encoding="utf-8")
    client_ms = int(html.split("const PARTNERS_CLIENT_TIMEOUT_MS =")[1].split(";")[0])
    assert web_api._PARTNER_RUN_BUDGET_S * 1000 < client_ms < 240_000
    assert web_api._PARTNER_SEARCH_PHASE_S < web_api._PARTNER_VERIFY_PHASE_END_S < web_api._PARTNER_RUN_BUDGET_S


def test_frontend_recovers_result_after_dropped_connection() -> None:
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "app" / "templates" / "chat.html").read_text(
        encoding="utf-8")
    assert '"/api/partners/discover/result"' in html
    assert "recoverPartnersResult()" in html


# ── Yandex: one retry of the failed query only ───────────────────────────

def _res(url):
    return SearchResult(title="t", url=url, snippet="турагентство авторские туры",
                        domain=url.split("/")[2], published_at=None, provider="yandex", rank=1)


class _FlakyProvider:
    """First call of one query fails; ``transient`` tells whether it's retryable."""

    def __init__(self, *, transient: bool, always_fail: bool = False):
        self.calls: list[str] = []
        self.last_failure_transient = False
        self._transient, self._always_fail = transient, always_fail
        self._failed: set[str] = set()

    def search(self, query, **_kw):
        self.calls.append(query)
        self.last_failure_transient = False
        if query == "турагент авторские туры" and (
            self._always_fail or query not in self._failed
        ):
            self._failed.add(query)
            self.last_failure_transient = self._transient
            return None
        return SearchResponse(query=query, results=[_res("https://a-agency.ru/")], provider="yandex")


async def _no_verify(self, result):
    return "e", EvidenceSource.SNIPPET


def _service(provider):
    service = PartnerDiscoveryService(provider)
    service._verify = _no_verify.__get__(service, PartnerDiscoveryService)
    return service


def test_transient_failure_retries_only_that_query_once() -> None:
    provider = _FlakyProvider(transient=True)
    _run(_service(provider).find_candidates())
    assert provider.calls.count("турагент авторские туры") == 2
    assert len(provider.calls) == len(set(provider.calls)) + 1  # nothing else repeated


def test_retry_happens_at_most_once() -> None:
    provider = _FlakyProvider(transient=True, always_fail=True)
    _run(_service(provider).find_candidates())
    assert provider.calls.count("турагент авторские туры") == 2


def test_non_transient_failure_is_not_retried() -> None:
    provider = _FlakyProvider(transient=False)
    _run(_service(provider).find_candidates())
    assert provider.calls.count("турагент авторские туры") == 1


def test_no_retry_when_search_budget_is_nearly_spent() -> None:
    provider = _FlakyProvider(transient=True)
    deadline = time.monotonic() + 1.0  # less than the minimum retry window
    _run(_service(provider).find_candidates(search_deadline=deadline))
    assert provider.calls.count("турагент авторские туры") == 1


def test_expired_search_deadline_stops_new_queries() -> None:
    provider = _FlakyProvider(transient=False)
    _run(_service(provider).find_candidates(search_deadline=time.monotonic() - 1))
    assert provider.calls == []


def test_yandex_provider_marks_only_network_errors_transient(monkeypatch) -> None:
    provider = YandexSearchProvider(YandexSearchConfig(api_key="k", folder_id="f"))

    def timeout_open(request, *, timeout):
        raise TimeoutError("timed out")

    monkeypatch.setattr(yp, "_open", timeout_open)
    assert provider.search("q") is None
    assert provider.last_failure_transient is True

    def http_error_open(request, *, timeout):
        raise urllib.error.HTTPError("u", 500, "err", {}, None)

    monkeypatch.setattr(yp, "_open", http_error_open)
    assert provider.search("q") is None
    assert provider.last_failure_transient is False


# ── pool qualification: candidates beyond the first five are still judged ─

from app.services import partner_pool  # noqa: E402


def _pool(n):
    return [_cand(f"https://site{i}.ru/") for i in range(n)]


def _fake_qualify(good_urls, calls, *, fail_on_call=None):
    async def fake(batch, llm, *, min_score, min_confidence, max_results, require_llm,
                   llm_timeout_seconds=None):
        calls.append([c["url"] for c in batch])
        if fail_on_call is not None and len(calls) == fail_on_call:
            raise QualificationUnavailable("exception")
        return [{**c, "total_score": 80, "confidence": 0.9} for c in batch if c["url"] in good_urls]
    return fake


async def _same(batch):
    return batch


def _qp(pool, calls, good, **kw):
    with patch.object(partner_pool, "qualify_candidates", _fake_qualify(good, calls,
                      fail_on_call=kw.pop("fail_on_call", None))):
        return _run(partner_pool.qualify_pool(
            pool, object(), enrich=_same, enrich_fallback=lambda b: b, rank=lambda b: b,
            min_score=65, min_confidence=0.6, max_results=5, **kw))


def test_candidate_beyond_first_five_is_qualified_when_first_five_fail() -> None:
    calls = []
    outcome = _qp(_pool(7), calls, {"https://site6.ru/"})
    assert [c["url"] for c in outcome.candidates] == ["https://site6.ru/"]
    assert len(calls) == 2 and outcome.checked == 7 and not outcome.incomplete


def test_second_batch_not_checked_once_five_are_qualified() -> None:
    calls = []
    outcome = _qp(_pool(9), calls, {f"https://site{i}.ru/" for i in range(5)})
    assert len(calls) == 1 and len(outcome.candidates) == 5


def test_first_batch_failure_is_a_failure_not_empty() -> None:
    with pytest.raises(QualificationUnavailable):
        _qp(_pool(7), [], set(), fail_on_call=1)


def test_later_batch_failure_returns_partial_and_says_so() -> None:
    outcome = _qp(_pool(7), [], {"https://site0.ru/"}, fail_on_call=2)
    assert [c["url"] for c in outcome.candidates] == ["https://site0.ru/"]
    assert outcome.incomplete and outcome.checked == 5 and outcome.total == 7


def test_budget_exhausted_before_second_batch_is_reported_incomplete() -> None:
    calls = []
    outcome = _qp(_pool(7), calls, set(), deadline=time.monotonic() + 30)
    assert len(calls) == 1 and outcome.incomplete_reason == "budget"
    assert outcome.checked == 5 and outcome.candidates == []


def test_find_candidates_max_results_extends_pool_without_reordering() -> None:
    results = [_res(f"https://agency{i}.ru/") for i in range(8)]

    class _P:
        def search(self, query, **_kw):
            return SearchResponse(query=query, results=results, provider="yandex")

    default = _run(_service(_P()).find_candidates())
    pool = _run(_service(_P()).find_candidates(max_results=10))
    assert len(default) == 5 and len(pool) == 8
    assert [c.url for c in pool[:5]] == [c.url for c in default]


def test_web_scenario_first_five_rejected_good_one_found_further(web_api) -> None:
    prospects = [
        ProspectCandidate(
            name=f"P{i}", url=f"https://site{i}.ru/", prospect_type=ProspectType.TRAVEL_AGENT,
            query="q", evidence="e", evidence_source=EvidenceSource.PAGE,
            fit=ProspectFit.STRONG, rank_score=5,
        ) for i in range(7)
    ]
    seen = {}

    async def find(self, **kw):
        seen.update(kw)
        return prospects

    calls = []

    async def scenario():
        with _web_patches(web_api, find, _LLM()), \
                patch.object(web_api, "enrich_candidates", AsyncMock(side_effect=lambda b, *a, **k: b)), \
                patch.object(partner_pool, "qualify_candidates",
                             _fake_qualify({"https://site6.ru/"}, calls)):
            return await web_api.discover_partners(_principal(1))

    body = _run(scenario())
    assert seen["max_results"] == 10
    assert [c["url"] for c in body["candidates"]] == ["https://site6.ru/"]
    assert "incomplete" not in body


def test_web_partial_check_is_reported(web_api) -> None:
    prospects = [
        ProspectCandidate(
            name=f"P{i}", url=f"https://site{i}.ru/", prospect_type=ProspectType.TRAVEL_AGENT,
            query="q", evidence="e", evidence_source=EvidenceSource.PAGE,
            fit=ProspectFit.STRONG, rank_score=5,
        ) for i in range(7)
    ]

    async def find(self, **kw):
        return prospects

    async def scenario():
        with _web_patches(web_api, find, _LLM()), \
                patch.object(web_api, "enrich_candidates", AsyncMock(side_effect=lambda b, *a, **k: b)), \
                patch.object(partner_pool, "qualify_candidates",
                             _fake_qualify(set(), [], fail_on_call=2)):
            return await web_api.discover_partners(_principal(1))

    body = _run(scenario())
    assert body["candidates"] == [] and body["incomplete"] is True
    assert (body["checked"], body["total"]) == (5, 7)
    assert "error" not in body


def test_frontend_shows_incomplete_check_note() -> None:
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "app" / "templates" / "chat.html").read_text(
        encoding="utf-8")
    assert "partnersIncompleteNote(data)" in html and "Проверка неполная" in html
