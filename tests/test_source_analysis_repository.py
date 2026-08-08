from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.partner_repository import PartnerRepository
from app.repositories.source_analysis_repository import SourceAnalysisRepository
from app.services.llm.models import SourceAnalysisPayload


def run(value): return asyncio.run(value)


def payload():
    return SourceAnalysisPayload("Итог", ("Факт",), (), "Польза", ("Туристы",), ("Угол",), ("Пост",), ())


def setup(tmp_path: Path):
    path = tmp_path / "db.sqlite3"
    partners = PartnerRepository(path); run(partners.init())
    workspace, _ = run(partners.ensure_owner_workspace(1))
    artifacts = ArtifactRepository(path); run(artifacts.init())
    source = run(artifacts.create_source(workspace.id, source_type="text", title="T", original_text="X"))
    repo = SourceAnalysisRepository(path); run(repo.initialize()); run(repo.initialize())
    return path, workspace.id, source, artifacts, repo


def test_success_json_tuple_status_and_idempotent_initialize(tmp_path):
    _, wid, source, artifacts, repo = setup(tmp_path)
    saved = run(repo.save_successful_analysis(wid, source.id, payload()))
    assert saved.key_facts == ("Факт",)
    assert run(repo.get_by_source_id(wid, source.id)) == saved
    assert run(artifacts.get_source(wid, source.id)).status == "analyzed"


def test_tenant_isolation_duplicate_and_new_without_analysis(tmp_path):
    _, wid, source, artifacts, repo = setup(tmp_path)
    assert run(repo.get_by_source_id(wid + 1, source.id)) is None
    with pytest.raises(ValueError): run(repo.save_successful_analysis(wid + 1, source.id, payload()))
    assert run(artifacts.get_source(wid, source.id)).status == "new"
    run(repo.save_successful_analysis(wid, source.id, payload()))
    with pytest.raises(ValueError): run(repo.save_successful_analysis(wid, source.id, payload()))


def test_transaction_rolls_back_insert_when_status_update_fails(tmp_path):
    path, wid, source, artifacts, repo = setup(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TRIGGER fail_analysis_status BEFORE UPDATE OF status ON sources BEGIN SELECT RAISE(ABORT, 'fail'); END")
    with pytest.raises(sqlite3.IntegrityError): run(repo.save_successful_analysis(wid, source.id, payload()))
    assert run(repo.get_by_source_id(wid, source.id)) is None
    assert run(artifacts.get_source(wid, source.id)).status == "new"


@pytest.mark.parametrize("damaged", ['{"not": "a list"}', '["valid", 1]', '"text"'])
def test_corrupted_json_is_not_silently_accepted(tmp_path, damaged):
    path, wid, source, _, repo = setup(tmp_path)
    run(repo.save_successful_analysis(wid, source.id, payload()))
    with sqlite3.connect(path) as db:
        db.execute("UPDATE source_analyses SET key_facts = ? WHERE source_id = ?", (damaged, source.id))
    with pytest.raises(ValueError, match="key_facts"):
        run(repo.get_by_source_id(wid, source.id))
