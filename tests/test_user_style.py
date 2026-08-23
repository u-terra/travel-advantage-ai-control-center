"""Stage 3B1: личный стиль пользователя — repository + UserStyleService.

Ключевые гарантии: ключ (workspace_id, telegram_user_id), изоляция между
пользователями одного workspace и между workspace, лимит на 5 примеров.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.domain.partners import WorkspaceContext
from app.repositories.partner_repository import PartnerRepository, TooManyUserExamplesError
from app.services.user_style import UserStyleAccessError, UserStyleService

OWNER_ID = 586249067
MEMBER_ID = 111222333


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _stack(tmp_path: Path) -> tuple[PartnerRepository, int, int]:
    repo = PartnerRepository(tmp_path / "workspace.sqlite3")
    await repo.init()
    owner_membership = await repo.bootstrap_owner_membership(OWNER_ID)
    workspace_id = owner_membership.workspace_id
    await repo.create_membership(workspace_id, MEMBER_ID, role="member")
    return repo, workspace_id, MEMBER_ID


def _ctx(workspace_id: int, telegram_user_id: int, role: str = "owner") -> WorkspaceContext:
    return WorkspaceContext(telegram_user_id, workspace_id, role, "active")


# --- repository: ключ (workspace_id, telegram_user_id) ----------------------

def test_1_style_saved_and_read_back_by_workspace_and_user(tmp_path: Path) -> None:
    repo, workspace_id, _ = _run(_stack(tmp_path))
    _run(repo.set_user_style_description(workspace_id, OWNER_ID, "Пишу с юмором"))
    saved = _run(repo.get_user_preferences(workspace_id, OWNER_ID))
    assert saved.style_description == "Пишу с юмором"
    assert saved.workspace_id == workspace_id
    assert saved.telegram_user_id == OWNER_ID


def test_2_another_user_in_same_workspace_does_not_see_style(tmp_path: Path) -> None:
    repo, workspace_id, member_id = _run(_stack(tmp_path))
    _run(repo.set_user_style_description(workspace_id, OWNER_ID, "Стиль владельца"))

    member_prefs = _run(repo.get_user_preferences(workspace_id, member_id))
    assert member_prefs is None  # у member своей записи ещё нет — не подтягивает чужую

    _run(repo.set_user_style_description(workspace_id, member_id, "Стиль участника"))
    owner_prefs = _run(repo.get_user_preferences(workspace_id, OWNER_ID))
    member_prefs = _run(repo.get_user_preferences(workspace_id, member_id))
    assert owner_prefs.style_description == "Стиль владельца"
    assert member_prefs.style_description == "Стиль участника"


def test_3_another_workspace_does_not_see_examples(tmp_path: Path) -> None:
    repo = PartnerRepository(tmp_path / "workspace.sqlite3")
    _run(repo.init())
    membership_a = _run(repo.bootstrap_owner_membership(OWNER_ID))
    # Второй, полностью независимый workspace/пользователь.
    other_id = 999888777
    provisioned = _run(repo.provision_partner(
        other_id, "Другое агентство", "other-agency",
        business_name="Другое агентство", business_type="agency",
        short_description="d", context={},
    ))

    _run(repo.add_user_example_post(membership_a.workspace_id, OWNER_ID, "Пример A"))
    other_prefs = _run(repo.get_user_preferences(provisioned.workspace.id, other_id))
    assert other_prefs is None  # чужие examples не протекли в другой workspace/пользователя


# --- лимит на 5 примеров -----------------------------------------------------

def test_4_up_to_five_examples_are_saved(tmp_path: Path) -> None:
    repo, workspace_id, _ = _run(_stack(tmp_path))
    result = None
    for i in range(5):
        result = _run(repo.add_user_example_post(workspace_id, OWNER_ID, f"Пример {i}"))
    assert len(result.example_posts) == 5


def test_5_sixth_example_is_rejected(tmp_path: Path) -> None:
    repo, workspace_id, _ = _run(_stack(tmp_path))
    for i in range(5):
        _run(repo.add_user_example_post(workspace_id, OWNER_ID, f"Пример {i}"))
    with pytest.raises(TooManyUserExamplesError):
        _run(repo.add_user_example_post(workspace_id, OWNER_ID, "Пример 6"))
    # Шестая попытка не должна была ничего изменить.
    saved = _run(repo.get_user_preferences(workspace_id, OWNER_ID))
    assert len(saved.example_posts) == 5


def test_one_example_is_enough_no_minimum_required(tmp_path: Path) -> None:
    repo, workspace_id, _ = _run(_stack(tmp_path))
    result = _run(repo.add_user_example_post(workspace_id, OWNER_ID, "Единственный пример"))
    assert result.example_posts == ("Единственный пример",)


# --- avoid_phrases ------------------------------------------------------------

def test_6_avoid_phrases_are_saved(tmp_path: Path) -> None:
    repo, workspace_id, _ = _run(_stack(tmp_path))
    result = _run(repo.set_user_avoid_phrases(
        workspace_id, OWNER_ID, ["уникальное предложение", "  ", "успейте купить"],
    ))
    assert result.avoid_phrases == ("уникальное предложение", "успейте купить")


def test_clearing_examples_preserves_style_and_avoid_phrases(tmp_path: Path) -> None:
    repo, workspace_id, _ = _run(_stack(tmp_path))
    _run(repo.set_user_style_description(workspace_id, OWNER_ID, "Стиль"))
    _run(repo.add_user_example_post(workspace_id, OWNER_ID, "Пример"))
    _run(repo.set_user_avoid_phrases(workspace_id, OWNER_ID, ["штамп"]))

    result = _run(repo.clear_user_example_posts(workspace_id, OWNER_ID))
    assert result.example_posts == ()
    assert result.style_description == "Стиль"
    assert result.avoid_phrases == ("штамп",)


# --- UserStyleService: доступ только к СВОЕЙ записи --------------------------

def test_service_writes_only_to_callers_own_row(tmp_path: Path) -> None:
    repo, workspace_id, member_id = _run(_stack(tmp_path))
    owner_service = UserStyleService(repo)

    _run(owner_service.set_style_description(_ctx(workspace_id, OWNER_ID), "Владелец"))
    _run(owner_service.set_style_description(
        _ctx(workspace_id, member_id, role="member"), "Участник",
    ))

    owner_prefs = _run(repo.get_user_preferences(workspace_id, OWNER_ID))
    member_prefs = _run(repo.get_user_preferences(workspace_id, member_id))
    assert owner_prefs.style_description == "Владелец"
    assert member_prefs.style_description == "Участник"


def test_service_member_role_can_write_own_style_unlike_business_profile(tmp_path: Path) -> None:
    """В отличие от BusinessProfileService (owner/admin-only на запись),
    личный стиль — данные самого пользователя: role='member' тоже может
    редактировать СВОЙ стиль."""
    repo, workspace_id, member_id = _run(_stack(tmp_path))
    service = UserStyleService(repo)
    result = _run(service.set_style_description(
        _ctx(workspace_id, member_id, role="member"), "Мой стиль",
    ))
    assert result.style_description == "Мой стиль"


def test_service_rejects_missing_workspace_context(tmp_path: Path) -> None:
    repo, _, _ = _run(_stack(tmp_path))
    service = UserStyleService(repo)
    with pytest.raises(UserStyleAccessError):
        _run(service.set_style_description(None, "x"))


def test_existing_user_without_preferences_row_returns_none(tmp_path: Path) -> None:
    """Существующие пользователи без personal-style записи (созданные до
    Stage 3B1) продолжают работать как раньше — get() отдаёт None, а не
    ошибку."""
    repo, workspace_id, _ = _run(_stack(tmp_path))
    service = UserStyleService(repo)
    result = _run(service.get(_ctx(workspace_id, OWNER_ID)))
    assert result is None
