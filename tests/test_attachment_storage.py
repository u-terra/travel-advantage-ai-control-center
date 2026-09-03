"""Unit tests for app.services.attachment_storage.AttachmentStorage - the
on-disk layout and its path-traversal guard, independent of the DB/HTTP
layers above it.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services.attachment_storage import AttachmentStorage


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def storage(tmp_path):
    return AttachmentStorage(tmp_path / "web_uploads")


def test_write_then_read_round_trips(storage) -> None:
    _run(storage.write(1, 2, "abc123.jpg", b"hello world"))

    assert _run(storage.read(1, 2, "abc123.jpg")) == b"hello world"


def test_write_places_file_under_workspace_and_conversation_subdirs(storage) -> None:
    _run(storage.write(42, 99, "token.png", b"data"))

    path = storage._path(42, 99, "token.png")
    assert path.is_file()
    assert path.parent.name == "99"
    assert path.parent.parent.name == "42"


def test_delete_removes_the_file(storage) -> None:
    _run(storage.write(1, 2, "abc.jpg", b"data"))
    path = storage._path(1, 2, "abc.jpg")
    assert path.exists()

    storage.delete(1, 2, "abc.jpg")

    assert not path.exists()


def test_delete_missing_file_does_not_raise(storage) -> None:
    storage.delete(1, 2, "does-not-exist.jpg")  # must not raise


@pytest.mark.parametrize("malicious_name", [
    "../escape.jpg",
    "..\\escape.jpg",
    "sub/dir.jpg",
    "sub\\dir.jpg",
    "..",
    "",
])
def test_path_rejects_traversal_attempts_in_stored_filename(storage, malicious_name) -> None:
    with pytest.raises(ValueError):
        storage._path(1, 2, malicious_name)


def test_read_of_unwritten_file_raises_file_not_found(storage) -> None:
    with pytest.raises(FileNotFoundError):
        _run(storage.read(1, 2, "never-written.jpg"))
