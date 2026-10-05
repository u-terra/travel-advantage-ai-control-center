from __future__ import annotations

import re

# Все ключевые слова — подстроки в нижнем регистре.
# Совпадение проверяется in-поиском по lower-тексту запроса.
# Принципы:
#   — общие «темы» (поездка, отдых, путешествие) НЕ используются как сигнал AI Travel Assistant;
#   — латинское "content" в "Travel Content Factory" не цепляет CONTENT (используем кириллицу);
#   — ASSISTANT разделён на intent (явный клиентский контекст) и topic (предметные слова).
#     Topic-слова сами по себе НЕ переводят запрос в AI Travel Assistant, если есть явное
#     намерение создать контент.

CONTENT_KEYWORDS: tuple[str, ...] = (
    "пост", "посты",
    "контент-план", "контент план", "контент",
    "reels", "рилс", "рилз",
    "stories", "сторис",
    "сценар",
    "переписат",
    "живее",
    "ответ на возражен", "возражен",
    "напиши пост", "напиши текст", "напиши сценарий", "напиши сообщение",
    "сделай пост", "сделай сценарий", "сделай сторис", "сделай reels",
    "подготовь пост", "подготовь сценарий", "подготовь сообщение",
    # личные / первые / холодные сообщения — это тоже создание текста
    "первое сообщение", "личное сообщение", "холодное сообщение",
    "сообщение клиент", "сообщение партн", "сообщение человек",
    "копирайт",
)

# Живой прод-баг: «Сделай план публикаций на 14 дней» не содержал ни одного
# слова из CONTENT_KEYWORDS («план» сам по себе туда не входит — иначе он
# перебивал бы предметные ASSISTANT_TOPIC_KEYWORDS вроде «тарифный план») и
# уходил в Module.ORCHESTRATOR (маршрут не определён уверенно). Это не
# конкретная формулировка теста, а целый класс задач: «план/график/расписание
# публикаций/постов/контента [на период]» всегда означает Content Factory,
# в отличие от голого слова «план». Поэтому — отдельный регекс-сигнал, а не
# ещё одна строка в CONTENT_KEYWORDS.
CONTENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:план|график|расписание)\w*(?:\s+\w+){0,3}?\s+"
        r"(?:публикац|пост\w*|контент\w*|материал\w*)",
        re.IGNORECASE,
    ),
)

# Явные клиентские конструкции. Только эти фразы переводят запрос в AI Travel Assistant.
# Намеренно исключены слишком широкие признаки:
#   — голое "клиент" встречается в обычных контентных задачах («пост для клиента»);
#   — "как устроен" встречается в темах постов («пост о том, как устроен Travel Advantage»);
#   — голые "что ответить" / "как ответить" — это может быть «ответ на возражение» в контенте.
ASSISTANT_INTENT_KEYWORDS: tuple[str, ...] = (
    "человек спрашивает", "человек пишет",
    "клиент спрашивает", "клиент пишет",
    "вопрос клиента", "вопрос от клиента", "вопрос человека",
    "что ответить клиенту", "что ответить человеку",
    "как ответить клиенту", "как ответить человеку",
    "ответить клиенту", "ответить человеку",
    "ответ клиенту", "ответ человеку",
    "что такое travel advantage", "что такое life experiences",
    "личный разбор",
    "заявка", "заявку",
)

# Предметные слова: считаются для AI Travel Assistant только когда нет явного
# контентного намерения. Они также участвуют в детекции уровня Safety Layer.
ASSISTANT_TOPIC_KEYWORDS: tuple[str, ...] = (
    "travel advantage",
    "life experiences",
    "тариф",
    "брониров",
    "оплат",
    # Live prod bug: «Какие сейчас изменения правил въезда в Индонезию для
    # россиян?» matched none of the categories above (не контент, не Safety,
    # не Radar, не Partner Packaging, нет явного "клиент спрашивает"/"ответ
    # клиенту" intent) and fell through to Module.ORCHESTRATOR as
    # is_uncertain — the user got "Не удалось уверенно определить маршрут"
    # instead of an answer, and WebSearchService (wired into
    # _maybe_send_draft, only reachable for CONTENT_FACTORY/TRAVEL_ASSISTANT)
    # never even ran. This is the same "предметное слово без явного intent"
    # class as travel advantage/тариф/брониров above — a bare visa/entry-
    # rule/border/flight word, with no content-creation signal present, IS a
    # travel question and belongs to AI Travel Assistant, same as "Что такое
    # MWR Life?" already does (see test_router.py). Still gated by the same
    # content_score>0-and-intent_score==0 rule above, so "Напиши пост про
    # визу на Бали" stays Content Factory exactly as before.
    "виза", "визы", "визовый", "визового", "безвиз",
    "въезд", "границ", "погранич",
    "перелёт", "перелет", "рейс", "рейсы",
)

# Product/rank signals that require context rather than broad single words.
# In particular, bare Ruby/Elite/points/partner are intentionally excluded:
# they collide with programming, games, sport, and ordinary business text.
ASSISTANT_TOPIC_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:loyalty\s+points?|travel\s+credits?|mwr\s+life|"
        r"elite\s+turbo|guest\s+pass)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:\b(?:silver|ruby)\b.{0,60}(?:\b(?:выплат\w*|доход\w*|"
        r"заработ\w*|закры\w*|ранг\w*)\b|\$\s*\d+)|"
        r"(?:\b(?:выплат\w*|доход\w*|заработ\w*|закры\w*|ранг\w*)\b|"
        r"\$\s*\d+).{0,60}\b(?:silver|ruby)\b)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:\b(?:что|как|с\s+чего)\b.{0,50}\bнов\w*\s+партн[её]р\w*\b|"
        r"\bнов\w*\s+партн[её]р\w*\b.{0,50}\b(?:что|как|с\s+чего)\b)",
        re.IGNORECASE,
    ),
)

RADAR_KEYWORDS: tuple[str, ...] = (
    "найти людей", "найти сигнал", "найти темы",
    "сигнал", "сигналы",
    "спрос",
    "обсуждают", "что обсуждают",
    "где ищут отдых", "где ищут поездк",
    "темы для контент", "темы для постов",
    "идеи для постов", "идеи для контент",
    "lead radar", "лид радар",
    "интерес к поездк",
    "публичные сигнал",
)

SAFETY_KEYWORDS: tuple[str, ...] = (
    "проверь", "проверить",
    "перед публикацией", "перед отправкой",
    "опасные обещания", "рискованные обещания",
    "safety layer",
    "оцени риск", "оценить риск",
)

# Явное ДЕЙСТВИЕ пользователя в начале запроса — «перепиши», «адаптируй»,
# «сократи» — против слов внутри вставленного/цитируемого материала (например
# «Проверить сведения можно на сайте...» в самом тексте поста). Проверяются
# только в пределах _leading_instruction() (см. app/routing/router.py), а не
# по всему сообщению — иначе тематическое слово из цитаты продолжало бы
# конкурировать за приоритет с явной командой пользователя. Список
# сознательно маленький: конкретные императивы «переделать текст», а не
# общая тема или синонимы.
#
# Live prod bug: «Нужно переписать пост чтобы не обвинили в плагиате: <пост>»
# used the INFINITIVE "переписать", not the imperative "перепиши" above — this
# list only had imperative forms, so has_rewrite_action stayed False and the
# Safety keyword scan (and, downstream, detect_safety_level) ran over the
# WHOLE message including the pasted post's own numbers, instead of being
# scoped to the leading instruction. "переписать" and "перефразировать" are
# the infinitive counterparts of the imperatives already above; "изложи"/
# "изложить" ("изложи другими словами") and "плагиат" ("чтобы не было
# плагиата", "не обвинили в плагиате") are the other explicit rewrite phrasings
# from the same bug report; "сделай/сделать уникальным" requires the verb, not
# the bare adjective "уникальн", so an unrelated "напиши уникальный пост про…"
# (new content, not a rewrite of pasted material) does not falsely match.
REWRITE_ACTION_KEYWORDS: tuple[str, ...] = (
    "перепиши", "перепишите", "переписать",
    "адаптируй", "адаптируйте",
    "сократи", "сократите",
    "перефразируй", "перефразируйте", "перефразировать",
    "изложи", "изложить",
    "плагиат",
    "сделай уникальным", "сделать уникальным",
)

# Live prod bug: "короче и мягче" / "ещё вариант" / "деловее" sent right
# after the bot's own generated client-reply draft (not a pasted post - the
# user has nothing to paste, they are asking to revise what the BOT just
# wrote) do not describe any rewrite ACTION on their own ("короче" is an
# adjective/adverb, not an imperative verb like "сократи") and must not be
# folded into REWRITE_ACTION_KEYWORDS: that list's own has_rewrite_action
# gate (see app/routing/router.py) specifically scopes Safety-keyword
# detection to a LEADING instruction, on the assumption the rest of the
# message is pasted third-party source material to transform - that
# assumption is wrong here, there is no pasted material, only the bot's own
# prior turn. Kept as a separate, narrow list consumed only by
# app.handlers.tasks' assistant-response follow-up recovery (not by
# route_text() at all), so it never changes what REWRITE_ACTION_KEYWORDS
# means for the existing pasted-post rewrite scenario.
RESPONSE_REVISION_KEYWORDS: tuple[str, ...] = (
    "короче",
    "мягче",
    "деловее",
    "без давления",
    "ещё вариант", "другой вариант",
)

PACKAGING_KEYWORDS: tuple[str, ...] = (
    "инструкц",
    "гайд",
    "коммерческое предложение",
    "презентац",
    "материалы для партнёр", "материалы для партнер",
    "упаковк", "упаковать материал",
    "демо для партнёр", "демо для партнер",
    "описание продукт",
    "onboarding", "онбординг",
    "faq для партнёр", "faq для партнер",
)
