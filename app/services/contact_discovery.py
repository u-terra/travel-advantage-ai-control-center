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
import json
import logging

from app.chat_provider import OpenAIChatProvider
from app.domain.contacts import ContactCandidate, ContactConfidence, ContactType
from app.domain.prospects import ProspectCandidate
from app.planner.fetch import FetchedPublicSource, PublicSourceFetchError, fetch_public_source_sync
from app.services.web_search.base import SearchResult, WebSearchProvider

log = logging.getLogger(__name__)

_MAX_TARGETED_QUERIES = 3
_TARGETED_SEARCH_LIMIT = 3
_ROLE_SUFFIXES = ("основатель", "руководитель", "организатор")

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
    return results


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

    return _project_fallback(prospect, provenance="person_not_found")
