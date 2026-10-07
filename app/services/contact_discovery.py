"""Partner Discovery v0, contact-discovery layer: given a ProspectCandidate
(a project/resource already found by app.services.partner_discovery), find
the one concrete, publicly identifiable human contact to approach - or,
failing that, fall back to the project's own public channel.

This module changes NOTHING about discovery itself: it never touches
app.services.partner_discovery's queries, ranking, scoring, dedupe, or
denylist, and it is not invoked as part of that pipeline. It only runs
AFTER a prospect is already chosen, on its own explicit trigger.

Algorithm (find_contact), in order - stops at the first confident result:

A. Fetch the prospect's own public page (fetch_public_source_sync).
B. ONE LLM extraction call over that page's text. The LLM is used purely
   as an EXTRACTOR/VALIDATOR of text already in hand - it is never asked
   to search for or infer information not present in the given text. If
   it returns a confident person+role, that is HIGH confidence and we
   STOP (no web search call).
C. Only if A/B found nobody: run up to 3 targeted Yandex searches (project
   name + "основатель"/"руководитель"/"организатор"), small result limit
   each. This is a narrow, fixed-shape query set - never a broad person
   search, never a users.search/groups.getMembers-style lookup (neither of
   which this codebase's web_search layer even exposes - see
   app.services.web_search.base.WebSearchProvider).
D. ONE LLM validation call over the combined search titles/snippets/URLs
   from step C. Accepts a person only if the name, an explicit role, AND
   an explicit link to the original project are all present together in
   the evidence. Result here is capped at MEDIUM confidence - a person
   found this way is from an independent page the project does not
   control, never promoted to HIGH.
E. Only if A-D found nobody AND the prospect itself looks like a
   community/directory/network (a guild, association, club, catalog -
   see _looks_like_community_directory) rather than a single-owner
   business: run a SECOND, separate, up-to-3-query Yandex search pass
   (project name + "гид"/"экскурсовод"/"автор тура" instead of
   "основатель"/"руководитель"/"организатор") to find ONE concrete public
   PROFESSIONAL inside that resource (a guide/excursion
   leader/tour organizer/tour author) - never the resource's own
   owner/leader, which steps A-D already tried and failed to find. Same
   fail-closed validation shape as step D (one LLM call, MEDIUM
   confidence, evidence_url must be one of the actually-returned search
   URLs), and the SAME narrow "max 3 queries, no broad person search, no
   member-list lookup" constraint as step C - this never scrapes a
   directory's member list, it only ever searches the public web the
   same way step C already does. An ordinary single-owner business (e.g.
   a tour operator with its own "team" page) is deliberately NOT
   classified as a directory just because it lists staff - see
   _looks_like_community_directory's own docstring.

Fail-closed by construction: both B and D return "person not found" (never
a fabricated name/URL) whenever the LLM is unsure, whenever the LLM call
raises, or whenever its response is not valid, well-formed JSON in the
expected shape. In every "not found" case the caller gets a
ContactType.PROJECT ContactCandidate pointing at the prospect's own URL -
never an empty/null result and never a guess.

Privacy boundary: only public, professional facts ever enter or leave this
module - a display name, a role, a public URL, and a short evidence
excerpt. No phone, email, address, or private/closed-profile data is ever
extracted, stored on ContactCandidate (see app.domain.contacts - the
dataclass has no such fields), or sent to the LLM for "discovery" (only
already-fetched public page text / already-returned public search
snippets are ever in the prompt). No private API is used or could be used:
app.services.web_search only exposes WebSearchProvider.search() (public web
search), and fetch_public_source_sync only fetches a public URL - neither
has any member-list/users.search-style capability to call in the first
place.

Universal across ORCHESTRAVEL: nothing here is Travel-Advantage-specific,
and ``ta_affiliated`` plays no role - same posture as
app.handlers.partner_discovery.

Stateless, same as Partner Discovery v0 itself: no DB, no migration, no
persistence. Callers (future Telegram UI) are expected to hold the
resulting ContactCandidate in FSM state, exactly like ProspectCandidate
already is.
"""

from __future__ import annotations

import asyncio
import dataclasses
import html
import json
import logging
from urllib.parse import urlsplit, urlunsplit

from app.chat_provider import OpenAIChatProvider
from app.domain.contacts import ContactCandidate, ContactConfidence, ContactType
from app.domain.prospects import ProspectCandidate
from app.planner.fetch import FetchedPublicSource, PublicSourceFetchError, fetch_public_source_sync
from app.services.web_search.base import SearchResult, WebSearchProvider

log = logging.getLogger(__name__)

_MAX_TARGETED_QUERIES = 3
_TARGETED_SEARCH_LIMIT = 3
_ROLE_SUFFIXES = ("основатель", "руководитель", "организатор")

# Step E (professional-contact fallback): a SEPARATE, narrow query set for
# community/directory resources only - see _looks_like_community_directory.
# Distinct wording from _ROLE_SUFFIXES on purpose: those three words mean
# "the person who runs/owns this resource" (what steps C/D already looked
# for and failed to find), these mean "a professional who operates inside
# it" - the two must never be conflated into one search pass.
_MAX_PROFESSIONAL_QUERIES = 3
_PROFESSIONAL_SEARCH_LIMIT = 3
_PROFESSIONAL_ROLE_SUFFIXES = ("гид", "экскурсовод", "автор тура")

# Minimal, narrow lexical heuristic (no new architecture/enum per task) -
# a resource is only ever treated as a community/directory/network if its
# OWN name/evidence/URL (already produced by app.services.partner_discovery,
# never re-fetched or re-scraped here) contains one of these markers.
# Deliberately NOT based on page structure/content (e.g. "has a team page"),
# so an ordinary single-owner tour operator that happens to list staff
# (quinta.ru/team/) is not misclassified - only an explicit
# guild/association/club/catalog/community self-description counts.
_COMMUNITY_DIRECTORY_MARKERS = (
    "гильдия", "сообщество", "ассоциация", "клуб", "каталог", "directory", "community",
)


def _looks_like_community_directory(prospect: ProspectCandidate) -> bool:
    """True only when the prospect's own name/evidence/URL (not any fetched
    page content) explicitly self-describes as a guild/association/club/
    catalog/community - see _COMMUNITY_DIRECTORY_MARKERS. Never true for an
    ordinary business just because it has staff/a team page."""
    haystack = " ".join(
        part for part in (prospect.name, prospect.evidence, prospect.url) if part
    ).lower()
    return any(marker in haystack for marker in _COMMUNITY_DIRECTORY_MARKERS)

_PAGE_EXTRACTION_INSTRUCTIONS = """Ты обрабатываешь данные для внутреннего инструмента Partner Discovery. Это НЕ диалог с пользователем - разовая техническая задача. Отвечай только данными, без приветствий и комментариев.

Тебе дан текст публичной страницы проекта "{project_name}" (URL: {url}). Твоя задача - только ИЗВЛЕЧЬ факт, если он явно присутствует в тексте. НИЧЕГО не домысливай и не выводи по смыслу.

Верни строго валидный JSON-объект одной из двух форм, без markdown, без пояснений:

1) Если в тексте ЯВНО названо имя конкретного человека И его роль ЯВНО связана именно с этим проектом (владелец/основатель/руководитель/организатор/автор/гид/представитель):
{{"found": true, "display_name": "...", "role": "...", "evidence_text": "..."}}
"evidence_text" - короткая цитата/пересказ (до 300 символов) из текста страницы, подтверждающая и имя, и роль.

2) Если явного именованного человека с явной ролью в тексте нет (роль упомянута без имени, имя упомянуто без роли, не уверен, или текст вообще не об этом):
{{"found": false}}

Правила:
- Имя человека должно быть буквально в тексте, а не додумано.
- Роль нельзя выводить "по смыслу" (например, по тону текста или общему описанию деятельности) - только если текст прямо называет роль этого человека.
- Если есть сомнение - верни {{"found": false}}. Ложноположительный результат хуже, чем "не найдено".

ТЕКСТ СТРАНИЦЫ:
{page_text}"""

# Dangling, valueless query-string tokens that are a known AMP-link markup
# artifact (e.g. a site's own `?amp` canonical-link variant, or a trailing
# `&amp;` left over from an un-decoded HTML entity producing an empty
# second segment) - never a real parameter, which always carries either a
# value or a recognizable name of its own. See _normalize_result_url.
_URL_ARTIFACT_QUERY_TOKENS = frozenset({"amp", ""})


def _normalize_result_url(url: str) -> str:
    """Defensive cleanup applied to every search-result URL (and therefore
    to any evidence_url built from one) before it is shown to the LLM or
    returned to a caller - never invents or rewrites a URL's real content.

    Fixes two observed artifacts, both confirmed on real production search
    results for a prospect with an AMP-enabled page:
    1. Literal HTML entities (e.g. "&amp;") that slipped through
       un-decoded from an upstream HTML source - html.unescape() decodes
       these back to their real character ("&amp;" -> "&").
    2. A resulting bare, valueless "amp" query token (or the empty token
       left behind by a decoded trailing "&") - stripped only when the
       token is an EXACT, valueless match; a real parameter that merely
       contains "amp" (e.g. "utm_source=amp") is untouched, since it is a
       different string entirely.
    """
    if not url:
        return url
    decoded = html.unescape(url)
    parts = urlsplit(decoded)
    if not parts.query:
        return urlunsplit(parts)
    kept = [
        token for token in parts.query.split("&")
        if token not in _URL_ARTIFACT_QUERY_TOKENS
    ]
    new_query = "&".join(kept)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def _normalize_search_results(results: list[SearchResult]) -> list[SearchResult]:
    return [dataclasses.replace(result, url=_normalize_result_url(result.url)) for result in results]


_SEARCH_VALIDATION_INSTRUCTIONS = """Ты обрабатываешь данные для внутреннего инструмента Partner Discovery. Это НЕ диалог с пользователем - разовая техническая задача. Отвечай только данными, без приветствий и комментариев.

Тебе дан список результатов публичного веб-поиска по проекту "{project_name}" (его собственная страница: {project_url}). Найди СРЕДИ НИХ конкретного публично названного человека, связанного именно с этим проектом.

Верни строго валидный JSON-объект одной из двух форм, без markdown, без пояснений:

1) Если среди результатов есть результат, где ОДНОВРЕМЕННО присутствуют: явно названное имя человека, явно указанная его роль (основатель/руководитель/организатор/владелец/автор), и явная связь именно с проектом "{project_name}":
{{"found": true, "display_name": "...", "role": "...", "evidence_text": "...", "evidence_url": "..."}}
"evidence_url" должен быть ОДНИМ из URL, перечисленных ниже, скопированным буквально.

2) Если такого результата нет - разные результаты называют разных/неоднозначных людей, роль не указана явно, связь с проектом не очевидна, или результаты не относятся к делу:
{{"found": false}}

Правила:
- Имя и роль должны быть буквально в предоставленном тексте, не выведены по смыслу.
- Если несколько результатов называют РАЗНЫХ людей как относящихся к проекту - это конфликт, верни {{"found": false}}.
- evidence_url нельзя придумывать - только скопировать из списка ниже.
- При любом сомнении верни {{"found": false}}.

РЕЗУЛЬТАТЫ ПОИСКА:
{results_block}"""


_PROFESSIONAL_SEARCH_VALIDATION_INSTRUCTIONS = """Ты обрабатываешь данные для внутреннего инструмента Partner Discovery. Это НЕ диалог с пользователем - разовая техническая задача. Отвечай только данными, без приветствий и комментариев.

Проект "{project_name}" ({project_url}) - это сообщество/каталог/сеть (гильдия, ассоциация, клуб и т.п.), а не самостоятельный бизнес с одним владельцем. Тебе дан список результатов публичного веб-поиска по этому проекту. Найди СРЕДИ НИХ одного конкретного публично названного ПРОФЕССИОНАЛА (гид, экскурсовод, организатор тура, автор тура), связанного именно с этим проектом - НЕ руководителя/владельца самого проекта.

Верни строго валидный JSON-объект одной из двух форм, без markdown, без пояснений:

1) Если среди результатов есть ОДИН результат, где ОДНОВРЕМЕННО присутствуют: явно названное имя человека, явно указанная его профессиональная роль (гид/экскурсовод/организатор тура/автор тура), и явная связь именно с проектом "{project_name}":
{{"found": true, "display_name": "...", "role": "...", "evidence_text": "...", "evidence_url": "..."}}
"evidence_url" должен быть ОДНИМ из URL, перечисленных ниже, скопированным буквально.

2) Если такого результата нет:
{{"found": false}}

Правила:
- Все четыре признака (имя, роль, связь с проектом, URL) должны быть подтверждены В РАМКАХ ОДНОГО результата - нельзя брать имя из одного результата и роль или связь с проектом из другого.
- Имя и роль должны быть буквально в предоставленном тексте, не выведены по смыслу.
- evidence_url нельзя придумывать - только скопировать из списка ниже.
- При любом сомнении верни {{"found": false}}.

РЕЗУЛЬТАТЫ ПОИСКА:
{results_block}"""


def _project_name(prospect: ProspectCandidate) -> str:
    return prospect.name or prospect.url


def _project_fallback(prospect: ProspectCandidate, provenance: str) -> ContactCandidate:
    return ContactCandidate(
        contact_type=ContactType.PROJECT,
        display_name=_project_name(prospect),
        project_name=_project_name(prospect),
        public_url=prospect.url,
        evidence_text="",
        evidence_url=prospect.url,
        provenance=provenance,
    )


def _parse_json_object(raw_text: str) -> dict | None:
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _sanitize_text(value: object, max_len: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned[:max_len]


async def _call_llm(llm_provider: OpenAIChatProvider, prompt: str) -> str | None:
    try:
        result = await asyncio.to_thread(llm_provider.generate, message=prompt)
    except Exception:
        log.warning("contact_discovery: llm call failed", exc_info=True)
        return None
    return result.text


async def _extract_from_page(
    prospect: ProspectCandidate,
    page: FetchedPublicSource,
    llm_provider: OpenAIChatProvider,
) -> ContactCandidate | None:
    """Step B: one LLM extraction call over the page already fetched in
    step A. Returns a HIGH-confidence ContactCandidate, or None if no
    confident person was found (caller proceeds to step C)."""
    prompt = _PAGE_EXTRACTION_INSTRUCTIONS.format(
        project_name=_project_name(prospect), url=prospect.url, page_text=page.text[:6000],
    )
    raw_text = await _call_llm(llm_provider, prompt)
    if raw_text is None:
        return None

    parsed = _parse_json_object(raw_text)
    if parsed is None or parsed.get("found") is not True:
        return None

    display_name = _sanitize_text(parsed.get("display_name"), 120)
    role = _sanitize_text(parsed.get("role"), 120)
    evidence_text = _sanitize_text(parsed.get("evidence_text"), 300)
    if not display_name or not role or not evidence_text:
        return None

    return ContactCandidate(
        contact_type=ContactType.PERSON,
        display_name=display_name,
        role=role,
        project_name=_project_name(prospect),
        public_url=prospect.url,
        evidence_text=evidence_text,
        evidence_url=page.final_url or prospect.url,
        confidence=ContactConfidence.HIGH,
        provenance="official_page",
    )


def _targeted_queries(project_name: str) -> list[str]:
    return [f'"{project_name}" {suffix}' for suffix in _ROLE_SUFFIXES][:_MAX_TARGETED_QUERIES]


def _run_targeted_searches(
    provider: WebSearchProvider, project_name: str,
) -> list[SearchResult]:
    """Step C: up to _MAX_TARGETED_QUERIES searches, each capped at
    _TARGETED_SEARCH_LIMIT results. Deliberately NOT a broad person search:
    a fixed, small query set derived only from the project's own name."""
    results: list[SearchResult] = []
    for query in _targeted_queries(project_name):
        try:
            response = provider.search(query, limit=_TARGETED_SEARCH_LIMIT)
        except Exception:
            log.warning("contact_discovery: targeted search failed", exc_info=True)
            continue
        if response is not None:
            results.extend(response.results)
    return _normalize_search_results(results)


def _format_results_block(results: list[SearchResult]) -> str:
    lines = []
    for i, result in enumerate(results, 1):
        lines.append(
            f"{i}. TITLE: {result.title}\n   SNIPPET: {result.snippet}\n   URL: {result.url}"
        )
    return "\n\n".join(lines)


async def _validate_search_results(
    prospect: ProspectCandidate,
    results: list[SearchResult],
    llm_provider: OpenAIChatProvider,
) -> ContactCandidate | None:
    """Step D: one LLM validation call over all step-C results combined.
    Returns a MEDIUM-confidence ContactCandidate, or None."""
    if not results:
        return None

    prompt = _SEARCH_VALIDATION_INSTRUCTIONS.format(
        project_name=_project_name(prospect),
        project_url=prospect.url,
        results_block=_format_results_block(results),
    )
    raw_text = await _call_llm(llm_provider, prompt)
    if raw_text is None:
        return None

    parsed = _parse_json_object(raw_text)
    if parsed is None or parsed.get("found") is not True:
        return None

    display_name = _sanitize_text(parsed.get("display_name"), 120)
    role = _sanitize_text(parsed.get("role"), 120)
    evidence_text = _sanitize_text(parsed.get("evidence_text"), 300)
    evidence_url = _sanitize_text(parsed.get("evidence_url"), 500)
    if not display_name or not role or not evidence_text or not evidence_url:
        return None
    # evidence_url must be one of the URLs actually returned by search -
    # the LLM must never be able to introduce a URL of its own (see module
    # docstring: "fail-closed by construction").
    if evidence_url not in {result.url for result in results}:
        return None

    return ContactCandidate(
        contact_type=ContactType.PERSON,
        display_name=display_name,
        role=role,
        project_name=_project_name(prospect),
        public_url=prospect.url,
        evidence_text=evidence_text,
        evidence_url=evidence_url,
        confidence=ContactConfidence.MEDIUM,
        provenance="targeted_search",
    )


def _professional_queries(project_name: str) -> list[str]:
    return [f'"{project_name}" {suffix}' for suffix in _PROFESSIONAL_ROLE_SUFFIXES][
        :_MAX_PROFESSIONAL_QUERIES
    ]


def _run_professional_searches(
    provider: WebSearchProvider, project_name: str,
) -> list[SearchResult]:
    """Step E's search pass - same shape/limits as step C's
    _run_targeted_searches (up to _MAX_PROFESSIONAL_QUERIES queries, each
    capped at _PROFESSIONAL_SEARCH_LIMIT results), just with
    _PROFESSIONAL_ROLE_SUFFIXES instead of _ROLE_SUFFIXES. Scoped to the
    resource by construction - every query already contains the project's
    own (quoted) name, same as step C."""
    results: list[SearchResult] = []
    for query in _professional_queries(project_name):
        try:
            response = provider.search(query, limit=_PROFESSIONAL_SEARCH_LIMIT)
        except Exception:
            log.warning("contact_discovery: professional search failed", exc_info=True)
            continue
        if response is not None:
            results.extend(response.results)
    return _normalize_search_results(results)


async def _validate_professional_search_results(
    prospect: ProspectCandidate,
    results: list[SearchResult],
    llm_provider: OpenAIChatProvider,
) -> ContactCandidate | None:
    """Step E's validation call - same fail-closed shape as
    _validate_search_results (step D), with the professional-vs-owner
    prompt and role vocabulary. Returns a MEDIUM-confidence
    ContactCandidate, or None. Reuses the existing ContactCandidate shape
    unchanged (no new fields)."""
    if not results:
        return None

    prompt = _PROFESSIONAL_SEARCH_VALIDATION_INSTRUCTIONS.format(
        project_name=_project_name(prospect),
        project_url=prospect.url,
        results_block=_format_results_block(results),
    )
    raw_text = await _call_llm(llm_provider, prompt)
    if raw_text is None:
        return None

    parsed = _parse_json_object(raw_text)
    if parsed is None or parsed.get("found") is not True:
        return None

    display_name = _sanitize_text(parsed.get("display_name"), 120)
    role = _sanitize_text(parsed.get("role"), 120)
    evidence_text = _sanitize_text(parsed.get("evidence_text"), 300)
    evidence_url = _sanitize_text(parsed.get("evidence_url"), 500)
    if not display_name or not role or not evidence_text or not evidence_url:
        return None
    # evidence_url must be one of the URLs actually returned by search -
    # same anti-hallucination guard as step D.
    if evidence_url not in {result.url for result in results}:
        return None

    return ContactCandidate(
        contact_type=ContactType.PERSON,
        display_name=display_name,
        role=role,
        project_name=_project_name(prospect),
        public_url=prospect.url,
        evidence_text=evidence_text,
        evidence_url=evidence_url,
        confidence=ContactConfidence.MEDIUM,
        provenance="professional_search",
    )


async def find_contact(
    prospect: ProspectCandidate,
    *,
    llm_provider: OpenAIChatProvider | None,
    web_search_provider: WebSearchProvider | None,
) -> ContactCandidate:
    """Entry point. Always returns a ContactCandidate - never None, never
    raises past this function (every failure mode - missing provider, fetch
    failure, LLM exception/invalid response - resolves to the safe
    ContactType.PROJECT fallback pointing at the prospect's own page)."""
    if llm_provider is None or not llm_provider.config.api_key:
        return _project_fallback(prospect, provenance="no_llm_provider")

    try:
        page = await asyncio.to_thread(fetch_public_source_sync, prospect.url)
    except PublicSourceFetchError:
        page = None

    if page is not None:
        found = await _extract_from_page(prospect, page, llm_provider)
        if found is not None:
            return found

    if web_search_provider is None:
        return _project_fallback(prospect, provenance="person_not_found")

    results = _run_targeted_searches(web_search_provider, _project_name(prospect))
    found = await _validate_search_results(prospect, results, llm_provider)
    if found is not None:
        return found

    # Step E: owner-contact flow (A-D) found nobody. Only for a prospect
    # that explicitly self-describes as a community/directory/network (see
    # _looks_like_community_directory), try once more for a professional
    # INSIDE it, not its owner - a separate, equally narrow (max 3 query)
    # search pass. An ordinary single-owner business just falls through to
    # the same PROJECT fallback as before this step existed.
    if _looks_like_community_directory(prospect):
        professional_results = _run_professional_searches(
            web_search_provider, _project_name(prospect),
        )
        professional_found = await _validate_professional_search_results(
            prospect, professional_results, llm_provider,
        )
        if professional_found is not None:
            return professional_found

    return _project_fallback(prospect, provenance="person_not_found")
