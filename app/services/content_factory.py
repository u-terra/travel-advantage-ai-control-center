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
from app.domain.usage import LLMUsage
from app.services.classification_contract import (
    CLASSIFICATION_KEY,
    build_classification_request,
)
from app.services.llm.models import (
    ContentDraft,
    ContentTopic,
    ContentTopicsResult,
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
    "ContentTopic",
    "ContentTopicsResult",
    "SourceAnalysisPayload",
    "TextCheckResult",
    "TextSafetyFinding",
    "analyze_source_sync",
    "check_text_sync",
    "generate_draft_sync",
    "propose_topics_sync",
]


@dataclass(frozen=True)
class ContentFactoryConfig:
    url: str
    token: str
    timeout_seconds: float
    source_analysis_url: str = ""
    # F2D: explicit override for the propose-topics endpoint, same optional-
    # override convention as source_analysis_url above. Empty by default -
    # auto-derived from `url` (see _topics_endpoint), so existing .env files
    # keep working unchanged.
    topics_url: str = ""

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


def _parse_usage(raw: object) -> LLMUsage | None:
    """Usage Cost & Subscription Foundation: Content Factory's own OpenAI
    adapter (/opt/travel_content_factory/ai_providers/openai_provider.py,
    a separate deployment - not this repo) already receives real token
    counts from the underlying Responses API but currently only logs them,
    never returns them. This parses an optional top-level "usage" field
    (sibling to "ok"/"text"/"analysis") the SAME shape OpenAI's own response
    already has (input_tokens/output_tokens), so this side is ready the
    moment that gap is closed - until then `raw` is always None here and
    this returns None, never a guess."""
    if not isinstance(raw, dict):
        return None
    input_tokens = raw.get("input_tokens")
    output_tokens = raw.get("output_tokens")
    total_tokens = raw.get("total_tokens")
    if not isinstance(input_tokens, int):
        input_tokens = None
    if not isinstance(output_tokens, int):
        output_tokens = None
    if not isinstance(total_tokens, int):
        total_tokens = None
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return None
    return LLMUsage(
        input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens,
    )


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
        usage=_parse_usage(data.get("usage")),
    )


def _topics_endpoint(config: ContentFactoryConfig) -> str | None:
    if config.topics_url.strip():
        return config.topics_url.strip()
    parts = urlsplit(config.url.strip())
    if parts.query or parts.fragment:
        return None
    path = parts.path.rstrip("/")
    if path.endswith("/internal/generate"):
        path = path[: -len("/internal/generate")] + "/internal/propose-topics"
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    return None


def propose_topics_sync(
    config: ContentFactoryConfig, *, source_text: str, count: int,
) -> ContentTopicsResult | None:
    """Блокирующий вызов /internal/propose-topics (F2D).

    Returns None on any problem: network/timeout, non-2xx, non-JSON,
    ok != True, wrong topic count, duplicate ids, or any missing/empty
    id/title/angle/reason - same fail-closed convention as
    analyze_source_sync. Never attempts to salvage a partially-valid
    response: a controlled failure here becomes a controlled provider
    error one layer up, not a best-effort guess.
    """
    endpoint = _topics_endpoint(config)
    if not endpoint or not config.token or count <= 0:
        return None

    req = urllib.request.Request(
        endpoint,
        data=json.dumps(
            {"source_text": source_text, "count": count},
            ensure_ascii=False,
        ).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "X-Internal-Token": config.token},
    )
    try:
        with urllib.request.urlopen(req, timeout=config.timeout_seconds) as resp:
            raw = resp.read()
    except Exception:
        log.warning("content_factory: propose topics request failed")
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if type(data) is not dict or data.get("ok") is not True:
        return None

    raw_topics = data.get("topics")
    if type(raw_topics) is not list or len(raw_topics) != count:
        return None

    topics: list[ContentTopic] = []
    seen_ids: set[str] = set()
    for item in raw_topics:
        if type(item) is not dict:
            return None
        topic_id, title, angle, reason = (
            item.get("id"), item.get("title"), item.get("angle"), item.get("reason"),
        )
        if any(
            type(value) is not str or not value.strip()
            for value in (topic_id, title, angle, reason)
        ):
            return None
        topic_id = topic_id.strip()
        if topic_id in seen_ids:
            return None
        seen_ids.add(topic_id)
        topics.append(ContentTopic(
            id=topic_id, title=title.strip(), angle=angle.strip(), reason=reason.strip(),
        ))
    return ContentTopicsResult(topics=tuple(topics))


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
    except urllib.error.HTTPError as exc:
        # Diagnosability fix (production radar:22378/22379): this branch
        # used to log a bare "content_factory: request failed" with no
        # detail, so a real cause (e.g. Content Factory's own HTTP 400
        # "Исходный текст слишком длинный" when source_text exceeds its
        # 6000-char hard cap - see generate_draft's caller for the actual
        # fix) was indistinguishable from a network outage or any other
        # failure. The response BODY here is Content Factory's own error
        # message (never our request/token), safe to log. Truncated - this
        # is a log line, not meant to carry the full payload.
        try:
            body = exc.read()[:300].decode("utf-8", errors="replace")
        except Exception:
            body = ""
        log.warning("content_factory: request failed (HTTP %s): %s", exc.code, body)
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.warning("content_factory: request failed (%s: %s)", type(exc).__name__, exc)
        return None
    except Exception:
        log.exception("content_factory: unexpected request failure")
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
    return ContentDraft(
        text=text.strip(), warnings=warnings, usage=_parse_usage(data.get("usage")),
    )
