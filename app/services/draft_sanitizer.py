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
# Quality fix: production showed "Если хотите, могу помочь сравнить варианты
# поездки в Японию по датам и погоде." surviving sanitization - "помочь"
# (infinitive, "могу помочь") wasn't in this list (only "помогу", a
# different, future-tense first-person form), and подобрать/организовать
# are equally common AI-offer verbs missing before.
_ASSISTANT_OFFER_VERB_RE = re.compile(
    r"\b(могу|помогу|подготовлю|пришлю|покажу|расскажу|сравню|подскажу|"
    r"помочь|подготовить|сравнить|подсказать|показать|прислать|рассказать|"
    r"подобрать|организовать)\b",
    re.IGNORECASE,
)

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
    # Quality fix (signal -> post): production showed a draft that
    # DISCUSSES the signal instead of BEING the post ("Пока одни достают
    # осенние свитера... в этом сигнале цепляет..."). Distinct from the
    # source-reliability meta-phrases above - this is the model narrating
    # the existence of the signal/source to the reader rather than writing
    # from its own voice using the source's facts.
    "в этом сигнале",
    "источник сообщает",
    "в тексте упоминается",
    "как видно из этого сообщения",
    "автор пишет, что",
    # Quality fix (live example): "Под ударом, по сообщению источника,
    # Пхукет, Краби..." - another phrasing of the same "narrating the
    # source's existence instead of just stating the fact" problem, this
    # time as a parenthetical attribution rather than a full sentence. A
    # fact in the finished post must stand on its own; if it can't be
    # stated confidently without hedging on where it came from, it should
    # be dropped, not wrapped in "по сообщению источника".
    "по сообщению источника",
    "в сигнале говорится",
    "по данным сигнала",
)

# General class for the same "internal kitchen" leak as the markers above -
# any "по <источник-word> <источника/сигнала>" or "в <источнике/сигнале>
# <сказано/указано/говорится/упоминается>" attribution, not just the exact
# phrasings listed. Whole-text scan (not trailing-only), same as
# _contains_meta_marker, since this kind of aside can appear anywhere a
# fact is stated, not only at the end of the post.
_SOURCE_META_REFERENCE_RE = re.compile(
    r"по\s+(?:сообщению|данным|словам|информации)\s+(?:источника|сигнала)"
    r"|в\s+(?:источнике|сигнале)\s+(?:сказано|указано|говорится|упоминается)",
    re.IGNORECASE,
)


def _contains_source_meta_reference(sentence: str) -> bool:
    return bool(_SOURCE_META_REFERENCE_RE.search(sentence))

# Quality fix ("Подготовить пост" 2/5 rating): generic filler phrases that
# make a draft read like a template rather than a specific, self-contained
# post - they carry no fact and no author's own point, just padding around
# whatever the model already wrote. Distinct from _META_PROCESS_MARKERS
# above (those are the model narrating its own process/the source's
# reliability); these are stock transition phrases any generic AI post
# reaches for regardless of the actual signal. Same removal mechanism
# (drop the whole sentence) since, like the meta markers, the phrase itself
# carries the sentence - what is left after removing it is rarely worth
# keeping half of.
_GENERIC_FILLER_MARKERS: tuple[str, ...] = (
    "это отличный повод",
    "прекрасный повод",
    "хороший повод",
    "важно отметить",
    "важно понимать",
    "стоит отметить",
    "нельзя не отметить",
    "нельзя не сказать",
    "стоит сказать",
    "не секрет, что",
    "как известно",
)


def _contains_generic_filler(sentence: str) -> bool:
    lowered = sentence.lower()
    return any(marker in lowered for marker in _GENERIC_FILLER_MARKERS)


# Quality fix (live example): "Напишите — подберу варианты по датам и
# направлению." was showing up as a systematic, auto-appended CTA regardless
# of the post's actual content - a universal template ("подберу/подберём
# варианты по <критерий> и <критерий>"), not a CTA tied to any specific fact
# of the post. The prompt-level constraint (see
# app.services.material_orchestration._RADAR_CONSTRAINTS) is the primary
# fix; this is the deterministic backstop for when the model still emits
# the pattern anyway - same "drop the whole sentence" mechanism as
# _GENERIC_FILLER_MARKERS above.
#
# Deliberately narrower than "any CTA mentioning подобрать" - a genuine,
# content-specific CTA like "Пишите — подберём подходящий вариант." (see
# test_write_to_us_we_will_pick_an_option_cta_survives below) must keep
# working. What marks this pattern as template boilerplate specifically is
# the "по <X> и <Y>" tail - two generic, interchangeable criteria not tied
# to any fact actually in the post - which a one-off, content-driven CTA
# normally does not need to spell out.
_TEMPLATE_CTA_RE = re.compile(
    r"подбер\w*\s+вариант\w*\s+по\s+\S+\s+и\s+\S+", re.IGNORECASE,
)


def _is_template_cta(sentence: str) -> bool:
    return bool(_TEMPLATE_CTA_RE.search(sentence))

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
    """Structural check, not a phrase list: a trailing sentence that
    literally opens with "Могу ..." (first-person AI capability claim) or
    "Если хотите, могу ..." is the AI's own voice regardless of how long the
    rest of the sentence runs on (e.g. "Могу помочь сравнить варианты
    поездки в Японию по датам и погоде." is exactly as much an AI self-offer
    as the short "Могу сравнить варианты." - see task notes: a former
    word-count cap here let longer/more specific real-world phrasings of the
    same offer slip through). A normal brand/agent CTA never opens a
    sentence with "Могу" or "Если хотите" in the first place ("Пишите —
    подберём подходящий вариант.", "Если планируете поездку — напишите.",
    "Напишите, если хотите подобрать поездку." all put a different word (or
    a different clause) first), so anchoring to sentence-start is the narrow
    structural signal that separates AI-voice offers from real commercial
    CTAs, without a large exact-phrase list.
    """
    if not _TRAILING_TRIGGER_RE.match(sentence):
        return False
    if sentence.lower().lstrip().startswith("если хотите"):
        return bool(_ASSISTANT_OFFER_VERB_RE.search(sentence))
    return True


# Quality fix (radar/signal-generated posts): banning one exact CTA phrase
# ("Напишите — подберу варианты по датам и направлению.") only made the
# model reach for the next equally generic one ("Напишите — подберу, что
# можно проверить по вашей поездке.") - chasing individual phrasings is a
# losing game. This is a structural class, not a phrase list: a TRAILING
# sentence that opens with a direct-address imperative inviting the reader
# to contact/write/order ("напишите", "пишите", "обращайтесь", "свяжитесь",
# "звоните", "закажите", "оформите") or a first-person promise to act for
# them ("подберу"/"подберём", "помогу", "расскажу", "узнаю") - regardless
# of what follows. Anchored to sentence-start, same principle as
# _TRAILING_TRIGGER_RE above, so ordinary content/questions mid-sentence or
# mid-post are never touched - only used when the caller has determined CTA
# was not explicitly requested for this generation (see cta_allowed below).
_CONTACT_CTA_START_RE = re.compile(
    r"^(напиш\w*|пиш\w*|обраща\w*|обрат\w*|свяж\w*|звон\w*|позвон\w*|"
    r"закаж\w*|оформ\w*|подбер\w*|помог\w*|расскаж\w*|узна\w*|подскаж\w*|"
    r"сравн\w*|могу\b)",
    re.IGNORECASE,
)

# Quality fix (live example after bc49151): "Если вы как раз выбираете
# Таиланд на ближайшие даты, могу сравнить варианты по регионам и
# подсказать, что лучше подойдёт вам." is the same AI-offer class as
# "Могу сравнить варианты..." (already caught by _CONTACT_CTA_START_RE's
# "могу\b" above and by _is_assistant_ending elsewhere) - just wrapped in a
# conditional "Если вы..., " clause first, so a plain sentence-start match
# on "могу" never reaches it. Structural, not a phrase list: ANY sentence
# that opens with "если" and contains one of the same offer verbs used
# elsewhere in this module (_ASSISTANT_OFFER_VERB_RE) is this pattern,
# regardless of how the condition itself is worded.
_STARTS_WITH_IF_RE = re.compile(r"^если\b", re.IGNORECASE)


def _is_contact_cta_ending(sentence: str) -> bool:
    stripped = sentence.strip()
    if _CONTACT_CTA_START_RE.match(stripped):
        return True
    return bool(_STARTS_WITH_IF_RE.match(stripped) and _ASSISTANT_OFFER_VERB_RE.search(stripped))


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


def sanitize_draft_text(
    text: str, *, disputed_claims: tuple[str, ...] = (), cta_allowed: bool = True,
) -> str:
    """Убирает ассистентские концовки, мета-фразы о процессе и предложения,
    пересказывающие disputed_claims. Возвращает готовый к показу текст.

    Пустой text на входе возвращает пустую строку — вызывающий код сам
    решает, считать ли это ошибкой генерации (как и раньше).

    cta_allowed=False (radar/signal-generated posts, where CTA was never
    explicitly requested - see app.web_api's two build_radar_generation_spec
    call sites) additionally cuts any TRAILING sentence matching the
    general contact-CTA class (_is_contact_cta_ending) - "Напишите...",
    "Подберу...", "Обращайтесь..." and structurally similar direct
    solicitations, regardless of exact wording. Default True preserves
    existing behavior for every other caller (free-text posts, client
    replies, competitor-signal posts) where a normal CTA is still allowed.
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
        if _contains_source_meta_reference(stripped):
            kept[index] = False
            continue
        if _contains_generic_filler(stripped):
            kept[index] = False
            continue
        if _is_template_cta(stripped):
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
        if _is_assistant_ending(stripped) or (
            not cta_allowed and _is_contact_cta_ending(stripped)
        ):
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
