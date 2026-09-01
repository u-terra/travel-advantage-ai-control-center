from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app.domain.competitor_intelligence import CompetitorIntelligence
from app.repositories.competitor_repository import (
    CompetitorAddressError,
    CompetitorLabelError,
    CompetitorRepository,
)
from app.repositories.partner_repository import PartnerRepository, empty_business_context


def _intelligence(competitor_id: int, analyzed_at: str) -> CompetitorIntelligence:
    return CompetitorIntelligence(
        competitor_id=competitor_id, competitor_label="Test", analyzed_at=analyzed_at,
        positioning=(), products=(), destinations_and_categories=(), promotions=(),
        loyalty_mechanics=(), service_and_ux=(), strengths=(),
        travel_advantage_comparison=(), fresh_signals=(), sources=(), opportunities=(),
    )


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


# ── Stage 3.2: human labels ──────────────────────────────────────────────────


def test_add_competitor_without_label_keeps_old_behavior(tmp_path: Path) -> None:
    db_path, workspace_a, _ = _two_workspaces(tmp_path)
    repository = CompetitorRepository(db_path)
    _run(repository.init())

    competitor = _run(repository.add_competitor(workspace_a, "https://vk.ru/progulkipovolge"))

    assert competitor.label == competitor.url == "https://vk.ru/progulkipovolge"


def test_add_competitor_with_blank_label_keeps_old_behavior(tmp_path: Path) -> None:
    db_path, workspace_a, _ = _two_workspaces(tmp_path)
    repository = CompetitorRepository(db_path)
    _run(repository.init())

    competitor = _run(repository.add_competitor(workspace_a, "https://vk.ru/progulkipovolge", label="   "))

    assert competitor.label == competitor.url


def test_add_competitor_with_label_stores_human_name(tmp_path: Path) -> None:
    db_path, workspace_a, _ = _two_workspaces(tmp_path)
    repository = CompetitorRepository(db_path)
    _run(repository.init())

    competitor = _run(repository.add_competitor(
        workspace_a, "https://vk.ru/progulkipovolge", label="ТурКлуб",
    ))

    assert competitor.label == "ТурКлуб"
    assert competitor.url == "https://vk.ru/progulkipovolge"


def test_update_label_renames_existing_competitor(tmp_path: Path) -> None:
    db_path, workspace_a, _ = _two_workspaces(tmp_path)
    repository = CompetitorRepository(db_path)
    _run(repository.init())

    competitor = _run(repository.add_competitor(workspace_a, "https://vk.ru/progulkipovolge"))
    assert competitor.label == competitor.url  # old-style row, exactly like production today

    updated = _run(repository.update_label(workspace_a, competitor.id, "ТурКлуб"))

    assert updated is not None
    assert updated.id == competitor.id
    assert updated.label == "ТурКлуб"
    assert updated.url == "https://vk.ru/progulkipovolge"  # url untouched


def test_update_label_is_isolated_by_workspace(tmp_path: Path) -> None:
    db_path, workspace_a, workspace_b = _two_workspaces(tmp_path)
    repository = CompetitorRepository(db_path)
    _run(repository.init())

    competitor = _run(repository.add_competitor(workspace_a, "https://competitor-a.example.com"))

    # workspace_b must not be able to rename workspace_a's competitor.
    result = _run(repository.update_label(workspace_b, competitor.id, "Не моё"))

    assert result is None
    listed_a = _run(repository.list_for_workspace(workspace_a))
    assert listed_a[0].label == listed_a[0].url  # unchanged


def test_update_label_unknown_competitor_id_returns_none(tmp_path: Path) -> None:
    db_path, workspace_a, _ = _two_workspaces(tmp_path)
    repository = CompetitorRepository(db_path)
    _run(repository.init())

    result = _run(repository.update_label(workspace_a, 999, "Что угодно"))
    assert result is None


@pytest.mark.parametrize("label", ["", "   "])
def test_update_label_rejects_empty_label(tmp_path: Path, label: str) -> None:
    db_path, workspace_a, _ = _two_workspaces(tmp_path)
    repository = CompetitorRepository(db_path)
    _run(repository.init())
    competitor = _run(repository.add_competitor(workspace_a, "https://competitor-a.example.com"))

    with pytest.raises(CompetitorLabelError):
        _run(repository.update_label(workspace_a, competitor.id, label))


def test_update_label_rejects_overly_long_label(tmp_path: Path) -> None:
    db_path, workspace_a, _ = _two_workspaces(tmp_path)
    repository = CompetitorRepository(db_path)
    _run(repository.init())
    competitor = _run(repository.add_competitor(workspace_a, "https://competitor-a.example.com"))

    with pytest.raises(CompetitorLabelError):
        _run(repository.update_label(workspace_a, competitor.id, "x" * 101))


def test_update_label_collapses_whitespace(tmp_path: Path) -> None:
    db_path, workspace_a, _ = _two_workspaces(tmp_path)
    repository = CompetitorRepository(db_path)
    _run(repository.init())
    competitor = _run(repository.add_competitor(workspace_a, "https://competitor-a.example.com"))

    updated = _run(repository.update_label(workspace_a, competitor.id, "  Тур   Клуб  "))
    assert updated.label == "Тур Клуб"


# ── list_intelligence_dates_for_workspace: cheap last-snapshot lookup for UI ──

def test_intelligence_dates_empty_when_nothing_analyzed_yet(tmp_path: Path) -> None:
    db_path, workspace_a, _ = _two_workspaces(tmp_path)
    repository = CompetitorRepository(db_path)
    _run(repository.init())
    _run(repository.add_competitor(workspace_a, "https://competitor-a.example.com"))

    assert _run(repository.list_intelligence_dates_for_workspace(workspace_a)) == {}


def test_intelligence_dates_returns_analyzed_at_per_competitor(tmp_path: Path) -> None:
    db_path, workspace_a, _ = _two_workspaces(tmp_path)
    repository = CompetitorRepository(db_path)
    _run(repository.init())
    a = _run(repository.add_competitor(workspace_a, "https://competitor-a.example.com"))
    b = _run(repository.add_competitor(workspace_a, "https://competitor-b.example.com"))
    _run(repository.save_intelligence(workspace_a, _intelligence(a.id, "2026-01-01T00:00:00+00:00")))

    dates = _run(repository.list_intelligence_dates_for_workspace(workspace_a))

    assert dates == {a.id: "2026-01-01T00:00:00+00:00"}
    assert b.id not in dates


def test_intelligence_dates_updates_on_re_analysis(tmp_path: Path) -> None:
    db_path, workspace_a, _ = _two_workspaces(tmp_path)
    repository = CompetitorRepository(db_path)
    _run(repository.init())
    a = _run(repository.add_competitor(workspace_a, "https://competitor-a.example.com"))
    _run(repository.save_intelligence(workspace_a, _intelligence(a.id, "2026-01-01T00:00:00+00:00")))
    _run(repository.save_intelligence(workspace_a, _intelligence(a.id, "2026-02-01T00:00:00+00:00")))

    dates = _run(repository.list_intelligence_dates_for_workspace(workspace_a))
    assert dates == {a.id: "2026-02-01T00:00:00+00:00"}


def test_intelligence_dates_picks_max_regardless_of_row_order(tmp_path: Path) -> None:
    """list_intelligence_dates_for_workspace() must return the MAX(analyzed_at)
    per competitor_id, not whatever row SQLite happens to return last.

    competitor_id is a single-column PRIMARY KEY on
    competitor_intelligence_snapshots today, so save_intelligence() can only
    ever leave one row per competitor - true multi-row history isn't
    reachable through the repository's own API. To still verify the
    query's GROUP BY/MAX logic itself (not the schema's uniqueness
    constraint) is what guarantees correctness, this test recreates just
    that one table without the PRIMARY KEY restriction and inserts several
    rows for the SAME competitor out of chronological order.
    """
    db_path, workspace_a, _ = _two_workspaces(tmp_path)
    repository = CompetitorRepository(db_path)
    _run(repository.init())
    competitor = _run(repository.add_competitor(workspace_a, "https://competitor-a.example.com"))

    with sqlite3.connect(db_path) as db:
        db.execute("DROP TABLE competitor_intelligence_snapshots")
        db.execute(
            "CREATE TABLE competitor_intelligence_snapshots ("
            "row_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "competitor_id INTEGER NOT NULL, workspace_id INTEGER NOT NULL, "
            "analyzed_at TEXT NOT NULL, payload_json TEXT NOT NULL)"
        )
        # Insertion order deliberately does NOT match chronological order,
        # and the maximum date is neither the first nor the last row inserted.
        for analyzed_at in (
            "2026-01-15T00:00:00+00:00",
            "2026-03-20T00:00:00+00:00",
            "2026-02-10T00:00:00+00:00",
        ):
            db.execute(
                "INSERT INTO competitor_intelligence_snapshots "
                "(competitor_id, workspace_id, analyzed_at, payload_json) "
                "VALUES (?, ?, ?, '{}')",
                (competitor.id, workspace_a, analyzed_at),
            )
        db.commit()

    dates = _run(repository.list_intelligence_dates_for_workspace(workspace_a))
    assert dates == {competitor.id: "2026-03-20T00:00:00+00:00"}


def test_intelligence_dates_isolated_by_workspace(tmp_path: Path) -> None:
    db_path, workspace_a, workspace_b = _two_workspaces(tmp_path)
    repository = CompetitorRepository(db_path)
    _run(repository.init())
    a = _run(repository.add_competitor(workspace_a, "https://competitor-a.example.com"))
    b = _run(repository.add_competitor(workspace_b, "https://competitor-b.example.com"))
    _run(repository.save_intelligence(workspace_a, _intelligence(a.id, "2026-01-01T00:00:00+00:00")))
    _run(repository.save_intelligence(workspace_b, _intelligence(b.id, "2026-03-01T00:00:00+00:00")))

    assert _run(repository.list_intelligence_dates_for_workspace(workspace_a)) == {
        a.id: "2026-01-01T00:00:00+00:00",
    }
    assert _run(repository.list_intelligence_dates_for_workspace(workspace_b)) == {
        b.id: "2026-03-01T00:00:00+00:00",
    }
