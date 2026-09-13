from __future__ import annotations

import asyncio
from pathlib import Path

from app.repositories.web_signal_repository import WebSignalRecord, WebSignalRepository
from tests.test_source_catalog_repository import new_workspace, setup, web_source


def run(value):
    return asyncio.run(value)


def record(
    workspace_id: int, source_id: str, *, title: str = "Title", summary: str = "Summary",
    item_url: str | None = None, source_name: str | None = None,
) -> WebSignalRecord:
    return WebSignalRecord(
        workspace_id=workspace_id, source_id=source_id,
        source_name=source_name or source_id,
        source_url=f"https://example.com/{source_id}",
        item_url=item_url or f"https://example.com/{source_id}",
        title=title, summary=summary, fetched_at="2026-09-13T00:00:00+00:00",
    )


def test_save_then_list_returns_stored_signal_with_provenance(tmp_path: Path) -> None:
    db_path, _, _, owner, _ = setup(tmp_path, [web_source("src-1")])
    repo = WebSignalRepository(db_path)
    run(repo.init())

    run(repo.save_many([record(owner, "src-1")]))

    results = run(repo.list_for_workspace(owner))
    assert len(results) == 1
    result = results[0]
    assert result.id is not None
    assert result.workspace_id == owner
    assert result.source_id == "src-1"
    assert result.source_url == "https://example.com/src-1"
    assert result.item_url == "https://example.com/src-1"
    assert result.title == "Title"
    assert result.summary == "Summary"
    assert result.fetched_at == "2026-09-13T00:00:00+00:00"
    assert result.created_at


def test_repeat_save_upserts_instead_of_duplicating(tmp_path: Path) -> None:
    db_path, _, _, owner, _ = setup(tmp_path, [web_source("src-1")])
    repo = WebSignalRepository(db_path)
    run(repo.init())

    run(repo.save_many([record(owner, "src-1", title="First run")]))
    run(repo.save_many([record(owner, "src-1", title="Second run")]))

    results = run(repo.list_for_workspace(owner))
    assert len(results) == 1
    assert results[0].title == "Second run"


def test_disabled_subscription_hides_already_stored_signal(tmp_path: Path) -> None:
    """A source can be disabled AFTER a signal was collected for it - the
    row stays in storage (history), but must stop being shown, mirroring
    WorkspaceSignalRepository's existing fail-closed visibility rule for
    legacy Radar signals."""
    db_path, _, _, owner, catalog = setup(tmp_path, [web_source("src-1")])
    repo = WebSignalRepository(db_path)
    run(repo.init())
    run(repo.save_many([record(owner, "src-1")]))
    assert len(run(repo.list_for_workspace(owner))) == 1

    run(catalog.set_enabled(owner, "src-1", False))

    assert run(repo.list_for_workspace(owner)) == []


def test_tenant_isolation_same_physical_source_different_workspaces(tmp_path: Path) -> None:
    """Two workspaces subscribed to the SAME physical source (same
    source_id) must each only ever see their own collected signal."""
    db_path, _, _, owner, catalog = setup(tmp_path, [web_source("src-1")])
    other = new_workspace(db_path, "tenant-b")
    run(catalog.add_source(other, "https://example.com/src-1"))

    repo = WebSignalRepository(db_path)
    run(repo.init())
    run(repo.save_many([
        record(owner, "src-1", title="Owner's own signal"),
        record(other, "src-1", title="Other tenant's own signal"),
    ]))

    owner_titles = [r.title for r in run(repo.list_for_workspace(owner))]
    other_titles = [r.title for r in run(repo.list_for_workspace(other))]
    assert owner_titles == ["Owner's own signal"]
    assert other_titles == ["Other tenant's own signal"]
