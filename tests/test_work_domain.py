from __future__ import annotations

import pytest

from app.domain.work import WorkSubjectValidationError, normalize_subject_name


@pytest.mark.parametrize(
    "name",
    ["Иван", "иван", " ИВАН ", "  Иван  ", "Иван\t"],
)
def test_normalize_subject_name_dedupes_case_and_whitespace_variants(name: str) -> None:
    assert normalize_subject_name(name) == "иван"


def test_normalize_subject_name_collapses_internal_whitespace() -> None:
    assert normalize_subject_name("Иван   Петров") == normalize_subject_name("Иван Петров")


@pytest.mark.parametrize("name", ["", "   ", "\t\n"])
def test_normalize_subject_name_rejects_blank(name: str) -> None:
    with pytest.raises(WorkSubjectValidationError):
        normalize_subject_name(name)


def test_normalize_subject_name_rejects_non_string() -> None:
    with pytest.raises(WorkSubjectValidationError):
        normalize_subject_name(None)  # type: ignore[arg-type]
