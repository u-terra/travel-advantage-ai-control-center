"""Domain model for files attached to a web-Ассистент message (images, PDFs,
plain text/markdown). A thin metadata record only - the actual bytes live on
disk under AttachmentStorage's root, never in this DB row and never under
any statically-served directory.

An attachment starts "pending" (message_id is None) right after upload,
before the user has actually sent the message - see
WebAttachmentRepository.create_pending()/attach_to_message(). This mirrors
how the composer works: files are uploaded as soon as they're picked, then
bound to whichever message the user eventually sends (or never, if they
change their mind - see the orphan reaper in app.web_api).
"""

from __future__ import annotations

from dataclasses import dataclass

ATTACHMENT_KINDS = frozenset({"image", "pdf", "text"})


@dataclass(frozen=True)
class WebAttachment:
    id: int
    public_id: str
    workspace_id: int
    telegram_user_id: int
    conversation_id: int
    message_id: int | None
    original_filename: str
    stored_filename: str
    content_type: str
    kind: str
    size_bytes: int
    created_at: str
