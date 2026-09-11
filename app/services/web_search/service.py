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
from dataclasses import dataclass
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


@dataclass(frozen=True)
class SearchDecision:
    """Result of decide_web_search() - a plain value, kept separate from
    SearchResponse so the "why" is inspectable/testable on its own."""

    should_search: bool
    site: str | None
    matched_category: str | None


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
    if any(marker in lowered for marker in _CHANGEABLE_RULES_MARKERS):
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
        return self._provider.search(query, site=site or decision.site)


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
    "отдельным блоком интерфейса."
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
    lines.append(_RULES)
    return "\n".join(lines).rstrip()
