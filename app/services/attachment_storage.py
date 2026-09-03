"""Local server-side storage for web-Ассистент attachment bytes.

Layout: <root>/<workspace_id>/<conversation_id>/<random>.<ext> - the
random physical filename comes from WebAttachmentRepository (secrets-based,
never the client's original filename), so path traversal via a crafted
filename is structurally impossible: nothing client-controlled ever
reaches the filesystem path. _safe_component() is still enforced as a
defense-in-depth check in case a caller ever passes something unexpected.

Root lives under the same data/ directory as journal.sqlite3 (see
app.web_api), outside anything served as static files - there is no
StaticFiles mount in this app, and this directory must never become one.
"""

from __future__ import annotations

import asyncio
from pathlib import Path


class AttachmentStorage:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, workspace_id: int, conversation_id: int, stored_filename: str) -> Path:
        safe_name = _safe_component(stored_filename)
        return (
            self.root
            / _safe_component(str(workspace_id))
            / _safe_component(str(conversation_id))
            / safe_name
        )

    async def write(
        self, workspace_id: int, conversation_id: int, stored_filename: str, data: bytes,
    ) -> None:
        path = self._path(workspace_id, conversation_id, stored_filename)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        await asyncio.to_thread(_write)

    async def read(
        self, workspace_id: int, conversation_id: int, stored_filename: str,
    ) -> bytes:
        path = self._path(workspace_id, conversation_id, stored_filename)
        return await asyncio.to_thread(path.read_bytes)

    def delete(self, workspace_id: int, conversation_id: int, stored_filename: str) -> None:
        path = self._path(workspace_id, conversation_id, stored_filename)
        path.unlink(missing_ok=True)


def _safe_component(value: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError("invalid path component")
    return value
