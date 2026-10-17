"""Freshness of a source vs. time-sensitive claims (signal -> post quality).

Incident (2026-10-16): a draft about train discounts used a Tutu.ru article
dated 03.11.2020 and presented "-30% for a birthday", "50 welcome points" and
"up to 2%" as current conditions. The publication date WAS known to the system
(``web_source_signals.published_at``) but it never reached generation, and no
rule existed for facts that expire.

Policy implemented here (pure, deterministic, no network, no LLM):

* the model is told the source date explicitly instead of guessing it from the
  article text;
* an old or undated source is NOT blocked - evergreen content (places, routes,
  how-to advice) stays usable. The age threshold on its own does nothing;
* only a source that is BOTH stale/undated AND contains time-sensitive material
  (prices, percentages, tariffs, discounts, bonuses, visas/entry rules, flights,
  schedules, validity periods) is treated as ``stale_time_sensitive``: its
  numeric/conditional statements are added to ``disputed_claims`` (the existing
  fail-closed path - the draft sanitizer cuts sentences retelling them and the
  safety block says part of the facts is unconfirmed), and the generation
  constraints forbid presenting them as current.

Quality thresholds, source verification and provenance are untouched: nothing
is promoted to verified, nothing is weakened; only more claims are held back.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone

from app.services.article_pages import parse_published_at
from app.services.llm.models import SourceAnalysisPayload

# A source older than this many days (or with no known date) cannot, on its
# own, confirm a CURRENT price/tariff/discount/rule. Named configuration, not a
# magic number: change it here (and in tests) if the policy changes.
STALE_SOURCE_AGE_DAYS = 90

# At most this many held-back claims per source (keeps the prompt small).
MAX_STALE_CLAIMS = 10
# Words kept on each side of a number when a long sentence is cut to a claim.
_CLAIM_WINDOW_WORDS = 4

_CONTEXT_UNKNOWN = "Дата публикации источника: неизвестна."

_MONEY_RE = re.compile(
    r"\d[\d\s .,]*\s?(?:₽|руб\w*|р\.|\$|€|usd|eur|евро|долл\w*)"
    r"|(?:₽|\$|€)\s?\d",
    re.IGNORECASE,
)
_PERCENT_RE = re.compile(r"\d+(?:[.,]\d+)?\s*(?:%|процент\w*)", re.IGNORECASE)
# Stems of words that mark material which expires. Deliberately small.
_WORD_MARKER_RE = re.compile(
    r"\bцен[аыуео]\b|\bценник|\bстоимост|\bстоит\b|\bстоят\b"
    r"|дешев|дешёв|подорож|подешев"
    r"|тариф|скидк|\bакци[яиюе]\b|акционн|распродаж|промокод|спецпредложен"
    r"|бонус|\bбалл(?:ы|ов|а)?\b|кэшбэк|кешбэк|cashback|лояльн"
    r"|\bвиз[аыуойе]\b|безвиз|въезд|шенген|границ"
    r"|\bрейс|расписани|вылет|авиабилет"
    r"|\bдейству(?:ет|ют)\b|срок действия|\bдо \d{1,2}[.\s]",
    re.IGNORECASE,
)
_DIGIT_RE = re.compile(r"\d")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|\n+")


@dataclass(frozen=True)
class SourceFreshness:
    published_date: date | None
    age_days: int | None
    time_sensitive: bool
    stale_time_sensitive: bool
    # "Дата публикации источника: YYYY-MM-DD, возраст: N дней." or the
    # "неизвестна" variant: handed to the model as an explicit meta field.
    context_line: str


def _days_word(count: int) -> str:
    tail = count % 100
    if 11 <= tail <= 14:
        return "дней"
    last = count % 10
    if last == 1:
        return "день"
    if 2 <= last <= 4:
        return "дня"
    return "дней"


def text_is_time_sensitive(*texts: str) -> bool:
    """True if any text names material that expires: prices/currency,
    percentages, tariffs, discounts, bonuses, visas/entry, flights,
    schedules, validity periods."""
    joined = "\n".join(t for t in texts if t)
    return bool(
        _MONEY_RE.search(joined)
        or _PERCENT_RE.search(joined)
        or _WORD_MARKER_RE.search(joined)
    )


def assess_source_freshness(
    published_at: str | None, *texts: str, now: datetime | None = None,
) -> SourceFreshness:
    """Age of the source and whether its time-sensitive content is stale.

    ``published_at`` is whatever the source declared (ISO, RFC822, or empty);
    an unparseable/empty value means "undated" and is never read as fresh.
    """
    moment = now or datetime.now(timezone.utc)
    parsed = parse_published_at(published_at)
    published_date: date | None = None
    age_days: int | None = None
    if parsed:
        published_date = datetime.fromisoformat(parsed).date()
        age_days = max((moment.date() - published_date).days, 0)

    time_sensitive = text_is_time_sensitive(*texts)
    stale = time_sensitive and (age_days is None or age_days > STALE_SOURCE_AGE_DAYS)
    if published_date is None:
        line = _CONTEXT_UNKNOWN
    else:
        line = (
            f"Дата публикации источника: {published_date.isoformat()}, "
            f"возраст: {age_days} {_days_word(age_days)}."
        )
    return SourceFreshness(
        published_date=published_date, age_days=age_days,
        time_sensitive=time_sensitive, stale_time_sensitive=stale,
        context_line=line,
    )


def _claim_from_sentence(sentence: str) -> list[str]:
    """Short number-centred snippets of one sentence ("скидка 30% для
    именинника"), so a paraphrase in a draft overlaps them strongly enough for
    the sanitizer, while the rest of a long sentence is not over-matched."""
    words = sentence.split()
    if len(words) <= 2 * _CLAIM_WINDOW_WORDS + 2:
        return [sentence.strip(" \t«»\"'—-")]
    snippets: list[str] = []
    last_end = -1
    for index, word in enumerate(words):
        if not _DIGIT_RE.search(word):
            continue
        start = max(0, index - _CLAIM_WINDOW_WORDS)
        end = min(len(words), index + _CLAIM_WINDOW_WORDS + 1)
        if start <= last_end and snippets:
            # overlapping window: extend the previous snippet instead of
            # emitting near-duplicates
            previous_start = last_end - len(snippets[-1].split())
            snippets[-1] = " ".join(words[max(previous_start, 0):end])
        else:
            snippets.append(" ".join(words[start:end]))
        last_end = end
    return [s.strip(" \t«»\"'—-,;:") for s in snippets]


def extract_time_sensitive_claims(*texts: str) -> tuple[str, ...]:
    """Numeric/conditional statements from the source that would expire:
    sentences with a digit AND a time-sensitive marker (price, %, tariff,
    discount, bonus, visa/entry, flight, schedule, validity)."""
    claims: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for sentence in _SENTENCE_SPLIT_RE.split(text or ""):
            sentence = sentence.strip()
            if not sentence or not _DIGIT_RE.search(sentence):
                continue
            if not (
                _MONEY_RE.search(sentence) or _PERCENT_RE.search(sentence)
                or _WORD_MARKER_RE.search(sentence)
            ):
                continue
            for claim in _claim_from_sentence(sentence):
                key = claim.lower()
                if claim and key not in seen:
                    seen.add(key)
                    claims.append(claim[:300])
                if len(claims) >= MAX_STALE_CLAIMS:
                    return tuple(claims)
    return tuple(claims)


def stale_source_warning(freshness: SourceFreshness) -> str:
    if freshness.published_date is None:
        basis = "дата публикации неизвестна"
    else:
        basis = (
            f"источник от {freshness.published_date.isoformat()} "
            f"({freshness.age_days} {_days_word(freshness.age_days or 0)})"
        )
    return (
        f"Устаревший или недатированный источник ({basis}): цены, скидки, "
        "тарифы, условия программ, визовые правила и расписания из него не "
        "подтверждены как действующие."
    )


def apply_freshness_to_analysis(
    analysis: SourceAnalysisPayload | None,
    freshness: SourceFreshness,
    *source_texts: str,
) -> SourceAnalysisPayload | None:
    """Fail-closed: for a stale/undated time-sensitive source the expiring
    statements join ``disputed_claims`` (so the sanitizer cuts them from the
    draft and the user sees the "part of the facts is unconfirmed" note) and a
    warning is added. Anything else is returned unchanged - evergreen content
    is never touched."""
    if analysis is None or not freshness.stale_time_sensitive:
        return analysis
    existing = {claim.strip().lower() for claim in analysis.disputed_claims}
    extra = tuple(
        claim for claim in extract_time_sensitive_claims(*source_texts)
        if claim.lower() not in existing
    )
    warning = stale_source_warning(freshness)
    warnings = analysis.warnings
    if warning not in warnings:
        warnings = warnings + (warning,)
    return replace(
        analysis,
        disputed_claims=analysis.disputed_claims + extra,
        warnings=warnings,
    )
