"""Unit tests for app.services.attachment_validation - the pure sniffing/
sanitization logic behind POST /api/attachments. No DB, no disk, no
FastAPI: real magic-byte and filename-safety edge cases in isolation.
"""

from __future__ import annotations

import pytest

from app.services.attachment_validation import (
    InvalidAttachmentError,
    UnsupportedAttachmentTypeError,
    sanitize_display_filename,
    validate_attachment,
)

_JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 20
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
_WEBP_BYTES = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 20
_PDF_BYTES = b"%PDF-1.7\n" + b"\x00" * 20


# ── sanitize_display_filename: path traversal / control chars ─────────────

def test_sanitize_strips_posix_directory_components():
    assert sanitize_display_filename("../../etc/passwd.jpg") == "passwd.jpg"


def test_sanitize_strips_windows_directory_components():
    assert sanitize_display_filename("C:\\Users\\evil\\..\\..\\file.png") == "file.png"


def test_sanitize_strips_control_characters():
    assert sanitize_display_filename("evil\x00\x01name.txt") == "evilname.txt"


def test_sanitize_falls_back_to_generic_name_when_empty():
    assert sanitize_display_filename("   ") == "file"
    assert sanitize_display_filename("") == "file"
    assert sanitize_display_filename("..") == "file"


def test_sanitize_caps_length_but_keeps_extension():
    long_name = ("a" * 300) + ".png"
    result = sanitize_display_filename(long_name)
    assert len(result) <= 150
    assert result.endswith(".png")


# ── validate_attachment: allowlist + magic-byte sniffing ──────────────────

@pytest.mark.parametrize("filename,data,kind,content_type", [
    ("photo.jpg", _JPEG_BYTES, "image", "image/jpeg"),
    ("photo.jpeg", _JPEG_BYTES, "image", "image/jpeg"),
    ("screen.png", _PNG_BYTES, "image", "image/png"),
    ("screen.webp", _WEBP_BYTES, "image", "image/webp"),
    ("hotel.pdf", _PDF_BYTES, "pdf", "application/pdf"),
])
def test_validate_accepts_matching_binary_types(filename, data, kind, content_type):
    result = validate_attachment(display_filename=filename, data=data)
    assert result.kind == kind
    assert result.content_type == content_type
    assert result.text is None


def test_validate_accepts_txt_and_decodes_text():
    result = validate_attachment(display_filename="notes.txt", data="Привет, мир".encode("utf-8"))
    assert result.kind == "text"
    assert result.text == "Привет, мир"


def test_validate_accepts_md_and_decodes_text():
    result = validate_attachment(display_filename="notes.md", data=b"# Title\nBody")
    assert result.kind == "text"
    assert result.text == "# Title\nBody"


def test_validate_rejects_unsupported_extension():
    with pytest.raises(UnsupportedAttachmentTypeError):
        validate_attachment(display_filename="script.svg", data=b"<svg></svg>")


@pytest.mark.parametrize("filename", [
    "malware.exe", "page.html", "app.js", "archive.zip", "archive.rar",
    "sheet.xlsx", "doc.docx",
])
def test_validate_rejects_denied_extensions(filename):
    with pytest.raises(UnsupportedAttachmentTypeError):
        validate_attachment(display_filename=filename, data=b"anything")


def test_validate_rejects_content_that_does_not_match_extension():
    """A PDF renamed to .png (or any mismatch) must not pass just because
    the extension looks right - the actual bytes are what's trusted."""
    with pytest.raises(InvalidAttachmentError):
        validate_attachment(display_filename="fake.png", data=_PDF_BYTES)


def test_validate_rejects_html_disguised_as_txt():
    """Extension allowlist alone would let this through; the point is
    txt/md just need to be valid UTF-8 text - HTML/script text inside a
    .txt is still just text (never executed, never rendered as HTML - see
    chat.html rendering it via textContent), so this specifically checks
    it doesn't get MISCLASSIFIED as something else, not that HTML-looking
    text is rejected."""
    result = validate_attachment(
        display_filename="page.txt", data=b"<script>alert(1)</script>",
    )
    assert result.kind == "text"
    assert result.text == "<script>alert(1)</script>"


def test_validate_rejects_binary_garbage_claiming_to_be_text():
    with pytest.raises(InvalidAttachmentError):
        validate_attachment(display_filename="notes.txt", data=b"\xff\xfe\x00\x01binary")


def test_validate_rejects_text_with_embedded_nul_byte():
    with pytest.raises(InvalidAttachmentError):
        validate_attachment(display_filename="notes.txt", data=b"hello\x00world")


def test_validate_rejects_empty_text_file():
    with pytest.raises(InvalidAttachmentError):
        validate_attachment(display_filename="empty.txt", data=b"")


def test_validate_rejects_corrupted_jpeg_with_jpg_extension():
    with pytest.raises(InvalidAttachmentError):
        validate_attachment(display_filename="broken.jpg", data=b"not a real jpeg")
