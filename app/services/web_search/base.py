"""Provider-agnostic web search contract.

Mirrors app.services.llm.base.LLMProvider's shape and error policy: business
logic (WebSearchService, app.web_api) depends only on this interface, never
on a concrete vendor. Today's only adapter is Yandex
(app.services.web_search.yandex_provider.YandexSearchProvider); adding a
fallback or a second vendor later means one more adapter class plus one line
in whatever builds WebSearchService - nothing here or in the caller changes.

Boundaries:
- a provider owns transport, timeouts, auth and parsing its own response into
  the models below - it never decides WHETHER to search (that is
  WebSearchService.maybe_search's job) and never touches OpenAI/LLM prompts;
- ``search()`` is a blocking call, same convention as every other provider in
  this codebase (LLMProvider, OpenAIChatProvider) - callers run it via
  ``asyncio.to_thread``;
- any error (network, timeout, malformed response, missing credentials)
  returns ``None`` - never raises, never leaks technical detail or secrets to
  the caller. This is what makes web search fail-soft: the Assistant must
  keep answering without it.

Not tied to FastAPI or Telegram - both channels can depend on this module.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SearchResult:
    """One normalized search hit, vendor fields already mapped away."""

    title: str
    url: str
    snippet: str
    domain: str
    published_at: str | None
    provider: str
    rank: int


@dataclass(frozen=True)
class SearchResponse:
    query: str
    results: list[SearchResult] = field(default_factory=list)
    provider: str = ""
    elapsed_ms: int = 0


class WebSearchProvider(ABC):
    """Provider-agnostic web search contract."""

    #: Identifier echoed into SearchResult.provider/SearchResponse.provider -
    #: matches WEB_SEARCH_PROVIDER for the active adapter.
    name: str = ""

    @abstractmethod
    def search(
        self,
        query: str,
        *,
        site: str | None = None,
        limit: int = 5,
        search_type: str | None = None,
        allow_exceeding_configured_max: bool = False,
    ) -> SearchResponse | None:
        """Run one search call. ``site`` restricts results to one domain when
        the concrete provider supports it (best-effort - a provider that
        cannot restrict by site should just ignore the argument, not fail).
        ``search_type`` optionally overrides the provider's default search
        scope/locale (e.g. Yandex's RU-only default) - same best-effort
        contract as ``site``, only used today by the official-source
        fallback (app.services.web_search.service._ensure_official_source)
        to search a wider scope when the default one found no official
        domain. ``allow_exceeding_configured_max`` lets ``limit`` exceed the
        provider's own configured result cap (e.g. YandexSearchConfig.
        max_results) for this ONE call - same best-effort contract, default
        False preserves every existing caller's behavior unchanged; used
        today only by the same official-source fallback, which needs to see
        further down the ranking than the default cap allows in order to
        find an official domain the default-size search would have missed.
        Returns ``None`` on any error - see module docstring."""
