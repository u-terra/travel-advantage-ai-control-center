"""Partner Discovery 2.0: lead qualification layer on top of
app.services.partner_discovery's existing stateless candidate pipeline.

Only active when Settings.partner_discovery_v2_enabled is True (see
app/config.py, app/handlers/partner_discovery.py, app/web_api.py). With the
flag off, nothing in this module is ever called - v0 behavior is byte-for-
byte unchanged.

``qualify_candidates`` takes the SAME candidate dict shape
app.services.partner_outreach already produces (candidate_to_dict +
enrich_candidates's display_name/reason) and adds: score breakdown,
total_score, confidence, why_fit, hook, risk_flags, evidence (each claim
tied to a source_url - never invented, see app.domain.lead_qualification).

One batched LLM call for the whole batch, same pattern as
app.services.partner_outreach.enrich_candidates: any failure (no provider,
transport error, malformed/incomplete JSON) falls back to a deterministic,
signal-based scorer for the WHOLE batch - never a partial mix, never
raises past this function.

Weak candidates are DROPPED, never padded: qualify_candidates returns at
most ``max_results`` leads, and fewer than that if fewer clear that bar -
see the task's own "2 леда found -> show 2" requirement.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
import socket
import time
import urllib.error
from urllib.parse import urlsplit

from app.chat_provider import OpenAIChatProvider
from app.domain.competitor_discovery import canonical_domain

log = logging.getLogger(__name__)

_QUALIFICATION_MAX_TEXT_LEN = 400

# ── Deterministic fallback scoring tiers (same style as partner_discovery's
# own _TYPE_MARKERS / lead_radar's keyword tiers - no LLM, no invented facts,
# only signals already present on the candidate dict) ─────────────────────
_PROFESSIONAL_RELEVANCE_BY_TYPE: dict[str, int] = {
    "travel_agent": 25,
    "tour_organizer": 22,
    "guide": 18,
    "travel_creator": 15,
    "travel_club": 15,
    "other_travel_project": 5,
}
_COMMERCIAL_POTENTIAL_BY_TYPE: dict[str, int] = {
    "travel_agent": 20,
    "tour_organizer": 18,
    "guide": 12,
    "travel_creator": 10,
    "travel_club": 10,
    "other_travel_project": 3,
}
_AUDIENCE_QUALITY_BY_TYPE: dict[str, int] = {
    "travel_agent": 8,
    "tour_organizer": 10,
    "guide": 10,
    "travel_creator": 15,
    "travel_club": 13,
    "other_travel_project": 3,
}

# A URL on one of these hosts is itself a usable public contact channel
# (Telegram/VK/Instagram) - same multi-tenant domain set partner_discovery
# already treats as one-page-per-identity.
_KNOWN_CHANNEL_DOMAINS: frozenset[str] = frozenset({
    "t.me", "vk.com", "vk.ru", "instagram.com",
})

# Domains that are never a usable per-candidate contact channel on their
# own - same denylist category app.services.partner_discovery already
# excludes as aggregators/listings, plus generic search-result hosts a
# candidate's own URL should never resolve to at this stage.
_NON_CHANNEL_DOMAINS: frozenset[str] = frozenset({
    "tripster.ru", "youtravel.me", "avito.ru", "uslugi.yandex.ru",
    "guidego.ru", "travel.yandex.ru",
})


# ── Contact channel resolution (task: "пользователь не должен сам искать,
# как связаться") - STRICTLY deterministic, never LLM-sourced: every field
# is parsed directly out of the candidate's own already-verified url/
# evidence text (the same two fields app.services.partner_discovery already
# fetched/verified), so a contact can never be invented by the LLM step.
#
# Priority (2026-10-11 fix - a real live case lost a found email/phone
# behind a generic website): Telegram > VK > public email > public phone
# > generic website. A generic root-domain website is deliberately checked
# LAST - it must never suppress a more specific direct channel that is
# already sitting right there in the same evidence text. ─────────────────

_TELEGRAM_URL_RE = re.compile(r"(?:https?://)?t\.me/([A-Za-z0-9_]{3,32})", re.IGNORECASE)
_VK_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?vk\.(?:com|ru)/([A-Za-z0-9_.]{2,64})", re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}")
# Same shape as app.services.partner_outreach._PHONE_RE (RU mobile/landline,
# +7/8 prefix) - a phone explicitly published on the candidate's own public
# page/snippet, not anything scraped from a private source.
_PHONE_RE = re.compile(r"(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}")


def _as_text_haystack(candidate: dict) -> str:
    return f"{candidate.get('url') or ''} {candidate.get('evidence') or ''}"


def _candidate_channels(candidate: dict) -> list[dict]:
    """Every distinct public contact channel findable in the candidate's
    own already-verified url/evidence text, in priority order (best
    first): Telegram > VK > email > phone > generic website. Each entry
    has the same shape as resolve_contact_channel's return value. A
    generic website is deliberately tried LAST so it never shadows a
    more specific channel sitting in the same evidence - the exact live
    bug this ordering fixes (Onvisor: evidence named a Telegram channel
    and an email, candidate's own url was a plain website - the website
    used to win purely by being checked first)."""
    url = candidate.get("url") or ""
    domain = canonical_domain(url) if url else ""
    evidence = candidate.get("evidence") or ""
    channels: list[dict] = []
    seen_values: set[str] = set()

    def _add(entry: dict) -> None:
        key = f"{entry['contact_type']}:{entry['contact_value']}"
        if key not in seen_values:
            seen_values.add(key)
            channels.append(entry)

    # 1) Telegram - the candidate's own URL is a t.me page.
    direct_telegram = _TELEGRAM_URL_RE.search(url)
    if direct_telegram:
        _add({
            "contact_type": "telegram",
            "contact_value": f"@{direct_telegram.group(1)}",
            "contact_url": url,
            "contact_source_url": url,
            "contact_confidence": "high",
        })

    # 2) VK - the candidate's own URL is a vk.com/vk.ru page.
    direct_vk = _VK_URL_RE.search(url)
    if direct_vk:
        _add({
            "contact_type": "vk",
            "contact_value": url,
            "contact_url": url,
            "contact_source_url": url,
            "contact_confidence": "high",
        })

    # 3) Telegram/VK mentioned inside the page's own evidence text (the
    # candidate's main page links out to its Telegram/VK, e.g. a website
    # footer) - still sourced from the SAME already-fetched evidence, just
    # medium confidence since it is one step removed from the URL itself.
    evidence_telegram = _TELEGRAM_URL_RE.search(evidence)
    if evidence_telegram:
        _add({
            "contact_type": "telegram",
            "contact_value": f"@{evidence_telegram.group(1)}",
            "contact_url": f"https://t.me/{evidence_telegram.group(1)}",
            "contact_source_url": url or evidence_telegram.group(0),
            "contact_confidence": "medium",
        })
    evidence_vk = _VK_URL_RE.search(evidence)
    if evidence_vk:
        matched = evidence_vk.group(0)
        contact_url = matched if matched.startswith("http") else f"https://{matched}"
        _add({
            "contact_type": "vk",
            "contact_value": contact_url,
            "contact_url": contact_url,
            "contact_source_url": url or contact_url,
            "contact_confidence": "medium",
        })

    # 4) Public email explicitly present in the page's own evidence text -
    # checked BEFORE the generic website fallback (see function docstring).
    email_match = _EMAIL_RE.search(evidence)
    if email_match:
        _add({
            "contact_type": "email",
            "contact_value": email_match.group(0),
            "contact_url": "",
            "contact_source_url": url or "",
            "contact_confidence": "medium",
        })

    # 5) Public phone explicitly present in the page's own evidence text -
    # same priority tier as email (both "a direct way to reach someone"),
    # also checked before the generic website fallback.
    phone_match = _PHONE_RE.search(evidence)
    if phone_match:
        _add({
            "contact_type": "phone",
            "contact_value": phone_match.group(0),
            "contact_url": "",
            "contact_source_url": url or "",
            "contact_confidence": "medium",
        })

    # 6) Official website - the candidate's own URL, as long as it is not
    # a known aggregator/listing/multi-tenant domain (those are never a
    # single candidate's own channel). Last resort: a bare root domain is
    # never treated as equivalent to an already-found direct channel.
    if url and domain and domain not in _NON_CHANNEL_DOMAINS and domain not in _KNOWN_CHANNEL_DOMAINS:
        _add({
            "contact_type": "website",
            "contact_value": domain,
            "contact_url": url,
            "contact_source_url": url,
            "contact_confidence": "low",
        })

    return channels


def resolve_contact_channel(candidate: dict) -> dict | None:
    """Returns the single BEST channel (same shape as each entry in
    _candidate_channels) or None if nothing could be confirmed. See
    _candidate_channels for the full priority order and anti-shadowing
    rationale. contact_source_url is always one of the candidate's own
    already-verified fields - never invented, never fetched fresh."""
    channels = _candidate_channels(candidate)
    return channels[0] if channels else None


def resolve_additional_contacts(candidate: dict, primary: dict | None) -> list[dict]:
    """Up to 2 more distinct channels beyond the primary one (task: show
    up to 2-3 total, e.g. Telegram + email + website, never duplicates) -
    same strictly-deterministic source as resolve_contact_channel, never
    LLM-sourced."""
    channels = _candidate_channels(candidate)
    if primary is not None:
        channels = [c for c in channels if c["contact_type"] != primary.get("contact_type")
                    or c["contact_value"] != primary.get("contact_value")]
    return channels[:2]


_CONTACTABILITY_BY_CONFIDENCE: dict[str, int] = {"high": 10, "medium": 7, "low": 4}

# Hard exclusion (2026-10-11 smoke-test finding): a candidate the system
# itself already flagged as one of these is never a genuine lead,
# regardless of how high its score/confidence otherwise comes out - a
# real smoke test surfaced a lead tagged BOTH "competitor_club" and
# "aggregator" that still passed min_score/min_confidence and got shown
# to the user. These flags are a hard gate, checked before min_score/
# min_confidence, never overridden by a high score.
_HARD_EXCLUSION_RISK_FLAGS: frozenset[str] = frozenset({
    "competitor_club", "aggregator", "government", "news_portal",
    "large_corporation_without_person", "spam_profile",
})


def _fallback_score_one(candidate: dict) -> dict:
    prospect_type = candidate.get("prospect_type", "other_travel_project")
    evidence_source = candidate.get("evidence_source", "snippet")
    rank_score = int(candidate.get("rank_score", 0) or 0)
    url = (candidate.get("url") or "").lower()

    professional_relevance = _PROFESSIONAL_RELEVANCE_BY_TYPE.get(prospect_type, 0)
    # No publish-date signal is available at this stage (v0 never extracted
    # one) - page-verified evidence is the only activity proxy we actually
    # have, so fallback activity stays conservative rather than guessing.
    activity = 10 if evidence_source == "page" else 5
    commercial_potential = _COMMERCIAL_POTENTIAL_BY_TYPE.get(prospect_type, 0)
    audience_quality = _AUDIENCE_QUALITY_BY_TYPE.get(prospect_type, 0)
    has_known_channel = any(domain in url for domain in _KNOWN_CHANNEL_DOMAINS)
    contactability = 10 if has_known_channel else (5 if url else 0)
    # No LLM-extracted personal hook in the fallback path - only a generic
    # topical tie (rank_score>0 means keyword markers matched), never a
    # fabricated "specific recent post" claim.
    personal_hook_score = 5 if rank_score > 0 else 0

    why_fit: list[str] = []
    if professional_relevance >= 15:
        why_fit.append("Профессиональная связь с путешествиями подтверждена публичными данными.")
    if has_known_channel:
        why_fit.append("Есть доступный публичный канал для связи.")
    if evidence_source == "page":
        why_fit.append("Информация подтверждена собственной страницей, а не только поисковым сниппетом.")
    if not why_fit:
        why_fit.append("Связь с путешествиями видна только по ключевым словам поискового результата.")

    risk_flags: list[str] = []
    if evidence_source != "page":
        risk_flags.append("insufficient_evidence")

    evidence = []
    excerpt = (candidate.get("evidence") or "")[:_QUALIFICATION_MAX_TEXT_LEN]
    if excerpt and url:
        evidence.append({
            "claim": excerpt,
            "source_url": candidate["url"],
            "source_title": candidate.get("display_name") or candidate.get("name") or "",
        })

    return {
        "professional_relevance": professional_relevance,
        "activity": activity,
        "commercial_potential": commercial_potential,
        "audience_quality": audience_quality,
        "contactability": contactability,
        "personal_hook_score": personal_hook_score,
        "why_fit": why_fit[:4],
        "hook": "",  # fallback never invents a personal hook sentence
        "risk_flags": risk_flags,
        "confidence": 0.5 if evidence_source == "page" else 0.35,
        # "evidence_claims", never "evidence" - the candidate dict already
        # has an "evidence" key (the raw page/snippet text consumed by
        # app.services.partner_outreach.build_explanation/dict_to_prospect)
        # that a {**candidate, **qualification} merge must never overwrite.
        "evidence_claims": evidence,
    }


def apply_fallback_qualification(candidates: list[dict]) -> list[dict]:
    result = []
    for candidate in candidates:
        scored = _fallback_score_one(candidate)
        result.append({**candidate, **scored})
    return result


# ── LLM qualification: ONE call for the whole batch ────────────────────────

_QUALIFICATION_INSTRUCTIONS = """Ты оцениваешь кандидатов для Partner Discovery - внутреннего инструмента поиска потенциальных деловых контактов в travel-индустрии. Это разовая техническая задача, не диалог - ответь только данными.

ЦЕЛЕВОЙ ПРОФИЛЬ (ICP) - читай перед оценкой каждого кандидата:
Мы ищем НЕ просто "travel-бизнес", а в первую очередь: конкретного человека; автора путешествий; гида; travel-блогера; организатора экспедиций; владельца/основателя небольшой travel-команды; небольшое профессиональное сообщество с понятным публичным представителем; проект с собственной аудиторией/каналом и реальной возможностью личного контакта.
НЕ приоритет (и часто повод для hard-exclusion risk_flags ниже): крупные туроператоры, агрегаторы/маркетплейсы, большие корпоративные travel-бренды, компании без конкретного публичного person-level контакта. Наличие travel-продукта САМО ПО СЕБЕ не означает partner fit - важно, есть ли доступный человек/владелец/автор.

Калибровочные примеры (используй как образец строгости, не копируй дословно):
- Маленький авторский туроператор с публичным именем владельца/автора, собственным каналом - ХОРОШИЙ кандидат, "aggregator"/"large_corporation_without_person" НЕ ставить только за то, что это "туроператор" - жёсткое правило "любой туроператор = плохо" запрещено.
- Сайт/страница, где явно перечислены МНОГО разных независимых туроператоров/гидов как единый каталог/маркетплейс (например "выбрать из N туроператоров", агрегатор предложений) -> risk_flags: ["aggregator"], даже если score по остальным пунктам высокий.
- Крупный корпоративный travel-бренд/сеть (большая агентская инфраструктура, сетевой туроператор, франшиза) БЕЗ упоминания конкретного человека/владельца/автора в evidence -> risk_flags: ["large_corporation_without_person"], даже при высоком score - высокий score НЕ оправдание не ставить этот флаг.
- Профессиональное сообщество/гильдия (например гильдия гидов) с понятным публичным представителем - допустимый кандидат, но НЕ выше по professional_relevance/personal_hook_score, чем конкретный автор/организатор/владелец небольшой команды с тем же уровнем evidence - сообщество без явного лидера оценивай чуть консервативнее, чем person-led проект.

Тебе дан список из {count} кандидатов. Для КАЖДОГО верни оценку по следующей модели (все числа - целые, в указанных границах):

- "professional_relevance" (0-25): профессиональная связь с путешествиями И наличие конкретного person-level представителя. 25 - человек/автор/гид/владелец небольшой команды с явной профессиональной travel-деятельностью; 15-20 - активно работает рядом с travel, но менее персонализировано (например community без явного лидера); 5-10 - путешествия лишь часть контента; 0 - связь практически отсутствует.
- "activity" (0-15): 15 - свежие регулярные публикации; 10 - периодически; 5 - низкая активность; 0 - профиль заброшен. Оценивай ТОЛЬКО по предоставленному EVIDENCE - если в evidence нет признаков даты/частоты публикаций, не выдумывай, ставь 5.
- "commercial_potential" (0-20): 20 - уже продаёт/рекомендует услуги; 10-15 - заметна предпринимательская деятельность; 0-5 - обычный пользователь.
- "audience_quality" (0-15): размер И качество аудитории, по тому, что явно видно в evidence.
- "contactability" (0-10): предварительная оценка - она НЕ используется напрямую (система определяет реальный публичный канал связи отдельно, детерминированно, по url/evidence), но всё равно оцени: 10 - очевидный канал связи (Telegram/VK/сайт в URL); 5 - связаться можно, но неудобно; 0 - фактически невозможно.
- "personal_hook_score" (0-15): 15 - в evidence есть конкретный свежий повод (публикация, тур, событие); 10 - есть тематическая зацепка; 5 - только общая релевантность; 0 - нет повода.
- "why_fit": список из 2-4 коротких конкретных аргументов НА РУССКОМ, основанных ТОЛЬКО на предоставленном evidence.
- "hook": ОДНО персонализированное предложение-зацепка для первого сообщения, основанное ТОЛЬКО на конкретном факте из evidence (например, на упомянутом туре/публикации/направлении). Если в evidence нет конкретного повода - верни пустую строку "", НЕ выдумывай повод.
- "risk_flags": список коротких тегов из набора ["inactive", "insufficient_evidence", "mlm_focus", "competitor_club", "aggregator", "government", "news_portal", "large_corporation_without_person", "spam_profile"] - только те, что подтверждаются evidence; иначе пустой список. НЕ указывай контактность здесь - это определяется системой отдельно. Эти теги важны: "competitor_club"/"aggregator"/"government"/"news_portal"/"large_corporation_without_person"/"spam_profile" полностью исключают кандидата из финального списка независимо от остальных баллов - ставь их честно, если они подтверждаются evidence, ВЫСОКИЙ SCORE НЕ ПРИЧИНА не ставить эти флаги - смотри калибровочные примеры выше.
- "confidence" (0.0-1.0): насколько предоставленный evidence ДОСТАТОЧЕН для уверенности в этой оценке. Короткий/неясный evidence -> низкий confidence, даже если сам score высокий.
- "evidence": список объектов {{"claim": "...", "source_url": "..."}} - каждое ключевое утверждение из why_fit/hook должно иметь соответствующий claim с ТЕМ ЖЕ source_url, что был дан во входных данных для этого кандидата. Никогда не добавляй claim без source_url.

СТРОГО ЗАПРЕЩЕНО: придумывать факты, даты, события, публикации, доход, личный опыт человека - всё должно быть прямо выводимо из предоставленного EVIDENCE/TITLE. Если данных недостаточно для какого-то поля - ставь минимальный честный балл, а не додумывай.

Верни СТРОГО валидный JSON-массив из ровно {count} объектов, в ТОМ ЖЕ порядке, что и во входном списке, без markdown, без пояснений. Формат каждого элемента:
{{"professional_relevance": 0, "activity": 0, "commercial_potential": 0, "audience_quality": 0, "contactability": 0, "personal_hook_score": 0, "why_fit": ["..."], "hook": "...", "risk_flags": [], "confidence": 0.0, "evidence": [{{"claim": "...", "source_url": "..."}}]}}"""


def _build_qualification_prompt(candidates: list[dict]) -> str:
    items = []
    for i, candidate in enumerate(candidates, 1):
        items.append(
            f"{i}. URL: {candidate['url']}\n"
            f"   TITLE: {candidate.get('display_name') or candidate.get('name') or '(нет)'}\n"
            f"   Тип: {candidate['prospect_type']}\n"
            f"   Источник evidence: {candidate['evidence_source']}\n"
            f"   EVIDENCE: {candidate['evidence'][:600]}"
        )
    header = _QUALIFICATION_INSTRUCTIONS.format(count=len(candidates))
    return header + "\n\nКандидаты:\n\n" + "\n\n".join(items)


def _coerce_int(value: object, *, max_value: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = int(value)
    if value < 0 or value > max_value:
        return None
    return value


def _coerce_confidence(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not (0.0 <= value <= 1.0):
        return None
    return value


def _parse_qualification_response(
    raw_text: str, candidates: list[dict],
) -> tuple[list[dict] | None, str | None]:
    """Mirrors app.services.partner_outreach._parse_enrichment_response's
    strictness: any shape/range problem -> (None, reason), the caller must
    fall back for the WHOLE batch."""
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None, "invalid_json"
    if not isinstance(parsed, list) or len(parsed) != len(candidates):
        return None, "shape_mismatch"

    valid_urls = {c["url"] for c in candidates}
    result: list[dict] = []
    for item, candidate in zip(parsed, candidates, strict=True):
        if not isinstance(item, dict):
            return None, "item_not_a_dict"
        scores = {
            key: _coerce_int(item.get(key), max_value=max_value)
            for key, max_value in (
                ("professional_relevance", 25),
                ("activity", 15),
                ("commercial_potential", 20),
                ("audience_quality", 15),
                ("contactability", 10),
                ("personal_hook_score", 15),
            )
        }
        if any(v is None for v in scores.values()):
            return None, "score_out_of_range"
        confidence = _coerce_confidence(item.get("confidence"))
        if confidence is None:
            return None, "bad_confidence"
        why_fit = item.get("why_fit")
        if not isinstance(why_fit, list) or not why_fit:
            return None, "bad_why_fit"
        why_fit = [str(w).strip() for w in why_fit if str(w).strip()][:4]
        if not why_fit:
            return None, "bad_why_fit"
        hook = item.get("hook")
        hook = str(hook).strip() if isinstance(hook, str) else ""
        risk_flags_raw = item.get("risk_flags")
        risk_flags = (
            [str(f).strip() for f in risk_flags_raw if str(f).strip()]
            if isinstance(risk_flags_raw, list) else []
        )
        evidence_raw = item.get("evidence")
        evidence: list[dict] = []
        if isinstance(evidence_raw, list):
            for ev in evidence_raw:
                if not isinstance(ev, dict):
                    continue
                claim = str(ev.get("claim") or "").strip()
                source_url = str(ev.get("source_url") or "").strip()
                # Provenance is non-negotiable (task requirement 7): a claim
                # without a matching known source_url is dropped, never kept.
                if not claim or source_url not in valid_urls:
                    continue
                evidence.append({
                    "claim": claim[:_QUALIFICATION_MAX_TEXT_LEN],
                    "source_url": source_url,
                    "source_title": candidate.get("display_name") or "",
                })
        # A hook not grounded in any accepted evidence claim is dropped -
        # never shown as a "personal" hook built on nothing (requirement 9).
        if hook and not evidence:
            hook = ""

        result.append({
            **scores,
            "why_fit": why_fit,
            "hook": hook,
            "risk_flags": risk_flags,
            "confidence": confidence,
            # "evidence_claims", not "evidence" - see apply_fallback_qualification's
            # own comment on why this merge must never clobber candidate["evidence"].
            "evidence_claims": evidence,
        })
    return result, None


# Qualification LLM call: bounded attempts. The provider (app.chat_provider)
# uses plain urllib - no SDK, so there are NO built-in retries underneath; the
# only retry is the one below. A urllib timeout is a socket-idle timeout, and
# the non-streaming Responses call sends nothing until the answer is ready, so
# setting it per attempt really bounds the attempt (no background thread is
# left running past it, unlike an asyncio.wait_for around a thread).
QUALIFICATION_ATTEMPT_TIMEOUT_S = 60.0
QUALIFICATION_MAX_ATTEMPTS = 2
_MIN_ATTEMPT_SECONDS = 20.0


def _is_transient_llm_error(exc: BaseException) -> bool:
    """Timeout / connection-level failure only. HTTP status errors, empty or
    unparseable output are NOT retried."""
    if isinstance(exc, urllib.error.HTTPError):
        return False
    return isinstance(exc, (TimeoutError, socket.timeout, urllib.error.URLError, ConnectionError, OSError))


def _provider_with_timeout(llm_provider, seconds: float):
    config = getattr(llm_provider, "config", None)
    if isinstance(llm_provider, OpenAIChatProvider) and dataclasses.is_dataclass(config):
        return type(llm_provider)(dataclasses.replace(config, timeout_seconds=seconds))
    return llm_provider


async def _generate_with_retry(
    llm_provider, prompt: str, *,
    attempt_timeout_seconds: float | None, max_attempts: int, deadline: float | None,
):
    """Returns the provider result or raises the last error. ``deadline`` is a
    time.monotonic() value for the whole step; an attempt (or a retry) is only
    started with at least _MIN_ATTEMPT_SECONDS left, and never runs past it."""
    model = getattr(getattr(llm_provider, "config", None), "model", "-")
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        timeout = attempt_timeout_seconds
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if attempt > 1 and remaining < _MIN_ATTEMPT_SECONDS:
                log.warning("partner_qualification.retry_skipped reason=budget remaining_s=%.0f", remaining)
                break
            timeout = remaining if timeout is None else min(timeout, remaining)
        started = time.monotonic()
        provider = _provider_with_timeout(llm_provider, timeout) if timeout else llm_provider
        try:
            call = asyncio.to_thread(provider.generate, message=prompt)
            # Backstop only; the provider's own HTTP timeout is the real bound.
            result = await (asyncio.wait_for(call, timeout=timeout + 5) if timeout else call)
        except Exception as exc:
            last_exc = exc
            log.warning(
                "partner_qualification.attempt model=%s input_chars=%d attempt=%d/%d "
                "timeout_s=%s elapsed_ms=%d outcome=error exc_type=%s",
                model, len(prompt), attempt, max_attempts,
                "-" if not timeout else f"{timeout:.0f}",
                int((time.monotonic() - started) * 1000), type(exc).__name__,
            )
            if not _is_transient_llm_error(exc):
                break
            continue
        usage = getattr(result, "usage", None)
        log.info(
            "partner_qualification.attempt model=%s input_chars=%d attempt=%d/%d "
            "timeout_s=%s elapsed_ms=%d outcome=ok output_chars=%d input_tokens=%s output_tokens=%s",
            model, len(prompt), attempt, max_attempts,
            "-" if not timeout else f"{timeout:.0f}",
            int((time.monotonic() - started) * 1000), len(result.text or ""),
            getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None),
        )
        return result
    assert last_exc is not None
    raise last_exc


class QualificationUnavailable(RuntimeError):
    """The LLM qualification step did not complete (no provider, timeout,
    transport error or unparseable answer). Raised only when the caller asked
    for ``require_llm=True``: the deterministic fallback score is a guess, not
    a completed check, so "nobody qualified" must not be reported from it."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def qualify_candidates(
    candidates: list[dict],
    llm_provider: OpenAIChatProvider | None,
    *,
    min_score: int = 65,
    min_confidence: float = 0.60,
    max_results: int = 7,
    require_llm: bool = False,
    llm_timeout_seconds: float | None = None,
    attempt_timeout_seconds: float | None = None,
    max_attempts: int = 1,
    deadline: float | None = None,
) -> list[dict]:
    """Returns at most ``max_results`` qualified candidate dicts, each
    merged with score/confidence/why_fit/hook/risk_flags/evidence fields,
    sorted by total_score descending. Candidates below min_score OR
    min_confidence are dropped - never padded back in to reach max_results.

    ``require_llm=True`` raises QualificationUnavailable instead of silently
    switching to the deterministic fallback when the LLM step fails.
    ``llm_timeout_seconds`` bounds a single wait for the LLM answer;
    ``attempt_timeout_seconds`` / ``max_attempts`` / ``deadline`` (monotonic)
    instead run up to ``max_attempts`` bounded attempts - a retry happens only
    after a transient timeout/network error and only inside ``deadline``.
    """
    if not candidates:
        log.info("partner_discovery.qualified count=0")
        return []

    log.info("partner_discovery.search_started candidate_count=%d", len(candidates))

    provider_present = llm_provider is not None and bool(llm_provider.config.api_key)
    if not provider_present:
        log.info("partner_discovery: qualification fallback_reason=no_provider")
        if require_llm:
            raise QualificationUnavailable("no_provider")
        scored = apply_fallback_qualification(candidates)
    else:
        prompt = _build_qualification_prompt(candidates)
        try:
            if attempt_timeout_seconds is not None or max_attempts > 1:
                result = await _generate_with_retry(
                    llm_provider, prompt, attempt_timeout_seconds=attempt_timeout_seconds,
                    max_attempts=max_attempts, deadline=deadline,
                )
            else:
                call = asyncio.to_thread(llm_provider.generate, message=prompt)
                if llm_timeout_seconds is not None:
                    result = await asyncio.wait_for(call, timeout=llm_timeout_seconds)
                else:
                    result = await call
        except Exception as exc:
            log.warning(
                "partner_discovery: qualification fallback_reason=exception exc_type=%s",
                type(exc).__name__, exc_info=True,
            )
            if require_llm:
                raise QualificationUnavailable("exception") from exc
            scored = apply_fallback_qualification(candidates)
        else:
            parsed, reason = _parse_qualification_response(result.text, candidates)
            if parsed is None:
                log.warning("partner_discovery: qualification fallback_reason=%s", reason)
                if require_llm:
                    raise QualificationUnavailable(reason or "unparseable")
                scored = apply_fallback_qualification(candidates)
            else:
                scored = [
                    {**candidate, **qualification}
                    for candidate, qualification in zip(candidates, parsed, strict=True)
                ]

    # Contact resolution is ALWAYS deterministic (never LLM-sourced, see
    # resolve_contact_channel's own docstring) and always overrides
    # whatever contactability guess the LLM/fallback path produced -
    # otherwise an LLM could effectively "invent" a contact by inflating
    # contactability without any real channel behind it.
    for item, candidate in zip(scored, candidates, strict=True):
        contact = resolve_contact_channel(candidate)
        if contact is not None:
            item.update(contact)
            item["contactability"] = _CONTACTABILITY_BY_CONFIDENCE[contact["contact_confidence"]]
            item["additional_contacts"] = resolve_additional_contacts(candidate, contact)
        else:
            item["contact_type"] = None
            item["contact_value"] = None
            item["contact_url"] = None
            item["contact_source_url"] = None
            item["contact_confidence"] = None
            item["contactability"] = 0
            item["additional_contacts"] = []
            if "no_public_contact" not in item["risk_flags"]:
                item["risk_flags"] = [*item["risk_flags"], "no_public_contact"]

    for item in scored:
        item["total_score"] = (
            item["professional_relevance"]
            + item["activity"]
            + item["commercial_potential"]
            + item["audience_quality"]
            + item["contactability"]
            + item["personal_hook_score"]
        )

    log.info(
        "partner_discovery.thresholds min_score=%d min_confidence=%.2f max_results=%d",
        min_score, min_confidence, max_results,
    )
    qualified: list[dict] = []
    for item in scored:
        hard_excluded = _HARD_EXCLUSION_RISK_FLAGS.intersection(item.get("risk_flags") or [])
        if hard_excluded:
            log.info(
                "partner_discovery.rejected reason=hard_exclusion flags=%s score=%d url=%s",
                ",".join(sorted(hard_excluded)), item["total_score"], item.get("url"),
            )
            continue
        if item["total_score"] < min_score:
            log.info(
                "partner_discovery.rejected reason=low_score score=%d url=%s",
                item["total_score"], item.get("url"),
            )
            continue
        if item["confidence"] < min_confidence:
            log.info(
                "partner_discovery.rejected reason=low_confidence confidence=%.2f score=%d url=%s",
                item["confidence"], item["total_score"], item.get("url"),
            )
            continue
        log.info(
            "partner_discovery.accepted score=%d confidence=%.2f url=%s",
            item["total_score"], item["confidence"], item.get("url"),
        )
        qualified.append(item)

    qualified.sort(key=lambda c: c["total_score"], reverse=True)
    qualified = qualified[:max_results]

    log.info("partner_discovery.qualified count=%d", len(qualified))
    log.info("partner_discovery.completed returned=%d", len(qualified))
    return qualified
