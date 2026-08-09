"""HTTP-транспорт внутреннего API Travel Content Factory.

Это низкоуровневый клиент, а не бизнес-сервис. Хендлеры его больше не
импортируют: наружу он выставлен через адаптер
``app.services.llm.openai_provider``, который приводит транспорт к общему
интерфейсу ``LLMProvider``. Модели ответа общие для всех провайдеров и лежат
в ``app.services.llm.models``.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit
from dataclasses import dataclass
from typing import Optional

from app.domain.content_intelligence import classification_from_payload
from app.services.classification_contract import (
    CLASSIFICATION_KEY,
    build_classification_request,
)
from app.services.llm.models import (
    ContentDraft,
    SourceAnalysisPayload,
    TextCheckResult,
    TextSafetyFinding,
)

log = logging.getLogger(__name__)

# Модели переехали в app.services.llm.models; имена сохранены здесь для
# совместимости существующих импортов.
__all__ = [
    "ContentDraft",
    "ContentFactoryConfig",
    "SourceAnalysisPayload",
    "TextCheckResult",
    "TextSafetyFinding",
    "analyze_source_sync",
    "check_text_sync",
    "generate_draft_sync",
]


@dataclass(frozen=True)
class ContentFactoryConfig:
    url: str
    token: str
    timeout_seconds: float
    source_analysis_url: str = ""

    @property
    def is_configured(self) -> bool:
        return bool(self.url) and bool(self.token)


_ANALYSIS_KEYS = frozenset({
    "summary", "key_facts", "disputed_claims", "audience_value",
    "target_audiences", "content_angles", "recommended_formats", "warnings",
})


def _analysis_endpoint(config: ContentFactoryConfig) -> str | None:
    if config.source_analysis_url.strip():
        return config.source_analysis_url.strip()
    parts = urlsplit(config.url.strip())
    if parts.query or parts.fragment:
        return None
    path = parts.path.rstrip("/")
    if path.endswith("/internal/generate"):
        path = path[: -len("/internal/generate")] + "/internal/analyze-source"
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    return None


def _string_list(value: object) -> tuple[str, ...] | None:
    if type(value) is not list:
        return None
    if any(type(item) is not str for item in value):
        return None
    return tuple(item.strip() for item in value if item.strip())


def analyze_source_sync(
    config: ContentFactoryConfig, *, source_text: str
) -> SourceAnalysisPayload | None:
    endpoint = _analysis_endpoint(config)
    if not endpoint or not config.token:
        return None
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(
            {
                "source_text": source_text,
                "source_type": "text",
                # Инструкция едет ОТДЕЛЬНЫМ полем от материала. Склеивать их
                # в один текст нельзя: тогда содержимое источника попало бы
                # туда же, где лежат указания модели.
                CLASSIFICATION_KEY: build_classification_request(),
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "X-Internal-Token": config.token},
    )
    try:
        with urllib.request.urlopen(req, timeout=config.timeout_seconds) as resp:
            raw = resp.read()
    except Exception:
        log.warning("content_factory: source analysis request failed")
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if type(data) is not dict or data.get("ok") is not True:
        return None
    analysis = data.get("analysis")
    # Обязательные поля должны быть все; лишние — игнорируются. Раньше здесь
    # стояло точное совпадение набора ключей, и любое расширение ответа
    # роняло разбор источника целиком, хотя текстовая часть была исправна.
    if type(analysis) is not dict or not _ANALYSIS_KEYS <= frozenset(analysis):
        return None
    summary = analysis["summary"]
    audience_value = analysis["audience_value"]
    if type(summary) is not str or not summary.strip():
        return None
    if type(audience_value) is not str or not audience_value.strip():
        return None
    parsed = {key: _string_list(analysis[key]) for key in _ANALYSIS_KEYS - {"summary", "audience_value"}}
    if any(value is None for value in parsed.values()):
        return None
    return SourceAnalysisPayload(
        summary=summary.strip(), audience_value=audience_value.strip(),
        key_facts=parsed["key_facts"] or (),
        disputed_claims=parsed["disputed_claims"] or (),
        target_audiences=parsed["target_audiences"] or (),
        content_angles=parsed["content_angles"] or (),
        recommended_formats=parsed["recommended_formats"] or (),
        warnings=parsed["warnings"] or (),
        # Сломанная или отсутствующая классификация — это None, а не отказ от
        # разбора: текстовый анализ остаётся доступным владельцу.
        classification=classification_from_payload(analysis.get(CLASSIFICATION_KEY)),
    )


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
