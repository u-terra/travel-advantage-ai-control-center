"""Детерминированная зачистка сгенерированного черновика перед показом.

Это НЕ fact-check и НЕ LLM-вызов: чисто механическая (regex/keyword) очистка
результата генерации после generate_draft(), до сохранения Artifact и до
показа пользователю. Решает узкий Stage 1: стилевые нарушения constraints,
которые модель не выполнила несмотря на явную инструкцию (ассистентские
концовки, внутренние мета-фразы о процессе), и fail-safe удаление
предложений, дословно или почти дословно повторяющих disputed_claims —
если фактическая проверка утверждения недоступна, оно просто не должно
попасть в текст.

Специально консервативно: режем только то, что совпадает с узкими
паттернами или явно пересекается с конкретным disputed claim. Обычный CTA
поста ("Забронировать можно по ссылке ниже") не трогаем.
"""

from __future__ import annotations

import re

# Ассистентские концовки — режем только В КОНЦЕ текста (это и просили:
# "финальные предложения"), а не любое употребление этих слов в тексте —
# иначе можно случайно вырезать нормальный CTA со словом "можно"/"хотите".
# Триггер — начало предложения; для "если хотите" дополнительно требуем
# реальный ассистентский оффер-глагол в этом же предложении (см.
# _looks_like_assistant_offer), иначе легитимный CTA вида "Если хотите
# увидеть..., маршрут начинается от..." тоже попал бы под срез.
_TRAILING_TRIGGER_RE = re.compile(r"^(могу\b|если хотите\b)", re.IGNORECASE)
_ASSISTANT_OFFER_VERB_RE = re.compile(
    r"\b(могу|помогу|подготовлю|пришлю|покажу|расскажу|сравню|подскажу)\b",
    re.IGNORECASE,
)

# Настоящие ассистентские концовки короткие ("Могу сравнить варианты
# поездки."). Длинное предложение, которое просто начинается с триггерного
# слова ("Я могу долго рассказывать про Карелию, но вот главное: ..."),
# обычно несёт реальный контент — резать его целиком неконсервативно.
_MAX_ASSISTANT_ENDING_WORDS = 10

# Максимум подряд идущих финальных предложений, которые можно срезать за
# один вызов — защита от вырезания всего текста на неожиданном входе.
_MAX_TRAILING_CUTS = 3

# Внутренние мета-фразы о процессе — такого не должно быть в готовом посте
# ни в начале, ни в середине, ни в конце, поэтому проверяем весь текст.
#
# Quality fix (signal/competitor -> material contract): production показал
# утечку мета-комментария о надёжности источника прямо в готовый пост
# («Остальное в исходном тексте — шутка и личная оценка, на них лучше не
# опираться.») - формулировки не совпадали ни с одним из прежних узких
# паттернов ("по исходному посту" и т.п. - ровно про пост, не про "текст"
# или "источник" в общем, и ничего про "лучше не опираться"/"личная
# оценка"/"шутка"). Список расширен под этот и соседние реальные варианты
# фразировки той же утечки - модель рассуждает о том, каким частям
# источника доверять, ПРЯМО В тексте поста вместо того, чтобы просто их не
# использовать.
_META_PROCESS_MARKERS: tuple[str, ...] = (
    "нужно проверить",
    "надо проверить",
    "лучше перепроверить",
    "следует перепроверить",
    "стоит перепроверить",
    "по исходному посту",
    "по исходному источнику",
    "в исходном посте",
    "в исходном тексте",
    "исходный текст содержит",
    "остальное в исходном",
    "в источнике сказано",
    "в источнике указано",
    "требует проверки",
    "требует ручной проверки",
    "лучше не опираться",
    "не стоит опираться",
    "на это лучше",
    "на них лучше",
    "личная оценка",
    "не удалось подтвердить",
)

# Доля слов disputed claim, которая должна встретиться в предложении черновика,
# чтобы считать его пересказом этого claim. Специально высокий порог —
# это fail-safe от повторения конкретного спорного утверждения, а не
# общий тематический фильтр.
_DISPUTED_CLAIM_OVERLAP_THRESHOLD = 0.6

_WORD_RE = re.compile(r"\w+", re.UNICODE)

# Разбиение на "предложения": по границам .!?… или по переносу строки,
# разделитель остаётся приклеенным к предложению — так восстановление
# текста после фильтрации не требует отдельной сборки пробелов/переносов.
_SENTENCE_RE = re.compile(r"[^.!?…\n]*(?:[.!?…]+|\n|$)")


def _split_sentences(text: str) -> list[str]:
    pieces = [m.group(0) for m in _SENTENCE_RE.finditer(text)]
    # finditer с $ в паттерне может дать один финальный пустой матч —
    # он ничего не добавляет и не должен создавать лишний пустой элемент.
    return [p for p in pieces if p != ""]


_STEM_LEN = 6


def _stem(word: str) -> str:
    # Грубый стемминг усечением: русская флексия (падеж/число) редко меняет
    # первые ~6 символов слова ("египетски*х*" / "египетски*е*" → "египетс"),
    # а точное посимвольное сравнение слов на инфлективном языке слишком
    # хрупкое для сравнения claim vs пересказанное предложение.
    word = word.lower()
    return word if len(word) <= _STEM_LEN else word[:_STEM_LEN]


def _normalize_words(text: str) -> set[str]:
    return {_stem(w) for w in _WORD_RE.findall(text)}


def _matches_disputed_claim(sentence: str, claim: str) -> bool:
    claim_words = _normalize_words(claim)
    if not claim_words:
        return False
    sentence_words = _normalize_words(sentence)
    if not sentence_words:
        return False
    overlap = claim_words & sentence_words
    return len(overlap) / len(claim_words) >= _DISPUTED_CLAIM_OVERLAP_THRESHOLD


def _contains_meta_marker(sentence: str) -> bool:
    lowered = sentence.lower()
    return any(marker in lowered for marker in _META_PROCESS_MARKERS)


def _is_assistant_ending(sentence: str) -> bool:
    if not _TRAILING_TRIGGER_RE.match(sentence):
        return False
    if len(_WORD_RE.findall(sentence)) > _MAX_ASSISTANT_ENDING_WORDS:
        return False
    if sentence.lower().lstrip().startswith("если хотите"):
        return bool(_ASSISTANT_OFFER_VERB_RE.search(sentence))
    return True


_TOKEN_RE = re.compile(r"\S+")
# A token that carries no real sentence content on its own: a hashtag, a bare
# emoji/punctuation run, or an @mention. Real words (Cyrillic/Latin letters
# not preceded by # or @) fail this.
_DECORATION_TOKEN_RE = re.compile(r"^(?:[#@]\w+|[^\w#@]+)$", re.UNICODE)


def _is_trailing_decoration(sentence: str) -> bool:
    """True for a trailing line that carries no real content of its own -
    typically hashtags and/or emoji social posts commonly end with
    (``#Турция #ОтпускМечты 🌴``). Quality fix: the trailing-assistant-ending
    scan below stops at the first sentence it doesn't recognize, scanning
    from the end - a real AI-tail sentence ("Могу сравнить варианты...")
    followed by a hashtag line was never reached and shipped in production.
    Decoration lines must be skipped over (not counted as a cut, not kept
    or removed themselves) so the scan can see past them."""
    stripped = sentence.strip()
    if not stripped:
        return True
    tokens = _TOKEN_RE.findall(stripped)
    return bool(tokens) and all(_DECORATION_TOKEN_RE.match(token) for token in tokens)


def sanitize_draft_text(text: str, *, disputed_claims: tuple[str, ...] = ()) -> str:
    """Убирает ассистентские концовки, мета-фразы о процессе и предложения,
    пересказывающие disputed_claims. Возвращает готовый к показу текст.

    Пустой text на входе возвращает пустую строку — вызывающий код сам
    решает, считать ли это ошибкой генерации (как и раньше).
    """
    if not text:
        return text

    sentences = _split_sentences(text)

    kept: list[bool] = [True] * len(sentences)
    for index, sentence in enumerate(sentences):
        stripped = sentence.strip()
        if not stripped:
            continue
        if _contains_meta_marker(stripped):
            kept[index] = False
            continue
        if any(_matches_disputed_claim(stripped, claim) for claim in disputed_claims if claim.strip()):
            kept[index] = False

    # Финальные ассистентские концовки — только с конца, только пока
    # совпадает паттерн, с ограничением на число срезаемых предложений.
    # Decoration-only trailing lines (hashtags/emoji - see
    # _is_trailing_decoration) are skipped over, not treated as a stop
    # signal: a real AI-tail sentence followed by "#Турция #ОтпускМечты"
    # must still be found and removed, not shipped as-is.
    cuts = 0
    for index in range(len(sentences) - 1, -1, -1):
        if not kept[index]:
            continue
        stripped = sentences[index].strip()
        if not stripped:
            continue
        if _is_trailing_decoration(stripped):
            continue
        if cuts >= _MAX_TRAILING_CUTS:
            break
        if _is_assistant_ending(stripped):
            kept[index] = False
            cuts += 1
            continue
        break

    result = "".join(
        sentence for sentence, keep in zip(sentences, kept) if keep
    )
    # Зачистка мусора после удаления: тройные+ переносы строк схлопываем,
    # начальные/конечные пробелы и переносы убираем.
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()
