"""Honest "why this matters" and a concrete angle for a content idea.

Production 13.10: a VK post about a hiking trail (ticket price 250 RUB
mentioned once) was explained as "динамика цен на рынке", and every idea got
the same "Свяжите тему с вашим направлением..." line. The external Lead Radar
recommender picks that wording from bare keyword hits, so Control Center
re-checks it against the material's OWN text before showing it:

- ``topic_kind`` - what the material is actually about (route, guide, season,
  tips, entry rules, costs, market news, ...), from its own words;
- ``reason_for`` - keeps the recommender's reason only when the text backs it
  up; a market/price/demand claim without market-change wording is replaced
  by a description of what the text really is;
- ``angle_for`` - a concrete way to present THAT kind of material. It never
  states a fact about the source and never invents the author's experience.

Pure functions, deterministic, no I/O.
"""

from __future__ import annotations


_ROUTE = ("тропа", "тропы", "тропу", "тропам", "экотроп", "маршрут", "поход",
          "трекинг", "треккинг", "пешком", "пешая", "бродить", "прогулк",
          "электричк", "км по ")
_GUIDE = ("гид по", "путеводител", "что посмотреть", "достопримечательн",
          "куда сходить", "чем заняться", "за выходные", "маршрут на")
_SEASON = ("сезон", "погода", "когда лучше", "лучшее время", "осенью",
           "зимой", "весной", "летом", "бархатный")
_TIPS = ("лайфхак", "совет", "инструкц", "памятка", "как выбрать", "как собрать",
         "что взять", "ошибки")
_RULES = ("визов", "визу", "визы", "визой", "безвиз", "въезд", "паспорт", "границ", "таможн", "электронное разрешение")
_COSTS = ("сколько стоит", "бюджет", "расходы", "траты", "потратил", "обошёл",
          "обошел", "стоимость поездки")
_MARKET_CHANGE = ("подорож", "подешев", "рост цен", "цены выросли", "цены растут",
                  "цены упали", "снижение цен", "повышение цен", "стало дороже",
                  "стало дешевле", "дорожает", "дешевеет", "инфляц", "динамика цен")
_DEMAND_CHANGE = ("турпоток", "спрос", "загрузка отел", "бронирован")
_AVIATION = ("авиа", "рейс", "перелёт", "перелет", "авиакомпани", "аэропорт")

# reason fragments produced by the external recommender that assert a market
# change; each needs wording in the material itself to be believable.
_MARKET_CLAIMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("динамика цен", _MARKET_CHANGE),
    ("изменение спроса", _DEMAND_CHANGE + _MARKET_CHANGE),
    ("изменения в авиасообщении", _AVIATION),
    ("визовых правил", _RULES),
    ("новости туроператоров", ("туроператор", "турагент", "ростуризм")),
)

_KIND_PHRASE = {
    "route": "рассказ о пешем маршруте",
    "guide": "гид по месту или маршруту",
    "season": "когда и в какую погоду ехать",
    "tips": "практические советы для поездки",
    "rules": "правила въезда и документы",
    "costs": "расходы на поездку",
    "market": "изменение цен или спроса на рынке",
    "other": "",
}

_ANGLES = {
    "route": (
        "Расскажите про маршрут так, как рассказали бы другу: как добраться, "
        "сколько идти, что увидите по пути. Берите только то, что есть в оригинале, "
        "и в конце подскажите, кому такая прогулка подойдёт."
    ),
    "guide": (
        "Соберите короткий гид: с чего начать и что не пропустить. Используйте "
        "только места и детали из материала, а свой выбор «что бы взяли первым» "
        "добавьте сами."
    ),
    "season": (
        "Ответьте на вопрос «когда ехать»: что меняется в это время и как "
        "подготовиться. Опирайтесь на то, что написано в материале, без "
        "обещаний погоды."
    ),
    "tips": (
        "Сделайте из материала короткую памятку: 3–4 понятных шага, которые "
        "читатель может применить перед поездкой."
    ),
    "rules": (
        "Объясните простыми словами, что изменилось для путешественника и что "
        "стоит проверить перед выездом на официальном сайте. Без утверждений "
        "сверх материала."
    ),
    "costs": (
        "Покажите, из чего складываются траты на такую поездку, используя только "
        "цифры из материала. Свои расходы добавляйте, только если они у вас были."
    ),
    "market": (
        "Объясните читателю, что меняется на рынке и что это значит при планировании "
        "поездки. Цифры и сроки — только из материала, без прогнозов."
    ),
    "other": (
        "Выберите одну конкретную мысль из материала и расскажите её своими "
        "словами: что произошло, почему это интересно путешественнику и что "
        "с этим можно сделать."
    ),
}


def _text(title: str, summary: str) -> str:
    return f"{title or ''} {summary or ''}".lower()


def _has(text: str, markers: tuple[str, ...]) -> bool:
    return any(m in text for m in markers)


def topic_kind(title: str, summary: str = "") -> str:
    text = _text(title, summary)
    if _has(text, _ROUTE):
        return "route"
    if _has(text, _RULES):
        return "rules"
    if _has(text, _MARKET_CHANGE):
        return "market"
    if _has(text, _GUIDE):
        return "guide"
    if _has(text, _SEASON):
        return "season"
    if _has(text, _COSTS):
        return "costs"
    if _has(text, _TIPS):
        return "tips"
    return "other"


def _claim_unbacked(reason: str, text: str) -> bool:
    low = (reason or "").lower()
    for fragment, markers in _MARKET_CLAIMS:
        if fragment in low and not _has(text, markers):
            return True
    return False


_HEADLINE_CUT = 90


def _headline(title: str) -> str:
    cleaned = " ".join((title or "").split()).rstrip(".…")
    if len(cleaned) <= _HEADLINE_CUT:
        return cleaned
    return cleaned[:_HEADLINE_CUT].rsplit(" ", 1)[0] + "…"


def reason_for(
    reason: str, title: str, summary: str, *, recommended_action: str = "",
    source_label: str = "",
) -> str:
    """The recommender's reason, unless it asserts something the material's
    own text does not support - then a description of what the text really
    is."""
    text = _text(title, summary)
    if not _claim_unbacked(reason, text):
        return reason
    kind = topic_kind(title, summary)
    phrase = _KIND_PHRASE.get(kind) or f"материал «{_headline(title)}»"
    head = f"{source_label}: " if source_label else ""
    tail = (
        " Можно взять как тему для поста."
        if recommended_action == "content" else ""
    )
    return f"{head}{phrase}.{tail}"


def angle_for(title: str, summary: str = "") -> str:
    return _ANGLES[topic_kind(title, summary)]


def web_reason(source_name: str, title: str, body_or_summary: str) -> str:
    kind = topic_kind(title, body_or_summary)
    phrase = _KIND_PHRASE.get(kind) or f"«{_headline(title)}»"
    where = f"Материал с сайта «{source_name}»" if source_name else "Материал с сайта-источника"
    return f"{where}: {phrase}."
