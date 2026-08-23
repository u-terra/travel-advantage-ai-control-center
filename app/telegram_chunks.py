"""Split long results into Telegram-safe message chunks.

Telegram's Bot API rejects a single text message over ~4096 characters
(``TelegramBadRequest: message is too long``). Every handler that can
produce long text (materials, generated drafts) sends the result as
consecutive messages within a safe margin below that hard limit instead of
truncating it.
"""

from __future__ import annotations

CHUNK_SIZE = 3500


def chunk_text(text: str, size: int = CHUNK_SIZE) -> list[str]:
    return [text[index:index + size] for index in range(0, len(text), size)] or [""]
