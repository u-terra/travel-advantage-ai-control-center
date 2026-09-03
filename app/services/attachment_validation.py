"""Pure validation/sniffing for web-Ассистент attachments - no disk or DB
access here, so the security-sensitive part (does this file actually look
like what its extension claims?) is cheap to unit test in isolation.

Deliberately allowlist-only: the six supported extensions below are the
only ones that can ever pass. There is no separate denylist for
SVG/HTML/JS/exe/archives/DOCX/XLSX - they are rejected simply by not being
on the allowlist. Extending to a new type later (e.g. DOCX) means adding
one row here and in _EXPECTED_SNIFF, nothing else in this module changes.

Never trusts the browser's declared Content-Type or the client-supplied
file extension by itself - every binary type is confirmed by sniffing its
magic bytes; every text type must actually decode as UTF-8 text.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_FILES_PER_UPLOAD = 5
MAX_FILE_SIZE_BYTES = 15 * 1024 * 1024
MAX_TOTAL_UPLOAD_BYTES = 40 * 1024 * 1024
MAX_TEXT_FILE_CHARS = 20_000
MAX_DISPLAY_FILENAME_LEN = 150

_UNSUPPORTED_MESSAGE = (
    "Формат файла не поддерживается. Разрешены: JPG, PNG, WEBP, PDF, TXT, MD."
)

# extension -> (kind, canonical content_type)
_EXTENSION_RULES: dict[str, tuple[str, str]] = {
    "jpg": ("image", "image/jpeg"),
    "jpeg": ("image", "image/jpeg"),
    "png": ("image", "image/png"),
    "webp": ("image", "image/webp"),
    "pdf": ("pdf", "application/pdf"),
    "txt": ("text", "text/plain"),
    "md": ("text", "text/markdown"),
}

# extension -> the magic-byte "family" _sniff_binary_kind() must report.
# jpg/jpeg share one family since they're the same format.
_EXPECTED_SNIFF: dict[str, str] = {
    "jpg": "jpg", "jpeg": "jpg", "png": "png", "webp": "webp", "pdf": "pdf",
}


class AttachmentValidationError(ValueError):
    """Base class - callers that don't need to distinguish reasons can
    catch just this."""


class UnsupportedAttachmentTypeError(AttachmentValidationError):
    """Extension isn't on the allowlist at all."""


class InvalidAttachmentError(AttachmentValidationError):
    """Extension is allowed but the actual bytes don't match it (renamed
    file, corrupted upload, binary data claiming to be text, ...)."""


@dataclass(frozen=True)
class AttachmentSniff:
    kind: str
    content_type: str
    extension: str
    text: str | None  # decoded content, only set for kind == "text"


def sanitize_display_filename(raw: str) -> str:
    """Never used to build a filesystem path (see AttachmentStorage) - this
    is purely the human-readable name shown in the UI and stored as
    original_filename. Strips any directory component (both separators, so
    a Windows-style path from a client is handled the same as a POSIX
    one), control characters, and caps the length while keeping the
    extension intact where possible."""
    name = (raw or "").strip().replace("\\", "/")
    name = name.rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    name = name.strip(".") or "file"

    if len(name) <= MAX_DISPLAY_FILENAME_LEN:
        return name

    stem, dot, ext = name.rpartition(".")
    if dot and stem and len(ext) <= 10:
        keep = max(MAX_DISPLAY_FILENAME_LEN - len(ext) - 1, 1)
        return stem[:keep] + "." + ext
    return name[:MAX_DISPLAY_FILENAME_LEN]


def _extract_extension(sanitized_filename: str) -> str:
    if "." not in sanitized_filename:
        return ""
    return sanitized_filename.rsplit(".", 1)[-1].lower()


def _sniff_binary_kind(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith(b"%PDF-"):
        return "pdf"
    return None


def validate_attachment(*, display_filename: str, data: bytes) -> AttachmentSniff:
    """`display_filename` must already be sanitize_display_filename()'d.
    Raises UnsupportedAttachmentTypeError / InvalidAttachmentError with a
    message safe to show the user as-is (no paths, no internals)."""
    extension = _extract_extension(display_filename)
    rule = _EXTENSION_RULES.get(extension)
    if rule is None:
        raise UnsupportedAttachmentTypeError(_UNSUPPORTED_MESSAGE)
    kind, content_type = rule

    if kind == "text":
        if not data:
            raise InvalidAttachmentError(
                f"Файл «{display_filename}» пустой."
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise InvalidAttachmentError(
                f"Не удалось прочитать файл «{display_filename}» — "
                "он повреждён или это не текстовый файл."
            ) from None
        if "\x00" in text:
            raise InvalidAttachmentError(
                f"Файл «{display_filename}» не похож на обычный текстовый файл."
            )
        return AttachmentSniff(
            kind=kind, content_type=content_type, extension=extension, text=text,
        )

    sniffed = _sniff_binary_kind(data)
    if sniffed is None or sniffed != _EXPECTED_SNIFF.get(extension):
        raise InvalidAttachmentError(
            f"Файл «{display_filename}» повреждён или его содержимое "
            "не соответствует расширению."
        )
    return AttachmentSniff(
        kind=kind, content_type=content_type, extension=extension, text=None,
    )
