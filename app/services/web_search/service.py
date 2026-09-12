"""Decides IF a web search should run, runs it through whatever
WebSearchProvider is configured, and formats the result as LLM-ready text.

No LLM call here for the search/no-search decision (deliberately, per the
ORCHESTRAVEL web-search MVP task): a query needing fresh/changeable
real-world information is detected with the same kind of deterministic
keyword rules already used elsewhere in this codebase (see
app.services.knowledge_service._retrieval_policy and
app.web_api._requested_competitor) - cheaper, faster, and fully testable
without mocking an LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from urllib.parse import urlsplit

from app.services.web_search.base import SearchResponse, WebSearchProvider

# ── A. Freshness ──────────────────────────────────────────────────────────
_FRESHNESS_MARKERS: tuple[str, ...] = (
    "сейчас", "сегодня", "последн", "свеж", "новости", "новое", "новую",
    "изменилось", "изменились", "изменения", "актуальн", "недавно",
)

# ── B. Rules/conditions that can change over time ───────────────────────────
_CHANGEABLE_RULES_MARKERS: tuple[str, ...] = (
    "въезд", "виза", "визы", "визовый", "визового", "безвиз",
    "ограничен", "перелёт", "перелет", "рейс", "рейсы", "цена", "цены",
    "тариф", "тарифы", "расписание",
    # Official-source-priority / geo-scope-guard task: high-risk categories
    # the user named that were not previously covered (tourist fees, customs,
    # passport/medical entry requirements). Deliberately phrase-level
    # (multi-word) or a narrow single-word stem per item, NOT a bare "сбор"/
    # "налог"/"паспорт" - those collide with unrelated text ("сбор
    # документов", general tax talk, "паспортный стол"). "таможен"/"декларац"
    # stay single-stem because in Russian they are near-exclusively customs/
    # declaration vocabulary already, so a stem is not "too broad" here.
    "туристический сбор", "туристического сбора", "туристическим сбором",
    "туристическом сборе", "курортный сбор", "налог для туристов",
    "туристический налог",
    # "таможня" (noun: на таможне, через таможню) and "таможенный" (adjective:
    # таможенные правила) are two different stems in Russian morphology
    # (irregular "-ен-" insertion for the adjective) - both kept, since both
    # are unambiguous customs vocabulary with no unrelated meaning.
    "таможн", "таможен", "декларац",
    "паспортные требования", "требования к паспорту",
    "медицинские требования",
)

# ── C. Market / named companies ──────────────────────────────────────────
_MARKET_MARKERS: tuple[str, ...] = ("конкурент", "рынк")
_NAMED_COMPANY_MARKERS: tuple[str, ...] = ("travel advantage", "mwr life")
# Bare company mentions ("напиши пост про Travel Advantage") must NOT trigger
# a search - only mentions paired with an actuality qualifier do (see task
# category C: "в контексте что нового / что происходит / актуально").
_ACTUALITY_QUALIFIERS: tuple[str, ...] = ("что нового", "что происходит", "актуальн")

# ── D. Explicit intent ───────────────────────────────────────────────────
_EXPLICIT_INTENT_MARKERS: tuple[str, ...] = (
    "найди в интернете", "поищи в интернете", "поищи", "проверь в интернете",
    "посмотри актуальную информацию", "найди свежую информацию",
    "найди актуальную информацию", "загугли",
)

# ── E. URL / domain in the query ──────────────────────────────────────────
_URL_RE = re.compile(
    r"https?://[^\s]+"
    r"|(?<![\w@.])(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s]*)?",
    re.IGNORECASE,
)


# ── Official-source priority ─────────────────────────────────────────────
#
# Domain-suffix/substring check, not a geography ontology: government sites
# follow a small number of real, well-known naming conventions across
# countries (.gov, .gov.<cc>, .go.<cc>, .gob.<cc>, .gouv.<cc>) plus a few
# named exceptions (Russian MFA/consular sites, embassy/consulate/imigrasi
# subdomains). New countries need no code change as long as they follow one
# of these conventions; one that doesn't simply stays "other" - reranking
# degrades to a no-op for it (see the "targeted fallback" limitation noted
# in the task write-up), it never mislabels or drops a result.
_OFFICIAL_DOMAIN_SUFFIX_PATTERN = re.compile(
    r"\.(gov|go|gob|gouv)\.[a-z]{2,3}$", re.IGNORECASE
)
_OFFICIAL_DOMAIN_EXACT_SUFFIXES: tuple[str, ...] = (".gov", ".mid.ru", ".kdmid.ru")
_OFFICIAL_DOMAIN_SUBSTRING_MARKERS: tuple[str, ...] = (
    "embassy", "consulate", "imigrasi",
)


def _is_official_domain(domain: str) -> bool:
    """True for a government/embassy/consulate/immigration-authority domain.

    Best-effort, deliberately conservative: only well-known conventions and
    a short substring list, so a false negative (a real official site not
    recognized) just falls back to today's unranked order for that one
    result - never a false claim of authority for a random domain.
    """
    lowered = (domain or "").strip().lower()
    if not lowered:
        return False
    for suffix in _OFFICIAL_DOMAIN_EXACT_SUFFIXES:
        # endswith() alone misses the bare domain equal to the suffix
        # itself (e.g. domain == "mid.ru", suffix == ".mid.ru").
        if lowered == suffix.lstrip(".") or lowered.endswith(suffix):
            return True
    if _OFFICIAL_DOMAIN_SUFFIX_PATTERN.search(lowered):
        return True
    return any(marker in lowered for marker in _OFFICIAL_DOMAIN_SUBSTRING_MARKERS)


def _rank_by_authority(response: SearchResponse) -> SearchResponse:
    """Stable-sorts results so official domains come first - reorder only,
    nothing is dropped or added. Stable sort (Python's sort() guarantee)
    keeps relative order within each tier, so results the provider already
    ranked against each other stay in that order among themselves.

    This is stage one of official-source priority: it can only reorder
    what the provider already returned in this one search call. If Yandex
    never returned an official domain within ``max_results``, there is
    nothing here to promote - see the task write-up's "targeted fallback"
    follow-up for closing that gap.
    """
    if response is None or not response.results:
        return response
    ranked = sorted(
        response.results, key=lambda result: 0 if _is_official_domain(result.domain) else 1,
    )
    if ranked == list(response.results):
        return response
    return replace(response, results=ranked)


# ── Official-source fallback (stage two of official-source priority) ───────
#
# Stage one (_rank_by_authority above) can only reorder what the provider
# already returned in ONE search call - if Yandex's top results for a query
# are all secondary sources (aggregators/media/agencies), there is nothing
# to promote. Live testing after stage one shipped confirmed exactly this:
# Yandex returned 5 secondary sources and no official domain within
# max_results, so reranking was a no-op.
#
# Stage two, for the same changeable-rules gate only: if the first search
# already has an official domain, do nothing more (no second call - see
# _ensure_official_source). If it doesn't, run exactly ONE targeted
# follow-up search biased toward government/embassy/immigration sources,
# then merge official results from that fallback in ahead of the original
# secondary results, dedup by URL, and cap the combined list at a sane
# limit. Nothing heavier than that - no per-country ontology, no second
# general search.
_OFFICIAL_FALLBACK_QUERY_SUFFIX = (
    " официальный сайт правительства посольство консульство миграционная служба"
)
_FALLBACK_COMBINED_RESULTS_LIMIT = 5


def _official_fallback_query(query: str) -> str:
    return f"{query.strip()}{_OFFICIAL_FALLBACK_QUERY_SUFFIX}"


def _merge_official_fallback(
    original: SearchResponse, fallback: SearchResponse | None
) -> SearchResponse:
    """Puts official results found by the fallback search ahead of the
    original results, keeps every secondary result from the original search,
    dedupes by URL, and caps the total. Only ever called when ``original``
    has no official domain yet - see _ensure_official_source."""
    if fallback is None or not fallback.results:
        return original
    seen_urls = {result.url for result in original.results}
    official_from_fallback = [
        result for result in fallback.results
        if _is_official_domain(result.domain) and result.url not in seen_urls
    ]
    if not official_from_fallback:
        return original
    combined = [*official_from_fallback, *original.results][:_FALLBACK_COMBINED_RESULTS_LIMIT]
    return replace(original, results=combined)


def _ensure_official_source(
    response: SearchResponse, provider: WebSearchProvider, *, query: str, site: str | None,
) -> SearchResponse:
    """Stage two entry point - called only for changeable-rules queries,
    after stage-one reranking already ran. A no-op (no second search call)
    when an official domain is already present; otherwise runs exactly one
    targeted fallback search and merges it in."""
    if any(_is_official_domain(result.domain) for result in response.results):
        return response
    fallback = provider.search(_official_fallback_query(query), site=site)
    return _merge_official_fallback(response, fallback)


@dataclass(frozen=True)
class SearchDecision:
    """Result of decide_web_search() - a plain value, kept separate from
    SearchResponse so the "why" is inspectable/testable on its own."""

    should_search: bool
    site: str | None
    matched_category: str | None


def _matches_changeable_rules(lowered: str) -> bool:
    return any(marker in lowered for marker in _CHANGEABLE_RULES_MARKERS)


def _matches_market(lowered: str) -> bool:
    if any(marker in lowered for marker in _MARKET_MARKERS):
        return True
    if any(company in lowered for company in _NAMED_COMPANY_MARKERS):
        return any(qualifier in lowered for qualifier in _ACTUALITY_QUALIFIERS)
    return False


def _extract_site(lowered: str) -> str | None:
    match = _URL_RE.search(lowered)
    if not match:
        return None
    candidate = match.group(0)
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    try:
        hostname = urlsplit(candidate).hostname
    except ValueError:
        return None
    return hostname or None


def decide_web_search(query: str) -> SearchDecision:
    """Pure, deterministic, no I/O - safe to unit test exhaustively.

    Order matters only for ``matched_category`` (diagnostic/testing value);
    ``should_search`` is a plain OR across every category, exactly like the
    task's category list A-E.
    """
    text = (query or "").strip()
    if not text:
        return SearchDecision(False, None, None)
    lowered = text.lower()

    detected_site = _extract_site(lowered)

    if any(marker in lowered for marker in _EXPLICIT_INTENT_MARKERS):
        return SearchDecision(True, detected_site, "explicit_intent")
    if detected_site:
        return SearchDecision(True, detected_site, "site")
    if any(marker in lowered for marker in _FRESHNESS_MARKERS):
        return SearchDecision(True, None, "freshness")
    if _matches_changeable_rules(lowered):
        return SearchDecision(True, None, "changeable_rules")
    if _matches_market(lowered):
        return SearchDecision(True, None, "market")
    return SearchDecision(False, None, None)


class WebSearchService:
    """The one thing app.web_api (and, later, Telegram) should ever import
    to get web-search context - never YandexSearchProvider directly, so
    swapping/adding a provider never touches a caller."""

    def __init__(self, provider: WebSearchProvider | None, *, enabled: bool) -> None:
        self._provider = provider
        self._enabled = enabled

    def maybe_search(
        self, query: str, *, site: str | None = None
    ) -> SearchResponse | None:
        """Blocking call (same convention as WebSearchProvider.search /
        LLMProvider methods) - callers use asyncio.to_thread. Returns None
        when search is disabled, unconfigured, not needed for this query, or
        failed for any reason - the caller never needs to distinguish why."""
        if not self._enabled or self._provider is None:
            return None
        decision = decide_web_search(query)
        if not decision.should_search:
            return None
        response = self._provider.search(query, site=site or decision.site)
        # Official-source priority is gated to queries that actually match
        # _CHANGEABLE_RULES_MARKERS (visas, entry rules, borders, fees,
        # customs, passport/medical entry requirements) - checked directly
        # here rather than via decision.matched_category, because
        # matched_category only records whichever category's check ran
        # FIRST in decide_web_search's if/elif chain (see its docstring:
        # "Order matters only for matched_category - diagnostic/testing
        # value"). The real prod query ("Какие СЕЙЧАС изменения правил
        # ВЪЕЗДА...") matches both freshness ("сейчас") and changeable_rules
        # ("въезд") - freshness wins the diagnostic label since it is
        # checked first, but the query is still exactly the high-risk class
        # this task is about, so gating on the label alone would have
        # silently skipped reranking for the one query this fix exists for.
        # A market/company/freshness-only query has no "official" domain
        # concept, so reordering it would just be arbitrary noise with no
        # safety benefit - hence still gated, just on the marker match
        # itself rather than the label.
        if response is not None and _matches_changeable_rules((query or "").lower()):
            response = _rank_by_authority(response)
            response = _ensure_official_source(
                response, self._provider, query=query, site=site or decision.site,
            )
        return response


# ── LLM-ready context formatting ────────────────────────────────────────────

_HEADER = "=== АКТУАЛЬНЫЙ ПОИСК В ИНТЕРНЕТЕ ==="
_RULES = (
    "Правила: используй эти данные, только если они относятся к вопросу; "
    "не придумывай сведения сверх найденных источников; при противоречии "
    "источников явно отметь это в ответе; для актуальных фактов опирайся на "
    "найденные источники. Ссылку можно упомянуть внутри содержательного "
    "ответа, если это уместно (например: «официальная форма: https://…»). "
    "Но НЕ добавляй в конце ответа отдельный раздел или список «Источники», "
    "«Sources», «Ссылки» и т.п. - источники уже показываются пользователю "
    "отдельным блоком интерфейса. "
    # Geographic-scope guard + official-source priority (real prod bug:
    # Indonesia-wide answer built from a Bali-only tourist-fee source).
    # Universal instruction, not a geography lookup table - the model is
    # told to check level-of-government match itself, using whatever scope
    # the source text already states.
    "Географический охват: если источник описывает правило для региона, "
    "провинции, города или конкретного пункта пересечения границы (например, "
    "Бали), нельзя формулировать это правило как действующее для всей страны "
    "(например, Индонезии) без отдельного источника, подтверждающего именно "
    "общегосударственный уровень. Всегда явно указывай географический "
    "уровень действия правила: страна / регион / город / пункт въезда. "
    "Денежные обязательные сборы указывай в валюте официального источника "
    "как есть, без фиксированного пересчёта в USD/EUR по курсу - курс "
    "меняется, а один зафиксированный в ответе эквивалент вводит в "
    "заблуждение. Если среди источников нет государственного/официального "
    "сайта (посольство, консульство, миграционная или таможенная служба), "
    "прямо скажи, что официальное подтверждение не найдено - не выдавай "
    "вторичный источник (агрегатор, СМИ, турагентство, страховую компанию) "
    "за установленный государством факт. При противоречии официального и "
    "вторичного источника приоритет всегда у официального."
)

_OFFICIAL_SOURCE_STATUS_FOUND = "OFFICIAL_SOURCE_STATUS: FOUND"
_OFFICIAL_SOURCE_STATUS_NOT_FOUND = "OFFICIAL_SOURCE_STATUS: NOT_FOUND"
# Explicit, machine-checkable status line (not just the prose _RULES rule
# above) - live testing showed the prose alone was not reliably followed: an
# answer was generated without mentioning the missing confirmation even
# though that exact rule already existed. Kept OUT of the always-appended
# _RULES constant and only added here, alongside the literal status line
# itself, for the same changeable-rules queries the fallback search
# (_ensure_official_source) is gated on - a market/freshness/company query
# has no "official source" concept and must not see this at all.
_OFFICIAL_SOURCE_STATUS_INSTRUCTION = (
    "Если строка выше - «OFFICIAL_SOURCE_STATUS: NOT_FOUND», ты ОБЯЗАН явно "
    "сообщить пользователю в ответе, что официальное государственное "
    "подтверждение по этому вопросу сейчас не найдено. Если строка выше - "
    "«OFFICIAL_SOURCE_STATUS: FOUND», официальный источник уже указан первым "
    "в списке результатов - используй его как основной."
)


def format_search_context(response: SearchResponse | None) -> str:
    """Same "=== HEADER ===" + per-item + rules shape as
    app.web_api._knowledge_context, so it reads as one consistent style of
    context block to the model, not a bolted-on second format. Empty string
    (never None) when there is nothing to show - callers already use the
    "\\n\\n".join(part for part in (...) if part) pattern for
    knowledge_context, so this composes into it for free."""
    if response is None or not response.results:
        return ""

    lines = [_HEADER, "", f"Запрос: {response.query}", ""]
    for result in response.results:
        lines.append(f"[{result.rank}] {result.title}")
        if result.snippet:
            lines.append(result.snippet)
        lines.append(f"Источник: {result.url}")
        lines.append("")
    # Only for the same high-risk/changeable-rules category the official-
    # source fallback (_ensure_official_source) itself is gated on - a
    # market/freshness/company query has no "official source" concept, so a
    # status line there would be meaningless noise, not a signal.
    if _matches_changeable_rules((response.query or "").lower()):
        has_official = any(_is_official_domain(result.domain) for result in response.results)
        lines.append(
            _OFFICIAL_SOURCE_STATUS_FOUND if has_official else _OFFICIAL_SOURCE_STATUS_NOT_FOUND
        )
        lines.append(_OFFICIAL_SOURCE_STATUS_INSTRUCTION)
        lines.append("")
    lines.append(_RULES)
    return "\n".join(lines).rstrip()
