"""Детерминированная зачистка финального ассистентского self-offer хвоста.

Второй защитный слой поверх anti-AI-tail constraints в prompt (см. commit
b718685): даже с явной инструкцией модель иногда всё равно заканчивает
черновик фразой вида «Могу сравнить варианты.» или «Если хотите, можем
вместе проверить конкретный отель и даты.». Это не LLM-вызов и не общий
CTA-фильтр — режем только последнее предложение/абзац текста, и только если
оно целиком является таким self-offer.

Специально узко: обычные человеческие CTA («Скажите даты — посмотрю
варианты.», «Если хотите, можно забронировать по этой ссылке.») не трогаем —
триггер ловит только «могу...», «если хотите/хочешь/нужно, могу/можем...»
и «если хотите/хочешь/нужно, можно + сервисное действие ассистента»
(разобрать/проверить/сравнить/посмотреть/подобрать/обсудить/уточнить).
«можно» само по себе НЕ считается self-offer — только в паре с одним из
этих глаголов, и только внутри «если...»-конструкции.

Не связан с app/services/draft_sanitizer.py (Radar Stage 1 Content Quality
Gate, disputed_claims fail-safe) — сделан отдельно и намеренно, чтобы не
зависеть от WIP в этом модуле.
"""

from __future__ import annotations

import re

_SENTENCE_RE = re.compile(r"[^.!?…\n]*(?:[.!?…]+|\n|$)")
_WORD_RE = re.compile(r"\w+", re.UNICODE)

_LEADING_TRIGGER_RE = re.compile(
    r"^(могу\b|если\s+(?:хотите|хочешь|нужно)\b)", re.IGNORECASE
)
_OFFER_VERB_RE = re.compile(
    r"\b(могу|можем|помогу|поможем|подготовлю|подготовим|пришлю|пришлём|"
    r"покажу|покажем|расскажу|расскажем|сравню|сравним|подскажу|подскажем)\b",
    re.IGNORECASE,
)

# "можно" само по себе слишком общее слово для human CTA ("можно
# забронировать по этой ссылке"), поэтому его не добавляем в _OFFER_VERB_RE.
# Считаем self-offer'ом только узкую конструкцию "можно + сервисное действие
# ассистента" (с допуском на 0-2 слова между ними, например "можно сразу
# разобрать") — сознательно маленький список конкретных инфинитивов из
# реальных production-кейсов, а не общий CTA-blacklist.
_MOZHNO_SERVICE_OFFER_RE = re.compile(
    r"\bможно\b(?:\s+\S+){0,2}?\s+"
    r"(?:разобрать|проверить|сравнить|посмотреть|подобрать|обсудить|уточнить)\b",
    re.IGNORECASE,
)

# extract_offer_sentence()-only superset of _OFFER_VERB_RE, deliberately
# NOT merged into it: _OFFER_VERB_RE also gates strip_assistant_tail's
# destructive cut, and widening that shared pattern would change which text
# strip_assistant_tail removes from what the user actually sees - out of
# scope for extraction, which removes nothing. "сделаю"/"сделаем" ("Если
# хотите, сделаю ещё три варианта.") is a real production offer phrasing
# missing from the stricter strip-only list.
_OFFER_EXTRACT_VERB_RE = re.compile(
    r"\b(могу|можем|помогу|поможем|подготовлю|подготовим|пришлю|пришлём|"
    r"покажу|покажем|расскажу|расскажем|сравню|сравним|подскажу|подскажем|"
    r"сделаю|сделаем)\b",
    re.IGNORECASE,
)

# Настоящий self-offer хвост короткий («Могу сравнить варианты.»).
# Длинное предложение, которое просто начинается с триггерного слова, обычно
# несёт реальный контент — резать его неконсервативно.
_MAX_TAIL_WORDS = 12

# Live prod bug (PendingOffer / assistant-offer follow-up): extract_offer_
# sentence() below reuses the exact same trigger/verb patterns as
# _is_assistant_tail, but with a much looser word cap. strip_assistant_tail's
# own _MAX_TAIL_WORDS=12 exists to avoid wrongly CUTTING a sentence that
# merely starts with a trigger word but is actually substantial content -
# that risk does not apply to extraction: nothing here is removed from the
# text the user sees, this only ALSO records the offer sentence as
# structured state (app.repositories.conversation_state_repository's
# PendingOffer) so a later short reply ("да", "разбери", "объясни разницу")
# can accept it without any lexical-overlap guessing. A real offer sentence
# like "Могу разобрать, в каких случаях членство действительно имеет смысл,
# а в каких — нет." (13 words) is deliberately ABOVE strip's own cutoff -
# extraction must still catch it.
_MAX_OFFER_EXTRACT_WORDS = 40

# Не больше двух подряд идущих финальных предложений за один вызов — защита
# от неожиданного вырезания всего текста.
_MAX_TRAILING_CUTS = 2


def _split_sentences(text: str) -> list[str]:
    pieces = [m.group(0) for m in _SENTENCE_RE.finditer(text)]
    return [p for p in pieces if p != ""]


def _is_assistant_tail(sentence: str) -> bool:
    if not _LEADING_TRIGGER_RE.match(sentence):
        return False
    if len(_WORD_RE.findall(sentence)) > _MAX_TAIL_WORDS:
        return False
    if sentence.lower().startswith("если"):
        return bool(_OFFER_VERB_RE.search(sentence)) or bool(
            _MOZHNO_SERVICE_OFFER_RE.search(sentence)
        )
    return True


def strip_assistant_tail(text: str) -> str:
    """Убирает финальный ассистентский self-offer, если он есть.

    Смотрит только на хвост текста и не трогает середину. Если хвоста нет —
    возвращает text без изменений (та же строка). Никогда не возвращает
    пустую строку: если срез свёл бы текст к пустому, возвращается исходный
    text.
    """
    if not text:
        return text

    sentences = _split_sentences(text)
    kept = [True] * len(sentences)
    cuts = 0

    for index in range(len(sentences) - 1, -1, -1):
        stripped = sentences[index].strip()
        if not stripped:
            # Пустой "кусок" — например, конечный перенос строки. Пропускаем
            # его и продолжаем смотреть дальше к концу текста.
            continue
        if cuts >= _MAX_TRAILING_CUTS:
            break
        if not _is_assistant_tail(stripped):
            break
        kept[index] = False
        cuts += 1

    if cuts == 0:
        return text

    result = "".join(sentence for sentence, keep in zip(sentences, kept) if keep)
    result = re.sub(r"\n{3,}", "\n\n", result).rstrip()

    return result if result else text


def extract_offer_sentence(text: str) -> str | None:
    """Returns the last non-empty sentence of text if it reads like a
    concrete self-offer of a next action ("Могу X.", "Если хотите, Y."),
    using the same trigger/verb patterns as strip_assistant_tail's own
    _is_assistant_tail - just without that function's _MAX_TAIL_WORDS=12
    cutoff (see _MAX_OFFER_EXTRACT_WORDS above for why). Returns None if the
    last sentence does not match, or if text is empty. Looks only at the
    single last sentence - deliberately narrower than strip_assistant_tail's
    multi-sentence _MAX_TRAILING_CUTS, since this only ever needs to capture
    ONE concrete offer to act on, not a whole tail."""
    if not text:
        return None
    for sentence in reversed(_split_sentences(text)):
        stripped = sentence.strip()
        if not stripped:
            continue
        if not _LEADING_TRIGGER_RE.match(stripped):
            return None
        if len(_WORD_RE.findall(stripped)) > _MAX_OFFER_EXTRACT_WORDS:
            return None
        if stripped.lower().startswith("если"):
            if _OFFER_EXTRACT_VERB_RE.search(stripped) or _MOZHNO_SERVICE_OFFER_RE.search(stripped):
                return stripped
            return None
        return stripped
    return None
