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

# Настоящий self-offer хвост короткий («Могу сравнить варианты.»).
# Длинное предложение, которое просто начинается с триггерного слова, обычно
# несёт реальный контент — резать его неконсервативно.
_MAX_TAIL_WORDS = 12

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
