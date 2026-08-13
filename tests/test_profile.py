from __future__ import annotations

import asyncio
from types import MappingProxyType
from typing import Any
from unittest.mock import AsyncMock

from app.domain.business_profiles import BusinessClaim, BusinessContext, BusinessProfile
from app.domain.partners import WorkspaceContext
from app.handlers.profile import _PROFILE_MISSING, _UNAVAILABLE, show_profile


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _Message:
    def __init__(self) -> None:
        self.answers: list[tuple[str, Any]] = []

    async def answer(self, text: str, reply_markup: Any = None, **kwargs: Any) -> None:
        self.answers.append((text, reply_markup))


def _context(workspace_id: int = 42) -> WorkspaceContext:
    return WorkspaceContext(100, workspace_id, "member", "active")


def _empty_context() -> dict[str, Any]:
    return {
        "specializations": (), "destinations": (), "audiences": (), "markets": (),
        "positioning": MappingProxyType({
            "statement": "", "value_proposition": "", "differentiators": (),
        }),
        "communication": MappingProxyType({
            "tone": "", "formality": "", "emoji_preference": "",
            "preferred_terms": (), "banned_formulations": (), "cta_preference": "",
        }),
        "goals": (),
        "content_preferences": MappingProxyType({
            "formats": (), "channels": (), "preferred_topics": (), "prohibited_topics": (),
        }),
        "public_contacts": MappingProxyType({}),
        "claims": (),
    }


def _profile(
    workspace_id: int = 42, *, status: str = "usable", name: str = "Acme Travel Club",
    full: bool = True,
) -> BusinessProfile:
    context_kwargs = _empty_context()
    if full:
        context_kwargs.update({
            "specializations": ("Круизы", "Авторские туры"),
            "destinations": ("Италия", "Греция"),
            "audiences": ("Семьи с детьми",),
            "markets": ("RU",),
            "positioning": MappingProxyType({
                "statement": "Тёплые поездки без спешки",
                "value_proposition": "Личный подбор маршрута",
                "differentiators": (),
            }),
            "communication": MappingProxyType({
                "tone": "Тёплый и внимательный", "formality": "", "emoji_preference": "",
                "preferred_terms": (), "banned_formulations": (), "cta_preference": "",
            }),
            "goals": ("Рост повторных обращений",),
            "public_contacts": MappingProxyType({"website": "https://acme.example"}),
            "claims": (
                BusinessClaim("10 лет на рынке", "verified", "лицензия", "now", "now"),
                BusinessClaim("Лучшие цены", "unverified", None, "now", None),
            ),
        })
    return BusinessProfile(
        1, workspace_id, name, "agency", "Семейное турагентство полного цикла",
        status, 1, 3, BusinessContext(**context_kwargs), "now", "now",
    )


def _repository(profile: BusinessProfile | None) -> Any:
    return AsyncMock(get_business_profile=AsyncMock(return_value=profile))


def test_profile_visible_for_resolved_workspace() -> None:
    message = _Message()
    repository = _repository(_profile())

    _run(show_profile(message, _context(42), repository))

    repository.get_business_profile.assert_awaited_once_with(42)
    text = message.answers[0][0]
    assert "Acme Travel Club" in text
    assert "Круизы" in text and "Италия" in text
    assert "Тёплый и внимательный" in text
    assert "10 лет на рынке" in text
    assert "Лучшие цены" not in text  # unverified claim не показываем как факт
    assert "персонализировать материалы" in text


def test_profile_lookup_is_scoped_to_caller_workspace() -> None:
    """Профиль запрашивается ровно для workspace_id из контекста вызывающего."""
    first_message, second_message = _Message(), _Message()
    first_repository = _repository(_profile(42, name="Workspace A"))
    second_repository = _repository(_profile(43, name="Workspace B"))

    _run(show_profile(first_message, _context(42), first_repository))
    _run(show_profile(second_message, _context(43), second_repository))

    first_repository.get_business_profile.assert_awaited_once_with(42)
    second_repository.get_business_profile.assert_awaited_once_with(43)
    assert "Workspace A" in first_message.answers[0][0]
    assert "Workspace B" not in first_message.answers[0][0]
    assert "Workspace B" in second_message.answers[0][0]
    assert "Workspace A" not in second_message.answers[0][0]


def test_profile_missing_shows_administrator_message() -> None:
    message = _Message()
    repository = _repository(None)

    _run(show_profile(message, _context(42), repository))

    assert message.answers[0][0] == _PROFILE_MISSING


def test_profile_unavailable_without_workspace_context() -> None:
    message = _Message()
    repository = _repository(_profile())

    _run(show_profile(message, None, repository))

    assert message.answers[0][0] == _UNAVAILABLE
    repository.get_business_profile.assert_not_awaited()


def test_profile_does_not_leak_internal_fields() -> None:
    message = _Message()
    repository = _repository(_profile(42))

    _run(show_profile(message, _context(42), repository))

    text = message.answers[0][0].lower()
    for forbidden in ("workspace_id", "revision", "schema_version", "\"id\":"):
        assert forbidden not in text


def test_profile_skips_empty_sections() -> None:
    message = _Message()
    repository = _repository(_profile(42, full=False))

    _run(show_profile(message, _context(42), repository))

    text = message.answers[0][0]
    assert "Специализации:" not in text
    assert "Направления:" not in text
    assert "Контакты:" not in text
    assert "Подтверждённые факты:" not in text


def test_profile_notes_incomplete_status() -> None:
    message = _Message()
    repository = _repository(_profile(42, status="incomplete"))

    _run(show_profile(message, _context(42), repository))

    assert "персонализация пока ограничена" in message.answers[0][0].lower()
