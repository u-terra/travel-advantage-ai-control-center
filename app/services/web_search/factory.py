"""Builds the one WebSearchService instance a caller needs, from Settings.

Mirrors app.orchestration.factory / app.planner.factory / app.services.llm.factory:
each entry point (app.web_api for Web, app.main for Telegram) builds its own
WebSearchService from the same Settings and the same construction rules, so
Telegram gets an identically-configured, fail-soft instance without a second
WebSearchProvider implementation or a second decision/config path. Provider
selection itself still lives in app.services.web_search.yandex_provider -
this module only wires config -> provider -> service, same shape as
app.web_api._build_web_search_service.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.services.web_search.base import WebSearchProvider
from app.services.web_search.service import WebSearchService
from app.services.web_search.yandex_provider import YandexSearchConfig, YandexSearchProvider

if TYPE_CHECKING:
    from app.config import Settings


def create_web_search_service(config: "Settings") -> WebSearchService:
    """WEB_SEARCH_ENABLED=false (default), an unrecognized
    WEB_SEARCH_PROVIDER, or missing Yandex credentials all fail-soft to a
    provider=None service - maybe_search() then always returns None and the
    caller behaves exactly as it did before this feature existed."""
    provider: WebSearchProvider | None = None
    if config.web_search_enabled and config.web_search_provider == YandexSearchProvider.name:
        yandex_config = YandexSearchConfig(
            api_key=config.yandex_search_api_key,
            folder_id=config.yandex_search_folder_id,
            timeout_seconds=config.yandex_search_timeout_seconds,
            max_results=config.yandex_search_max_results,
        )
        if yandex_config.is_configured:
            provider = YandexSearchProvider(yandex_config)
    return WebSearchService(provider, enabled=config.web_search_enabled)
