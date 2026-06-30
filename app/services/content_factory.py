from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContentFactoryConfig:
    url: str
    token: str
    timeout_seconds: float

    @property
    def is_configured(self) -> bool:
        return bool(self.url) and bool(self.token)


@dataclass(frozen=True)
class ContentDraft:
    text: str
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class TextSafetyFinding:
    phrase: str
    warning: str


@dataclass(frozen=True)
class TextCheckResult:
    warnings: tuple[TextSafetyFinding, ...]
    rewritten_text: str | None
    rewrite_warnings: tuple[TextSafetyFinding, ...]
    generation_mode: str | None
    ai_note: str | None


def _parse_safety_findings(value: object) -> tuple[TextSafetyFinding, ...]:
    if not isinstance(value, list):
        return ()

    findings: list[TextSafetyFinding] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        phrase = item.get("phrase")
        warning = item.get("warning")
        if (
            isinstance(phrase, str)
            and phrase.strip()
            and isinstance(warning, str)
            and warning.strip()
        ):
            findings.append(
                TextSafetyFinding(
                    phrase=phrase.strip(),
                    warning=warning.strip(),
                )
            )
    return tuple(findings)


def check_text_sync(
    config: ContentFactoryConfig,
    *,
    source_text: str,
) -> Optional[TextCheckResult]:
    """Блокирующий вызов Safety Layer внутри Travel Content Factory."""
    if not config.is_configured:
        return None

    base_url = config.url.rstrip("/")
    if not base_url.endswith("/internal/generate"):
        log.warning("content_factory: unexpected internal endpoint url")
        return None

    check_url = base_url.rsplit("/", 1)[0] + "/check-text"
    payload = json.dumps(
        {"source_text": source_text},
        ensure_ascii=False,
    ).encode("utf-8")

    req = urllib.request.Request(
        check_url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Internal-Token": config.token,
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=config.timeout_seconds) as resp:
            raw = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        log.warning("content_factory: text check request failed")
        return None
    except Exception:
        log.warning("content_factory: unexpected text check failure")
        return None

    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        log.warning("content_factory: invalid text check response payload")
        return None

    if not isinstance(data, dict) or not data.get("ok"):
        return None

    rewritten_text = data.get("rewritten_text")
    if not isinstance(rewritten_text, str) or not rewritten_text.strip():
        rewritten_text = None
    else:
        rewritten_text = rewritten_text.strip()

    generation_mode = data.get("generation_mode")
    if not isinstance(generation_mode, str) or not generation_mode.strip():
        generation_mode = None

    ai_note = data.get("ai_note")
    if not isinstance(ai_note, str) or not ai_note.strip():
        ai_note = None

    return TextCheckResult(
        warnings=_parse_safety_findings(data.get("warnings")),
        rewritten_text=rewritten_text,
        rewrite_warnings=_parse_safety_findings(
            data.get("rewrite_warnings")
        ),
        generation_mode=generation_mode,
        ai_note=ai_note,
    )




def generate_draft_sync(
    config: ContentFactoryConfig,
    *,
    source_text: str,
    material_type: str,
    output_format: str,
    mode: str,
) -> Optional[ContentDraft]:
    """Блокирующий вызов внутреннего API Travel Content Factory.

    Возвращает None при любой ошибке: сеть, timeout, не-2xx, не-JSON, ok!=True,
    пустой текст. Никакие технические детали наружу не пробрасываются.
    Токен не попадает ни в исключения, ни в логи.
    """
    if not config.is_configured:
        return None

    payload = json.dumps(
        {
            "mode": mode,
            "source_text": source_text,
            "material_type": material_type,
            "output_format": output_format,
        },
        ensure_ascii=False,
    ).encode("utf-8")

    req = urllib.request.Request(
        config.url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Internal-Token": config.token,
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=config.timeout_seconds) as resp:
            raw = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        log.warning("content_factory: request failed")
        return None
    except Exception:
        log.warning("content_factory: unexpected request failure")
        return None

    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        log.warning("content_factory: invalid response payload")
        return None

    if not isinstance(data, dict) or not data.get("ok"):
        return None

    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        return None

    raw_warnings = data.get("warnings") or ()
    warnings: tuple[str, ...] = tuple(
        str(w).strip()
        for w in raw_warnings
        if isinstance(w, (str, int, float)) and str(w).strip()
    )
    return ContentDraft(text=text.strip(), warnings=warnings)
