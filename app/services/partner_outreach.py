"""Shared Partner Discovery / Contact Discovery presentation + outreach
logic, extracted out of app/handlers/partner_discovery.py (2026-10-08) so
the Web cabinet can reuse the exact same card enrichment, outreach-draft
generation, and cross-tenant claim guard that Telegram already uses in
production - rather than growing a second, parallel implementation.

Nothing in this module is new behavior: every function here is moved
verbatim (same prompts, same fallback templates, same anti-hallucination
rules) from app/handlers/partner_discovery.py, which now imports from here
instead of defining its own copies. Telegram behavior is unchanged.

Still Telegram-specific and NOT here: FSM state handling, Telegram card
text rendering (_render_card/_render_contact_card), reply-keyboard/callback
wiring. Those stay in app/handlers/partner_discovery.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re

from app.chat_provider import OpenAIChatProvider
from app.domain.competitor_discovery import canonical_domain
from app.domain.contacts import ContactCandidate
from app.domain.partners import WorkspaceContext
from app.domain.prospects import EvidenceSource, ProspectCandidate, ProspectFit, ProspectType
from app.repositories.contact_claim_repository import ContactClaimRepository

log = logging.getLogger(__name__)

# ── Presentation-only cleanup (card name & explanation) ─────────────────────

_NAME_SPLIT_RE = re.compile(r"\s*(?:\||—|–|:)\s*|\s-\s|\.\s+")
_PLATFORM_NAME_TOKENS = frozenset({
    "вконтакте", "vk", "instagram", "инстаграм", "facebook", "телеграм",
    "telegram", "одноклассники",
})
_GENERIC_NAME_DESCRIPTORS = (
    "туроператор", "турагент", "турагентство", "авторские", "авторский",
    "гид", "гиды", "гидов", "экскурси", "блог", "реестр",
    "клуб путешественников", "сообщество путешественников",
)


def clean_name(raw_name: str, url: str) -> str:
    raw_name = (raw_name or "").strip()
    if not raw_name or raw_name.startswith("http://") or raw_name.startswith("https://"):
        return canonical_domain(url)

    parts = [p.strip() for p in _NAME_SPLIT_RE.split(raw_name) if p.strip()]
    parts = [
        p for p in parts
        if len(p) > 1 and not p.isdigit() and p.lower() not in _PLATFORM_NAME_TOKENS
    ]
    if not parts:
        return raw_name[:60]

    def noise_score(part: str) -> int:
        lowered = part.lower()
        return sum(1 for word in _GENERIC_NAME_DESCRIPTORS if word in lowered)

    return min(parts, key=lambda p: (noise_score(p), parts.index(p)))


_PHONE_RE = re.compile(r"(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}")
_URL_IN_TEXT_RE = re.compile(r"https?://\S+")

_NOISE_PHRASES = (
    "вход в лк", "личный кабинет", "рассрочку", "рассрочка", "в рассрочку",
    "гибкой оплаты", "кредит", "скидк", "бесплатно по россии", "звонить",
    "писать", "контакты", "cookie", "куки", "согласие на обработку",
    "политика конфиденциальности", "подарочный сертификат", "акции",
    "отзывы", "вопросы", "календарь", "расписание", "раннее бронирование",
    "оставить заявку", "записаться", "забронировать", "в корзину",
    "каталог", "фильтр", "сортировка", "режим работы", "вход", "меню",
)


def strip_noise(text: str) -> str:
    text = _URL_IN_TEXT_RE.sub(" ", text)
    text = _PHONE_RE.sub(" ", text)
    text = _EMAIL_RE.sub(" ", text)
    for phrase in _NOISE_PHRASES:
        text = re.sub(re.escape(phrase), " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip(" .,;:-")


def _first_clean_clause(cleaned: str, max_len: int = 160) -> str:
    for sep in (". ", "! ", "? "):
        idx = cleaned.find(sep)
        if 15 <= idx <= max_len:
            return cleaned[: idx + 1]
    if not cleaned:
        return ""
    if len(cleaned) <= max_len:
        return cleaned.rstrip(".") + "."
    truncated = cleaned[:max_len].rsplit(" ", 1)[0]
    return truncated.rstrip(",.;:") + "…"


_TYPE_SENTENCE = {
    "travel_agent": "Работает как турагентство/туроператор.",
    "tour_organizer": "Организует туры и путешествия.",
    "guide": (
        "Работает гидом/экскурсоводом - личная аудитория путешественников, "
        "для которой дополнительный travel-инструмент потенциально может быть полезен."
    ),
    "travel_creator": (
        "Ведёт публичный travel-блог/канал о путешествиях - собственная аудитория, "
        "интересующаяся темой путешествий."
    ),
    "travel_club": (
        "Объединяет сообщество путешественников/гидов - внутри такого ресурса часто "
        "можно найти несколько конкретных людей, работающих с путешественниками."
    ),
    "other_travel_project": "Публичный travel-проект.",
}


def build_explanation(prospect_type_value: str, evidence: str) -> str:
    """Max two sentences: a factual what-they-do statement derived from the
    already-computed prospect type, plus (when available) one cleaned
    detail clause from the public evidence itself. Never invents interest
    in partnership/income/Travel Advantage - it only describes what the
    public page/snippet already says."""
    type_sentence = _TYPE_SENTENCE.get(
        prospect_type_value, _TYPE_SENTENCE[ProspectType.OTHER.value],
    )
    clause = _first_clean_clause(strip_noise(evidence))
    if len(clause) < 10:
        return type_sentence
    return f"{type_sentence} {clause}"


# ── LLM enrichment: ONE call for the whole batch ────────────────────────────

_ENRICHMENT_MAX_RESPONSE_LEN = 600

_ENRICHMENT_INSTRUCTIONS = """Ты обрабатываешь данные для карточек Partner Discovery - внутреннего инструмента поиска потенциальных деловых контактов в travel-индустрии. Это НЕ диалог с пользователем - это разовая техническая задача, ответь только данными, без приветствий и комментариев.

Тебе дан список из {count} кандидатов, найденных поиском. Для КАЖДОГО верни ровно два поля:

- "display_name": реальное публичное название проекта/бренда/автора. Коротко (1-4 слова). Никогда не URL и не hostname, если в TITLE или EVIDENCE есть узнаваемое название.
  Порядок приоритета извлечения:
  1) Сначала смотри TITLE. Если он состоит из названия и короткой приписки через тире/вертикальную черту/двоеточие ("Название - что делает", "Название | что делает"), бренд - это именно сегмент с названием, а не описательный сегмент (слова вроде "туроператор", "авторские туры", "гид", "блог" - это описание, не название).
  2) Если в TITLE названия нет или оно неразличимо - ищи явно названное имя проекта/бренда/автора в EVIDENCE.
  3) Используй читаемый hostname (без www, без схемы) ТОЛЬКО если ни TITLE, ни EVIDENCE не содержат узнаваемого названия - это крайний случай, а не вариант по умолчанию.
  Ничего не придумывай: название должно быть напрямую взято из текста TITLE/EVIDENCE, а не додумано по смыслу URL или типа бизнеса.

  Примеры:
  TITLE "Авентурамания - туроператор" -> display_name "Авентурамания" (не "aventuramania.ru").
  TITLE "In Russia Today - туры по России" -> display_name "In Russia Today" (не "inrussiatoday.ru").
  TITLE "Гильдия гидов - Магазин Путешествий" -> display_name "Магазин Путешествий" (вторая часть - название, первая - описание).
  TITLE пустой или нечитаемый, EVIDENCE не называет субъекта по имени -> допустим hostname.

- "reason": 1-2 коротких предложения, только на основе предоставленного public evidence. Опиши:
  (а) чем публично занимается субъект;
  (б) ПОЧЕМУ именно этот субъект потенциально подходит как club-partner контакт - используй ТОЛЬКО явно подтверждённые публичные признаки: собственная туристическая аудитория/сообщество; гид/экскурсовод/организатор путешествий/автор туров; travel-блог или сообщество; работа непосредственно с путешественниками. Если из evidence не следует ни один такой признак (это просто обычная туристическая компания/сайт без признака личной аудитории или сообщества) - прямо скажи это нейтрально (например: "Обычный туристический бизнес без явного признака личной аудитории или сообщества"), не выдумывай признак, которого нет.

СТРОГО ЗАПРЕЩЕНО: утверждать или намекать, что субъект уже заинтересован в партнёрстве; утверждать, что он ищет дополнительный доход; утверждать, что ему нужен Travel Advantage; упоминать любые другие неподтверждённые мотивы; обещать какой-либо результат/выгоду/доход; включать телефоны, email, пункты меню, cookie/согласие, формы заявок, цены/рассрочки или любой служебный текст сайта.{ta_affiliated_clause}

Верни СТРОГО валидный JSON-массив из ровно {count} объектов, в ТОМ ЖЕ порядке, что и во входном списке, без markdown-разметки, без пояснений до или после JSON. Формат каждого элемента: {{"display_name": "...", "reason": "..."}}"""

_TA_AFFILIATED_ENRICHMENT_CLAUSE = (
    " Эта рабочая область работает с Travel Advantage, поэтому в \"reason\" можно "
    "упомянуть Travel Advantage как потенциальный дополнительный инструмент для "
    "этого человека/его аудитории - но только как возможность, не как факт, что "
    "он в ней заинтересован или нуждается."
)


def _build_enrichment_prompt(candidates: list[dict], ta_affiliated: bool = False) -> str:
    items = []
    for i, candidate in enumerate(candidates, 1):
        items.append(
            f"{i}. URL: {candidate['url']}\n"
            f"   TITLE (page/search result title): {candidate['name'] or '(нет)'}\n"
            f"   Предполагаемый тип (подсказка, не факт): {candidate['prospect_type']}\n"
            f"   Источник evidence: {candidate['evidence_source']}\n"
            f"   EVIDENCE (public text): {candidate['evidence'][:600]}"
        )
    header = _ENRICHMENT_INSTRUCTIONS.format(
        count=len(candidates),
        ta_affiliated_clause=_TA_AFFILIATED_ENRICHMENT_CLAUSE if ta_affiliated else "",
    )
    return header + "\n\nКандидаты:\n\n" + "\n\n".join(items)


def sanitize_enrichment_field(value: object, max_len: int) -> str | None:
    """Same noise-stripping as strip_noise (defense in depth even on the
    LLM's own output), but keeps a trailing sentence period intact - unlike
    strip_noise, this is final display text, not an intermediate clause fed
    into _first_clean_clause."""
    if not isinstance(value, str):
        return None
    cleaned = _URL_IN_TEXT_RE.sub(" ", value)
    cleaned = _PHONE_RE.sub(" ", cleaned)
    cleaned = _EMAIL_RE.sub(" ", cleaned)
    for phrase in _NOISE_PHRASES:
        cleaned = re.sub(re.escape(phrase), " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return None
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return cleaned


def _parse_enrichment_response(
    raw_text: str, expected_count: int,
) -> tuple[list[dict] | None, str | None]:
    """Returns (parsed, None) on success or (None, reason) on ANY shape/
    content problem - the caller must then use the deterministic fallback
    for the WHOLE batch, never a partial mix. ``reason`` is a short,
    secret-free category for diagnostic logging only."""
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
    if not isinstance(parsed, list):
        return None, "not_a_list"
    if len(parsed) != expected_count:
        return None, "count_mismatch"

    result: list[dict] = []
    for item in parsed:
        if not isinstance(item, dict):
            return None, "item_not_a_dict"
        display_name = sanitize_enrichment_field(item.get("display_name"), 60)
        reason = sanitize_enrichment_field(item.get("reason"), _ENRICHMENT_MAX_RESPONSE_LEN)
        if not display_name or not reason:
            return None, "item_missing_fields"
        result.append({"display_name": display_name, "reason": reason})
    return result, None


def apply_fallback_enrichment(candidates: list[dict]) -> list[dict]:
    return [
        {
            **candidate,
            "display_name": clean_name(candidate["name"], candidate["url"]),
            "reason": build_explanation(candidate["prospect_type"], candidate["evidence"]),
        }
        for candidate in candidates
    ]


async def enrich_candidates(
    candidates: list[dict], partner_enrichment_llm_provider: OpenAIChatProvider | None,
    *, ta_affiliated: bool = False,
) -> list[dict]:
    """Exactly one LLM call for the whole batch. Any failure - no provider,
    no credentials, transport error, invalid/malformed JSON, wrong item
    count - falls back to the deterministic, already-noise-free cleanup for
    every candidate in the batch."""
    if not candidates:
        return candidates

    provider_present = partner_enrichment_llm_provider is not None
    log.info("partner_discovery: enrichment provider_present=%s", provider_present)
    if not provider_present or not partner_enrichment_llm_provider.config.api_key:
        log.info("partner_discovery: enrichment fallback_reason=no_provider")
        return apply_fallback_enrichment(candidates)

    log.info("partner_discovery: enrichment_call_started batch_size=%d", len(candidates))
    prompt = _build_enrichment_prompt(candidates, ta_affiliated)
    try:
        result = await asyncio.to_thread(
            partner_enrichment_llm_provider.generate, message=prompt,
        )
    except Exception:
        log.warning(
            "partner_discovery: enrichment fallback_reason=exception", exc_info=True,
        )
        return apply_fallback_enrichment(candidates)

    parsed, reason = _parse_enrichment_response(result.text, len(candidates))
    if parsed is None:
        log.warning("partner_discovery: enrichment fallback_reason=%s", reason)
        return apply_fallback_enrichment(candidates)

    log.info("partner_discovery: enrichment success item_count=%d", len(parsed))
    return [
        {**candidate, **enriched}
        for candidate, enriched in zip(candidates, parsed, strict=True)
    ]


# club_partner fit demotion: a "has its own audience/community" signal
# (guide/travel_creator/travel_club) is ranked above a generic business-only
# match (travel_agent/tour_organizer/other) WITHIN a batch - display order
# only, a stable sort over what PartnerDiscoveryService already returned.
_PERSONALIZED_PROSPECT_TYPES = frozenset({"guide", "travel_creator", "travel_club"})


def rank_for_club_partner_fit(candidates: list[dict]) -> list[dict]:
    return sorted(
        candidates, key=lambda c: c["prospect_type"] not in _PERSONALIZED_PROSPECT_TYPES,
    )


def candidate_to_dict(candidate: ProspectCandidate) -> dict:
    return {
        "name": candidate.name,
        "url": candidate.url,
        "prospect_type": candidate.prospect_type.value,
        "query": candidate.query,
        "evidence": candidate.evidence,
        "evidence_source": candidate.evidence_source.value,
        "fit": candidate.fit.value,
    }


def dict_to_prospect(candidate: dict) -> ProspectCandidate:
    """Rebuilds the ProspectCandidate that candidate_to_dict flattened for
    storage. rank_score is not stored (it is an internal ranking aid, never
    needed again once a prospect is already chosen) - 0 is a safe
    placeholder since contact_discovery.find_contact never reads it. Uses
    the already-enriched "display_name" when present, falling back to the
    raw "name" only when no enrichment ran."""
    return ProspectCandidate(
        name=candidate.get("display_name") or candidate["name"],
        url=candidate["url"],
        prospect_type=ProspectType(candidate["prospect_type"]),
        query=candidate["query"],
        evidence=candidate["evidence"],
        evidence_source=EvidenceSource(candidate["evidence_source"]),
        fit=ProspectFit(candidate["fit"]),
        rank_score=0,
    )


_LINK_SOURCE_LABELS = {
    "official_page": "официальный сайт проекта",
    "targeted_search": "страница, где проект назван вместе с этим человеком",
    "professional_search": "страница, где проект назван вместе с этим человеком",
}
NO_CHANNEL_TEXT = "Канал связи не найден"


def contact_status_note(contact: dict) -> str:
    """One honest sentence about HOW the check ended, for a contact dict.
    Empty for a found person."""
    from app.services.contact_discovery import INCOMPLETE_REASON_LABELS

    status = contact.get("search_status")
    if status == "incomplete":
        reasons = [
            INCOMPLETE_REASON_LABELS.get(code, code)
            for code in contact.get("incomplete_reasons") or ()
        ]
        why = "; ".join(reasons) if reasons else "часть проверки не выполнена"
        return (
            f"Проверка не завершена: {why}. Это не значит, что контакта нет, "
            "- попробуйте позже."
        )
    if status == "not_found":
        return "Проверка завершена: публичного представителя проекта найти не удалось."
    return ""


def split_channels(contact: dict) -> tuple[list[dict], list[dict]]:
    """(personal, project) channels of a contact dict."""
    channels = contact.get("channels") or []
    return (
        [c for c in channels if c.get("owner") == "person"],
        [c for c in channels if c.get("owner") != "person"],
    )


def format_channel_line(channel: dict) -> str:
    from app.services.contact_discovery import channel_kind_label

    return (
        f"{channel_kind_label(channel.get('kind', ''))}: {channel.get('value', '')} "
        f"(источник: {channel.get('source_url', '')})"
    )


def contact_to_dict(contact: ContactCandidate) -> dict:
    return {
        "contact_type": contact.contact_type.value,
        "display_name": contact.display_name,
        "role": contact.role,
        "project_name": contact.project_name,
        "public_url": contact.public_url,
        "evidence_text": contact.evidence_text,
        "evidence_url": contact.evidence_url,
        "confidence": contact.confidence.value if contact.confidence else None,
        "provenance": contact.provenance,
        # Where the person-project link comes from (a person only).
        "link_source_label": _LINK_SOURCE_LABELS.get(contact.provenance, "") if contact.contact_type.value == "person" else "",
        "channels": [
            {"kind": ch.kind, "value": ch.value, "source_url": ch.source_url, "owner": ch.owner}
            for ch in contact.channels
        ],
        "search_status": contact.search_status,
        "incomplete_reasons": list(contact.incomplete_reasons),
    }


def strip_private_contact_data(text: str | None) -> str:
    """Defense in depth: strips any phone/email that might have slipped into
    evidence text before it reaches the user, reusing the same patterns
    strip_noise already applies to prospect cards."""
    cleaned = _PHONE_RE.sub(" ", text or "")
    cleaned = _EMAIL_RE.sub(" ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


# ── Outreach draft v0: a human-approved DRAFT first-contact message for the
# PERSON already found by Contact Discovery. No existing outreach/draft
# mechanism in this codebase operates on ContactCandidate/ProspectCandidate
# (the client-reply draft pipeline in app/services/generation_request_builder.py
# and app/services/draft_sanitizer.py is a separate, unrelated domain -
# customer message replies, not partner outreach).

_OUTREACH_DRAFT_INSTRUCTIONS = """Ты помогаешь подготовить ЧЕРНОВИК первого персонального сообщения для знакомства с конкретным человеком, найденным через Partner Discovery / Contact Discovery. Это ЧЕРНОВИК для ручной проверки человеком перед отправкой - НЕ автоматическая рассылка, отвечай только текстом сообщения.

Имя: {name}
Роль (публично подтверждена): {role}
Проект/сообщество: {project}
Почему найден (public evidence): {evidence}
{hook_line}{addressee_rules}

Напиши короткое (4-6 предложений), вежливое, персонализированное первое сообщение на русском языке, которое ПОНЯТНО объясняет, зачем мы пишем - пустое "давайте познакомимся" без причины не годится. Структура:
1) Обращение по имени (если из "Имя" можно уверенно выделить имя/имя-отчество - например из формата "Фамилия Имя Отчество" - используй именно имя/имя-отчество, а не полностью ФИО; если формат неясен - используй то, что есть).
2) Фраза вида "Обращаюсь к вам как к {{роль}}" (адресуя роль ПОЛУЧАТЕЛЮ) + при наличии проекта/сообщества упомяни его. НИКОГДА не формулируй как "Пишу вам как {{роль}}" - это звучит, будто роль принадлежит отправителю, а не адресату.
3) Объясни причину обращения.{ta_instruction}
4) Заверши ОДНИМ коротким вопросом о готовности узнать подробнее - например, "Было бы вам интересно коротко узнать подробнее?" - без развёрнутой презентации.

СТРОГО ЗАПРЕЩЕНО:
- начинать с "Увидел(а) вашу страницу" или любой похожей шаблонной фразы;
- формулировку "Пишу вам как {{роль}}" или любую другую, где роль из evidence может быть прочитана как роль ОТПРАВИТЕЛЯ, а не получателя;
- называть Travel Advantage каким-либо собственным/внутренним названием отправителя (например, упоминать проект/бренд/организацию отправителя рядом с Travel Advantage) - Travel Advantage называется только как "международный туристический клуб и платформа для путешествий", без смешивания с чем-либо ещё;
- утверждать или подразумевать, что человек уже заинтересован, нуждается в Travel Advantage или ищет что-либо конкретное;
- упоминать доход, заработок, окупаемость или любой гарантированный результат;
- придумывать детали о человеке сверх предоставленных полей, в том числе использовать пустые фразы вроде "обращаюсь в связи с {{название}}" без конкретики;
- утверждать личное знакомство, что вы изучили, видели, читали или смотрели материалы адресата;
- обещать выгоду, условия, скидки, преимущества или результат.
Если есть поле "Зацепка" или факты в "Почему найден" - опирайся ТОЛЬКО на них, один конкретный факт, без добавлений.

ГРАММАТИЧЕСКИ ОБЯЗАТЕЛЬНО - сообщение пишется от лица отправителя, чей пол НЕИЗВЕСТЕН и не должен угадываться: используй ТОЛЬКО глагольные формы, не зависящие от рода (настоящее время/инфинитив/безличные конструкции): "обращаюсь", "пишу", "хочу познакомить", "думаю", "могу коротко рассказать" и подобные. СТРОГО ЗАПРЕЩЕНЫ любые родовые формы прошедшего времени или конструкции с "(а)": "нашёл/нашла", "увидел/увидела", "хотел/хотела", "решил/решила", "был/была", "рад/рада" и любые их варианты с "(а)".

Верни только текст сообщения на русском языке, без пояснений, без markdown, без кавычек вокруг всего текста."""

_TA_AFFILIATED_DRAFT_INSTRUCTION = (
    " Хочу познакомить вас с Travel Advantage - международным туристическим "
    "клубом и платформой для путешествий. Думаю, этот формат может быть "
    "интересен проектам/людям, которые напрямую работают с путешественниками "
    "и туристической аудиторией - только как возможность, не обещая "
    "конкретных результатов и не утверждая, что человек в этом уже "
    "заинтересован или нуждается."
)


def greeting_name(display_name: str) -> str:
    """Best-effort "Имя Отчество" extraction from a "Фамилия Имя Отчество"
    -shaped Russian display_name - never invents a name that isn't there."""
    parts = [p for p in (display_name or "").split() if p]
    if len(parts) == 3:
        return f"{parts[1]} {parts[2]}"
    return display_name or ""


# A contact built from a SAVED lead (shared mailbox / company page) carries
# addressee_confirmed=False: nobody was identified as the recipient, so the
# draft must greet the team, not invent a person or role. Contacts coming from
# Contact Discovery never set the key and keep the personal-address behaviour.
_TEAM_ADDRESSEE_RULES = (
    "\nАДРЕСАТ НЕ ПОДТВЕРЖДЁН: контакт общий, конкретный человек неизвестен. "
    "Начни ровно со строки \"Здравствуйте, команда {team}!\". Не используй "
    "\"Обращаюсь к вам как к {{роль}}\", не называй должности и не обращайся по имени."
)
_UNSAFE_DRAFT_RE = re.compile(
    r"\bизучил\w*|\bвидел\w*|\bчитал\w*|\bсмотрел\w*|\bзнаком\w*\s+с\s+ваш\w*|"
    r"выгод\w*|скидк\w*|заработ\w*|доход\w*|гарантир\w*|\(а\)",
    re.IGNORECASE,
)


def _is_team_addressee(contact: dict) -> bool:
    return contact.get("addressee_confirmed") is False


def _team_greeting(contact: dict) -> str:
    team = strip_private_contact_data(
        contact.get("project_name") or contact.get("display_name") or "",
    )
    return f"Здравствуйте, команда {team}!" if team else "Здравствуйте!"


def _fact_sentences(contact: dict) -> list[str]:
    raw = strip_private_contact_data(contact.get("evidence_text") or "")
    return [p.strip(" .") for p in re.split(r"[;\n]", raw) if p.strip(" .")][:2]


def build_outreach_draft_prompt(
    contact: dict, ta_affiliated: bool, sender_business_name: str | None,
) -> str:
    hook = strip_private_contact_data(contact.get("hook") or "")
    return _OUTREACH_DRAFT_INSTRUCTIONS.format(
        hook_line=f"Зацепка (из сохранённых данных): {hook}\n" if hook else "",
        addressee_rules=(
            _TEAM_ADDRESSEE_RULES.format(team=_team_greeting(contact)[len("Здравствуйте, команда "):-1])
            if _is_team_addressee(contact) else ""
        ),
        name=strip_private_contact_data(contact.get("display_name") or ""),
        role=strip_private_contact_data(contact.get("role") or "") or "не указана",
        project=strip_private_contact_data(contact.get("project_name") or ""),
        evidence=strip_private_contact_data(contact.get("evidence_text") or "")[:600] or "(нет)",
        sender_business_name=strip_private_contact_data(sender_business_name or "") or "(не указан)",
        ta_instruction=_TA_AFFILIATED_DRAFT_INSTRUCTION if ta_affiliated else "",
    )


def fallback_outreach_draft(
    contact: dict, ta_affiliated: bool, sender_business_name: str | None = None,
) -> str:
    """Deterministic, noise-free fallback - no LLM required, never invents
    motive. Fully gender-neutral by construction. Travel Advantage is named
    only ever as "международный туристический клуб и платформа для
    путешествий", never alongside any sender project/brand name."""
    name = strip_private_contact_data(contact.get("display_name") or "")
    role = strip_private_contact_data(contact.get("role") or "")
    project = strip_private_contact_data(contact.get("project_name") or "")
    hook = strip_private_contact_data(contact.get("hook") or "").strip(" .")
    facts = _fact_sentences(contact)
    team = _is_team_addressee(contact)

    # Only stored, confirmed facts - no acquaintance, no viewed-materials
    # claims, no promised benefit.
    fact_lines: list[str] = []
    if facts:
        fact_lines.append("Из открытых источников: " + "; ".join(facts) + ".")
    if hook:
        fact_lines.append(f"Также упоминается: {hook}.")

    if team:
        greeting_line = _team_greeting(contact)
        intro = ""
    else:
        greeting_line = f"Здравствуйте, {greeting_name(name)}!"
        if role and project:
            intro = f"Обращаюсь к вам как к {role} в «{project}»."
        elif role:
            intro = f"Обращаюсь к вам как к {role}."
        else:
            intro = ""  # a bare "в связи с «проект»" says nothing - facts follow instead

    lines = [greeting_line, ""]
    if intro:
        lines += [intro, ""]
    if fact_lines:
        lines += [" ".join(fact_lines), ""]
    elif not intro:
        lines += ["Обращаюсь, чтобы коротко познакомиться.", ""]
    if ta_affiliated:
        lines.append(
            "Хочу познакомить вас с Travel Advantage - международным "
            "туристическим клубом и платформой для путешествий."
        )
        lines.append("")
        lines.append(
            "Думаю, этот формат может быть интересен проектам, которые напрямую "
            "работают с путешественниками и туристической аудиторией."
        )
    else:
        lines.append("Пишу, чтобы коротко познакомиться.")
    lines.append("")
    lines.append("Было бы вам интересно коротко узнать подробнее?")
    return "\n".join(lines)


async def generate_outreach_draft(
    contact: dict,
    llm_provider: OpenAIChatProvider | None,
    *,
    ta_affiliated: bool,
    sender_business_name: str | None = None,
) -> str:
    """Always returns a usable draft string - never None, never raises past
    this function. Any failure (no provider, no credentials, transport
    error, empty response) falls back to the deterministic template."""
    if llm_provider is None or not llm_provider.config.api_key:
        return fallback_outreach_draft(contact, ta_affiliated, sender_business_name)

    prompt = build_outreach_draft_prompt(contact, ta_affiliated, sender_business_name)
    try:
        result = await asyncio.to_thread(llm_provider.generate, message=prompt)
    except Exception:
        log.warning("partner_discovery: outreach draft llm call failed", exc_info=True)
        return fallback_outreach_draft(contact, ta_affiliated, sender_business_name)

    draft = sanitize_enrichment_field(result.text, 1000)
    if not draft:
        return fallback_outreach_draft(contact, ta_affiliated, sender_business_name)
    if _UNSAFE_DRAFT_RE.search(draft):
        log.warning("partner_discovery: outreach draft rejected (unsafe claim) - using fallback")
        return fallback_outreach_draft(contact, ta_affiliated, sender_business_name)
    if _is_team_addressee(contact):
        expected = _team_greeting(contact)
        if not draft.startswith(expected):
            log.warning("partner_discovery: outreach draft has wrong team greeting - using fallback")
            return fallback_outreach_draft(contact, ta_affiliated, sender_business_name)
    return draft


def contact_identity_key(contact: dict) -> str:
    """Deterministic cross-tenant claim identity - combines evidence_url
    (the specific public page/result that named this person) with a
    normalized display_name, rather than display_name alone, which could
    collide across genuinely different people sharing a common name."""
    url = (contact.get("evidence_url") or contact.get("public_url") or "").strip().lower()
    name = (contact.get("display_name") or "").strip().lower()
    raw = f"{url}|{name}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def check_and_claim_contact(
    contact: dict,
    workspace_context: WorkspaceContext | None,
    contact_claim_repository: ContactClaimRepository | None,
) -> bool:
    """True = this workspace may proceed with outreach right now (either it
    already held the claim, or none existed and one was just created for
    CLAIM_COOLDOWN_DAYS). False = a DIFFERENT workspace currently holds an
    active claim - the caller must show a neutral "already in progress"
    message and MUST NOT reveal which workspace holds it. No repository/
    workspace available -> fail-open (True)."""
    if contact_claim_repository is None or workspace_context is None:
        return True
    identity_key = contact_identity_key(contact)
    result = await contact_claim_repository.claim(identity_key, workspace_context.workspace_id)
    return result.allowed
