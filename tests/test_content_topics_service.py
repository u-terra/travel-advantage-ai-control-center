from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.domain.conversation_state import OfferItem
from app.repositories.conversation_state_repository import ConversationStateRepository
from app.services.content_topics_service import ContentTopicsService
from app.services.llm.models import ContentTopic, ContentTopicsResult
from tests.llm_fakes import FakeLLMProvider

WORKSPACE_A = 1
USER_A = 586249067


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _repository(tmp_path: Path) -> ConversationStateRepository:
    repository = ConversationStateRepository(tmp_path / "journal.sqlite3")
    _run(repository.init())
    return repository


def _topics() -> ContentTopicsResult:
    return ContentTopicsResult(topics=(
        ContentTopic(id="1", title="Тема раз", angle="История", reason="Актуально"),
        ContentTopic(id="2", title="Тема два", angle="Еда", reason="Сезонно"),
        ContentTopic(id="3", title="Тема три", angle="Природа", reason="Визуально"),
    ))


def test_propose_and_offer_creates_pending_offer_from_topics(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    provider = FakeLLMProvider(topics=_topics())
    service = ContentTopicsService(provider, repository)

    offer = _run(service.propose_and_offer(
        WORKSPACE_A, USER_A, source_text="Предложи три темы для поста", count=3,
    ))

    assert offer is not None
    assert offer.offer_type == "content_topics"
    assert [item.id for item in offer.items] == ["1", "2", "3"]
    assert offer.items[0].label == "Тема раз"
    assert offer.items[0].payload == {"angle": "История", "reason": "Актуально"}
    provider.propose_content_topics.assert_called_once_with(
        source_text="Предложи три темы для поста", count=3,
    )


def test_propose_and_offer_returns_none_when_provider_fails(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    provider = FakeLLMProvider(topics=None)
    service = ContentTopicsService(provider, repository)

    result = _run(service.propose_and_offer(WORKSPACE_A, USER_A, source_text="x", count=3))

    assert result is None
    assert _run(repository.get_active_offer(WORKSPACE_A, USER_A, "content_topics")) is None


def test_propose_and_offer_calls_provider_exactly_once(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    provider = FakeLLMProvider(topics=_topics())
    service = ContentTopicsService(provider, repository)

    _run(service.propose_and_offer(WORKSPACE_A, USER_A, source_text="x", count=3))

    assert provider.propose_content_topics.call_count == 1


def test_propose_and_offer_replaces_previous_content_topics_offer(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    old_items = (OfferItem(id="a", label="Старая тема", payload={}),)
    old_offer = _run(repository.create_offer(WORKSPACE_A, USER_A, "content_topics", old_items))
    provider = FakeLLMProvider(topics=_topics())
    service = ContentTopicsService(provider, repository)

    new_offer = _run(service.propose_and_offer(WORKSPACE_A, USER_A, source_text="x", count=3))

    assert new_offer is not None
    assert new_offer.id != old_offer.id
    active = _run(repository.get_active_offer(WORKSPACE_A, USER_A, "content_topics"))
    assert active is not None
    assert active.id == new_offer.id


def test_propose_and_offer_does_not_touch_an_active_radar_offer(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    radar_items = (OfferItem(id="7", label="Идея Radar", payload={}),)
    radar_offer = _run(
        repository.create_offer(WORKSPACE_A, USER_A, "radar_content_ideas", radar_items)
    )
    provider = FakeLLMProvider(topics=_topics())
    service = ContentTopicsService(provider, repository)

    _run(service.propose_and_offer(WORKSPACE_A, USER_A, source_text="x", count=3))

    active_radar = _run(
        repository.get_active_offer(WORKSPACE_A, USER_A, "radar_content_ideas")
    )
    assert active_radar is not None
    assert active_radar.id == radar_offer.id


def test_propose_and_offer_is_workspace_and_user_scoped(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    provider = FakeLLMProvider(topics=_topics())
    service = ContentTopicsService(provider, repository)

    _run(service.propose_and_offer(WORKSPACE_A, USER_A, source_text="x", count=3))

    assert _run(repository.get_active_offer(2, USER_A, "content_topics")) is None
    assert _run(repository.get_active_offer(WORKSPACE_A, 999, "content_topics")) is None


# ── resolve_offer_item (L/M) ─────────────────────────────────────────────


def test_resolve_offer_item_returns_the_exact_matching_item(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    provider = FakeLLMProvider(topics=_topics())
    service = ContentTopicsService(provider, repository)
    offer = _run(service.propose_and_offer(WORKSPACE_A, USER_A, source_text="x", count=3))
    assert offer is not None

    item = _run(service.resolve_offer_item(WORKSPACE_A, USER_A, offer.id, "2"))

    assert item is not None
    assert item.id == "2"
    assert item.label == "Тема два"


def test_resolve_offer_item_rejects_wrong_offer_id(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    provider = FakeLLMProvider(topics=_topics())
    service = ContentTopicsService(provider, repository)
    offer = _run(service.propose_and_offer(WORKSPACE_A, USER_A, source_text="x", count=3))
    assert offer is not None

    assert _run(
        service.resolve_offer_item(WORKSPACE_A, USER_A, offer.id + 999, "1")
    ) is None


def test_resolve_offer_item_rejects_unknown_item_id(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    provider = FakeLLMProvider(topics=_topics())
    service = ContentTopicsService(provider, repository)
    offer = _run(service.propose_and_offer(WORKSPACE_A, USER_A, source_text="x", count=3))
    assert offer is not None

    assert _run(service.resolve_offer_item(WORKSPACE_A, USER_A, offer.id, "999")) is None


def test_resolve_offer_item_does_not_leak_a_different_tenants_offer(tmp_path: Path) -> None:
    """M: a foreign workspace/user cannot resolve someone else's offer -
    get_active_offer is itself workspace/user-scoped."""
    repository = _repository(tmp_path)
    provider = FakeLLMProvider(topics=_topics())
    service = ContentTopicsService(provider, repository)
    offer = _run(service.propose_and_offer(WORKSPACE_A, USER_A, source_text="x", count=3))
    assert offer is not None

    other_workspace, other_user = 99, 555
    assert _run(
        service.resolve_offer_item(other_workspace, other_user, offer.id, "1")
    ) is None


def test_resolve_offer_item_returns_none_without_any_active_offer(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    provider = FakeLLMProvider(topics=_topics())
    service = ContentTopicsService(provider, repository)

    assert _run(service.resolve_offer_item(WORKSPACE_A, USER_A, 1, "1")) is None


def test_resolve_offer_item_does_not_resolve_a_different_offer_type(tmp_path: Path) -> None:
    """resolve_offer_item is content_topics-specific - an active Radar offer
    with the same id/item shape must not be resolved through this path."""
    repository = _repository(tmp_path)
    radar_items = (OfferItem(id="1", label="Идея Radar", payload={}),)
    radar_offer = _run(
        repository.create_offer(WORKSPACE_A, USER_A, "radar_content_ideas", radar_items)
    )
    provider = FakeLLMProvider(topics=_topics())
    service = ContentTopicsService(provider, repository)

    assert _run(
        service.resolve_offer_item(WORKSPACE_A, USER_A, radar_offer.id, "1")
    ) is None
