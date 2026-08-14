from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.repositories.competitor_repository import (
    CompetitorAddressError,
    CompetitorRepository,
)
from app.repositories.partner_repository import PartnerRepository, empty_business_context


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _two_workspaces(tmp_path: Path) -> tuple[Path, int, int]:
    """Два реальных, независимых workspace в одной БД: TA (owner) и сторонний
    партнёр, созданный через тот же provisioning-путь, что и в проде."""
    db_path = tmp_path / "workspace.sqlite3"
    partners = PartnerRepository(db_path)
    _run(partners.init())

    ta_workspace, _ = _run(partners.ensure_owner_workspace(586249067))

    context = empty_business_context()
    context["specializations"] = ["cruises"]
    provisioned = _run(partners.provision_partner(
        111222333, "Independent Agency", "independent-agency",
        business_name="Independent Agency",
        business_type="independent_agent",
        short_description="Сторонний тревел-агент.",
        context=context,
    ))

    return db_path, ta_workspace.id, provisioned.workspace.id


def test_competitor_added_in_one_workspace_is_invisible_in_another(
    tmp_path: Path,
) -> None:
    """Изоляция «Мои конкуренты»: конкурент, добавленный в workspace A, не
    виден в workspace B — и наоборот."""
    db_path, workspace_a, workspace_b = _two_workspaces(tmp_path)
    repository = CompetitorRepository(db_path)
    _run(repository.init())

    _run(repository.add_competitor(workspace_a, "https://competitor-a.example.com"))
    _run(repository.add_competitor(workspace_b, "https://competitor-b.example.com"))

    listed_a = _run(repository.list_for_workspace(workspace_a))
    listed_b = _run(repository.list_for_workspace(workspace_b))

    assert [c.url for c in listed_a] == ["https://competitor-a.example.com"]
    assert [c.url for c in listed_b] == ["https://competitor-b.example.com"]
    assert all(c.workspace_id == workspace_a for c in listed_a)
    assert all(c.workspace_id == workspace_b for c in listed_b)


def test_empty_workspace_has_no_competitors(tmp_path: Path) -> None:
    db_path, workspace_a, workspace_b = _two_workspaces(tmp_path)
    repository = CompetitorRepository(db_path)
    _run(repository.init())

    _run(repository.add_competitor(workspace_a, "https://competitor-a.example.com"))

    assert _run(repository.list_for_workspace(workspace_b)) == []


@pytest.mark.parametrize(
    "address",
    ["", "   ", "not-a-url", "ftp://competitor.example.com", "competitor.example.com"],
)
def test_add_competitor_rejects_invalid_address(
    tmp_path: Path, address: str,
) -> None:
    db_path, workspace_a, _ = _two_workspaces(tmp_path)
    repository = CompetitorRepository(db_path)
    _run(repository.init())

    with pytest.raises(CompetitorAddressError):
        _run(repository.add_competitor(workspace_a, address))
