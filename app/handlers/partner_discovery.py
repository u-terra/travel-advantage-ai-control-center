"""Partner Discovery v0 Telegram UI: read-only front end over the already-
stable app.services.partner_discovery.PartnerDiscoveryService.

This module changes NOTHING about discovery itself - no search queries, no
scoring/ranking, no dedupe, no denylist, no provider, no page verification,
no prospect_type logic. It only renders what the service returns, one
candidate card at a time, and walks the user through the batch already in
hand.

Universal Orchestrator function, not Travel Advantage-specific: the button
is shown for BusinessProfile.business_type == "club_partner" alone.
ta_affiliated plays no role in this button's visibility - see
app/handlers/menu.py's own _resolve_web_resource_links docstring for the
established business_type-vs-ta_affiliated distinction this reuses.

Stateless by design: candidates for the current batch live only in FSM
state (state.update_data), never in a database. "⏭ Следующий" only walks
that in-memory batch - it triggers no new search. Only "🔄 Найти ещё"
starts a fresh PartnerDiscoveryService.find_candidates() call.

Card content enrichment (display name + short "why interesting" reason):
ONE LLM call per batch (not per candidate, not per card view) via the
already-deployed OpenAIChatProvider (app.chat_provider - same provider
app.web_api's own chat already uses live in production; no new vendor/API
wired in for this). The deterministic cleanup this module used before
(_clean_name/_build_explanation) is kept as-is and now serves as the
fail-soft fallback: on a missing provider, a transport error, or a
response that is not valid well-formed JSON in the expected shape, every
candidate in the batch falls back to that same safe, noise-free text -
the batch never partially succeeds in a way that could expose raw
page/snippet noise.

Outreach draft v0 (2026-10-07): "✉️ Подготовить первое сообщение" on a
PERSON contact card (see on_partner_contact_draft), plus free-text commands
("Подготовь письмо Ярмоленко", "Что ей написать?" etc. - see
_DRAFT_COMMAND_RE/on_draft_command_text) that continue the SAME Contact
Discovery context already in FSM state. Draft only - never auto-sent,
always requires human approval before sending; never invents the
candidate's interest/motive/need, never promises income or results.
Travel-Advantage-specific wording is only ever added when the workspace is
BusinessProfile.ta_affiliated (see _is_ta_affiliated) - club_partner alone
never implies it, same established separation as the rest of this module.

Still not built: auto-send, persistence of discovered candidates/drafts to
a database.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.chat_provider import OpenAIChatProvider
from app.domain.competitor_discovery import canonical_domain
from app.domain.contacts import ContactCandidate
from app.domain.partners import WorkspaceContext
from app.domain.prospects import EvidenceSource, ProspectCandidate, ProspectFit, ProspectType
from app.keyboards import (
    BTN_FIND_PARTNERS,
    PARTNER_CONTACT_BACK,
    PARTNER_CONTACT_DRAFT,
    PARTNER_CONTACT_FIND,
    PARTNER_DISCOVERY_MORE,
    PARTNER_DISCOVERY_NEXT,
    active_main_menu,
    partner_contact_card_keyboard,
    partner_discovery_card_keyboard,
    partner_discovery_retry_keyboard,
)
from app.repositories.partner_repository import PartnerRepository
from app.services.contact_discovery import find_contact
from app.services.partner_discovery import PartnerDiscoveryService
from app.services.web_search.service import WebSearchService

router = Router(name="partner_discovery")
log = logging.getLogger(__name__)

_STATE_CANDIDATES = "partner_discovery_candidates"
_STATE_INDEX = "partner_discovery_index"
_STATE_CONTACT = "partner_discovery_contact"

_WORKSPACE_UNAVAILABLE = "Рабочее пространство недоступно."
_SEARCH_FAILED = "Не удалось выполнить поиск сейчас. Попробуйте немного позже."
_NO_RESULTS = (
    "По текущим запросам не нашлось новых потенциальных партнёров.\n\n"
    "Можно попробовать ещё раз немного позже."
)
_BATCH_EXHAUSTED = "Это все найденные кандидаты в этой подборке."
_CONTACT_CHECK_FAILED = (
    "Не удалось проверить публичные контакты сейчас. Попробуйте немного позже."
)

_CONTACT_CONFIDENCE_LABELS = {"HIGH": "высокое", "MEDIUM": "среднее"}

_TYPE_LABELS = {
    "travel_agent": "Турагент",
    "tour_organizer": "Организатор туров",
    "guide": "Гид",
    "travel_creator": "Travel-автор",
    "travel_club": "Клуб путешественников",
    "other_travel_project": "Travel-проект",
}
_EVIDENCE_SOURCE_LABELS = {"page": "страница сайта", "snippet": "поисковая выдача"}
_FIT_LABELS = {"STRONG": "высокое", "POSSIBLE": "возможное"}

_TYPE_SENTENCE = {
    "travel_agent": "Работает как турагентство/туроператор.",
    "tour_organizer": "Организует туры и путешествия.",
    # 2026-10-07: guide/travel_creator/travel_club get an explicit
    # club_partner fit rationale (own audience/community - a public signal,
    # never a claim that the subject wants a partnership or income) per the
    # task this was built for. travel_agent/tour_organizer/other stay
    # unchanged - they are the generic/company case this task asks to rank
    # below personalized candidates (see _PERSONALIZED_PROSPECT_TYPES), not
    # to rewrite into a false personal-fit claim.
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

# club_partner fit demotion (2026-10-07): a "has its own audience/community"
# signal (guide/travel_creator/travel_club) is ranked above a generic
# business-only match (travel_agent/tour_organizer/other) WITHIN a batch -
# display order only, a stable sort over what
# app.services.partner_discovery.PartnerDiscoveryService already returned.
# Never touches that service's own query/scoring/dedupe/ranking.
_PERSONALIZED_PROSPECT_TYPES = frozenset({"guide", "travel_creator", "travel_club"})


def _rank_for_club_partner_fit(candidates: list[dict]) -> list[dict]:
    return sorted(
        candidates, key=lambda c: c["prospect_type"] not in _PERSONALIZED_PROSPECT_TYPES,
    )

# ── Presentation-only cleanup (card name & explanation) ─────────────────────
# Everything below only reshapes what the service already returned for
# display - it never changes what is found, scored, ranked, deduped, or
# filtered (that stays entirely inside app/services/partner_discovery.py).

_NAME_SPLIT_RE = re.compile(r"\s*(?:\||—|–|:)\s*|\s-\s|\.\s+")
_PLATFORM_NAME_TOKENS = frozenset({
    "вконтакте", "vk", "instagram", "инстаграм", "facebook", "телеграм",
    "telegram", "одноклассники",
})
# Generic category words - strip them out when picking the one segment of a
# noisy page/VK title that is the actual brand, not a descriptor of what
# the brand does (see the live cases this was tuned against: "Гильдия
# гидов - Магазин Путешествий" must keep "Магазин Путешествий", "Duh
# Travel. Авторские путешествия | ..." must keep "Duh Travel").
_GENERIC_NAME_DESCRIPTORS = (
    "туроператор", "турагент", "турагентство", "авторские", "авторский",
    "гид", "гиды", "гидов", "экскурси", "блог", "реестр",
    "клуб путешественников", "сообщество путешественников",
)


def _clean_name(raw_name: str, url: str) -> str:
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

# Menu/service/consent/form boilerplate that shows up in raw page extracts -
# none of it says anything about what the subject publicly does, so none of
# it belongs in a human-facing explanation.
_NOISE_PHRASES = (
    "вход в лк", "личный кабинет", "рассрочку", "рассрочка", "в рассрочку",
    "гибкой оплаты", "кредит", "скидк", "бесплатно по россии", "звонить",
    "писать", "контакты", "cookie", "куки", "согласие на обработку",
    "политика конфиденциальности", "подарочный сертификат", "акции",
    "отзывы", "вопросы", "календарь", "расписание", "раннее бронирование",
    "оставить заявку", "записаться", "забронировать", "в корзину",
    "каталог", "фильтр", "сортировка", "режим работы", "вход", "меню",
)


def _strip_noise(text: str) -> str:
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


def _build_explanation(prospect_type_value: str, evidence: str) -> str:
    """Max two sentences: a factual what-they-do statement derived from the
    already-computed prospect type, plus (when available) one cleaned
    detail clause from the public evidence itself. Never invents interest
    in partnership/income/Travel Advantage - it only describes what the
    public page/snippet already says."""
    type_sentence = _TYPE_SENTENCE.get(
        prospect_type_value, _TYPE_SENTENCE[ProspectType.OTHER.value],
    )
    clause = _first_clean_clause(_strip_noise(evidence))
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

# Only appended when the current workspace is BusinessProfile.ta_affiliated
# (never by business_type/club_partner alone - see this module's own
# docstring and app/handlers/menu.py's established business_type-vs-
# ta_affiliated distinction). Still forbids inventing interest/need - it
# only permits NAMING Travel Advantage as a possible tool, same fail-closed
# posture as the rest of this prompt.
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


def _sanitize_enrichment_field(value: object, max_len: int) -> str | None:
    """Same noise-stripping as _strip_noise (defense in depth even on the
    LLM's own output), but keeps a trailing sentence period intact - unlike
    _strip_noise, this is final display text, not an intermediate clause
    fed into _first_clean_clause."""
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
        display_name = _sanitize_enrichment_field(item.get("display_name"), 60)
        reason = _sanitize_enrichment_field(item.get("reason"), _ENRICHMENT_MAX_RESPONSE_LEN)
        if not display_name or not reason:
            return None, "item_missing_fields"
        result.append({"display_name": display_name, "reason": reason})
    return result, None


def _apply_fallback_enrichment(candidates: list[dict]) -> list[dict]:
    return [
        {
            **candidate,
            "display_name": _clean_name(candidate["name"], candidate["url"]),
            "reason": _build_explanation(candidate["prospect_type"], candidate["evidence"]),
        }
        for candidate in candidates
    ]


async def _enrich_candidates(
    candidates: list[dict], partner_enrichment_llm_provider: OpenAIChatProvider | None,
    *, ta_affiliated: bool = False,
) -> list[dict]:
    """Exactly one LLM call for the whole batch (never per-candidate, never
    on "Следующий" - callers only invoke this right after a fresh search).
    Any failure - no provider, no credentials, transport error, invalid/
    malformed JSON, wrong item count - falls back to the deterministic,
    already-noise-free cleanup for every candidate in the batch.

    Diagnostic logging here is deliberately minimal and secret-free: a
    provider_present flag, a call-started marker, and on failure a short
    fallback_reason category (never the raw response body, never any
    credential) - added after a production incident where this path was
    silently always falling back with nothing in the logs to show why."""
    if not candidates:
        return candidates

    provider_present = partner_enrichment_llm_provider is not None
    log.info("partner_discovery: enrichment provider_present=%s", provider_present)
    if not provider_present or not partner_enrichment_llm_provider.config.api_key:
        log.info("partner_discovery: enrichment fallback_reason=no_provider")
        return _apply_fallback_enrichment(candidates)

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
        return _apply_fallback_enrichment(candidates)

    parsed, reason = _parse_enrichment_response(result.text, len(candidates))
    if parsed is None:
        log.warning("partner_discovery: enrichment fallback_reason=%s", reason)
        return _apply_fallback_enrichment(candidates)

    log.info("partner_discovery: enrichment success item_count=%d", len(parsed))
    return [
        {**candidate, **enriched}
        for candidate, enriched in zip(candidates, parsed, strict=True)
    ]


async def _is_club_partner(
    partner_repository: PartnerRepository, workspace_context: WorkspaceContext,
) -> bool:
    profile = await partner_repository.get_business_profile(workspace_context.workspace_id)
    return profile is not None and profile.business_type == "club_partner"


async def _is_ta_affiliated(
    partner_repository: PartnerRepository, workspace_context: WorkspaceContext | None,
) -> bool:
    """Authoritative, fail-closed TA-specific gate for enrichment/outreach
    wording - BusinessProfile.ta_affiliated ONLY, same established
    business_type-vs-ta_affiliated separation as app/handlers/competitors.py
    ::_refresh_competitor and this module's own docstring. Never True
    without a resolvable workspace/profile."""
    if workspace_context is None:
        return False
    profile = await partner_repository.get_business_profile(workspace_context.workspace_id)
    return bool(profile is not None and getattr(profile, "ta_affiliated", False))


def _candidate_to_dict(candidate: ProspectCandidate) -> dict:
    return {
        "name": candidate.name,
        "url": candidate.url,
        "prospect_type": candidate.prospect_type.value,
        "query": candidate.query,
        "evidence": candidate.evidence,
        "evidence_source": candidate.evidence_source.value,
        "fit": candidate.fit.value,
    }


def _render_card(candidates: list[dict], index: int) -> tuple[str, bool]:
    """Returns (text, has_more_in_batch). Expects each candidate dict to
    already carry "display_name"/"reason" - set once by _enrich_candidates
    right after a search, never recomputed here. No "Тип: ..." line -
    internal prospect_type stays internal, it is not shown to the user."""
    candidate = candidates[index]
    fit_label = _FIT_LABELS.get(candidate["fit"], candidate["fit"])
    evidence_label = _EVIDENCE_SOURCE_LABELS.get(
        candidate["evidence_source"], candidate["evidence_source"],
    )
    name = candidate.get("display_name") or _clean_name(candidate["name"], candidate["url"])
    explanation = candidate.get("reason") or _build_explanation(
        candidate["prospect_type"], candidate["evidence"],
    )
    text = (
        f"{index + 1}/{len(candidates)}\n\n"
        f"{name}\n"
        f"Соответствие: {fit_label}\n\n"
        f"Почему интересен:\n{explanation}\n\n"
        f"Источник:\n{candidate['url']}\n\n"
        f"Подтверждено: {evidence_label}"
    )
    has_more = index + 1 < len(candidates)
    return text, has_more


# ── Contact Discovery v0 integration (read-only layer on top of the same
# FSM batch; app/services/contact_discovery.py itself is not modified here) ──

def _dict_to_prospect(candidate: dict) -> ProspectCandidate:
    """Rebuilds the ProspectCandidate that _candidate_to_dict flattened for
    FSM storage. rank_score is not stored (it is an internal ranking aid,
    never needed again once a prospect is already chosen) - 0 is a safe
    placeholder since contact_discovery.find_contact never reads it.

    UX fix (2026-10-07): uses the already-enriched "display_name" when
    present (set once by _enrich_candidates right after search - see
    _render_card, which already shows this same name on the prospect
    card), falling back to the raw "name" (candidate.name - a search
    result's title, occasionally the bare URL when a result had no real
    title) only when no enrichment ran. Contact Discovery's own
    _project_name() falls back to prospect.url when prospect.name is
    falsy, and without this fix a PERSON card's "Проект:" line could
    show that raw/URL-ish name instead of the pretty one already shown
    everywhere else for this same prospect."""
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


def _contact_to_dict(contact: ContactCandidate) -> dict:
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
    }


def _strip_private_contact_data(text: str | None) -> str:
    """Defense in depth (privacy requirement #9): strips any phone/email
    that might have slipped into evidence text before it reaches the user,
    reusing the same patterns _strip_noise already applies to prospect
    cards. contact_discovery's own prompts already forbid this content -
    this is a second, independent guard on the render path."""
    cleaned = _PHONE_RE.sub(" ", text or "")
    cleaned = _EMAIL_RE.sub(" ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _render_contact_card(contact: dict) -> str:
    if contact["contact_type"] == "person":
        name = _strip_private_contact_data(contact["display_name"])
        role = _strip_private_contact_data(contact.get("role"))
        project = _strip_private_contact_data(contact["project_name"])
        evidence = _strip_private_contact_data(contact["evidence_text"])
        confidence_label = _CONTACT_CONFIDENCE_LABELS.get(
            contact.get("confidence"), contact.get("confidence") or "",
        )
        return (
            "Найден публичный контакт\n\n"
            f"{name}\n"
            f"Роль: {role}\n"
            f"Проект: {project}\n\n"
            f"Почему выбран:\n{evidence}\n\n"
            f"Источник:\n{contact['evidence_url']}\n\n"
            f"Подтверждение: {confidence_label}"
        )

    # UX fix (2026-10-07): the URL used to appear both here in the card
    # text AND as the "🌐 Открыть проект" button below it (same URL,
    # contact_dict["public_url"] in both places) - shown once now, via the
    # button only.
    project = _strip_private_contact_data(contact["project_name"])
    return (
        "Конкретный публичный представитель проекта не найден в доступных "
        "публичных источниках.\n\n"
        "Можно обратиться через официальный канал проекта:\n\n"
        f"{project}"
    )


async def _run_discovery(web_search_service: WebSearchService | None) -> list[dict] | None:
    """Returns a list of candidate dicts, an empty list on zero results, or
    None on a search failure - the three cases the caller must render
    differently."""
    if web_search_service is None or web_search_service.provider is None:
        return None
    service = PartnerDiscoveryService(web_search_service.provider)
    try:
        candidates = await service.find_candidates()
    except Exception:
        log.warning("partner_discovery: find_candidates failed", exc_info=True)
        return None
    return [_candidate_to_dict(c) for c in candidates]


@router.message(F.text == BTN_FIND_PARTNERS)
async def on_find_partners(
    message: Message,
    state: FSMContext,
    workspace_context: WorkspaceContext | None,
    partner_repository: PartnerRepository,
    web_search_service: WebSearchService | None = None,
    partner_enrichment_llm_provider: OpenAIChatProvider | None = None,
    v2_menu_enabled: bool = False,
) -> None:
    if workspace_context is None:
        await message.answer(
            _WORKSPACE_UNAVAILABLE, reply_markup=active_main_menu(v2_menu_enabled),
        )
        return

    # Defense in depth: the button itself is only shown to club_partner
    # workspaces, but reply-keyboard text can be replayed by any client.
    if not await _is_club_partner(partner_repository, workspace_context):
        await message.answer(
            _WORKSPACE_UNAVAILABLE, reply_markup=active_main_menu(v2_menu_enabled),
        )
        return

    candidates = await _run_discovery(web_search_service)
    if candidates is None:
        await message.answer(_SEARCH_FAILED)
        return
    if not candidates:
        await message.answer(_NO_RESULTS, reply_markup=partner_discovery_retry_keyboard())
        return

    ta_affiliated = await _is_ta_affiliated(partner_repository, workspace_context)
    candidates = await _enrich_candidates(
        candidates, partner_enrichment_llm_provider, ta_affiliated=ta_affiliated,
    )
    candidates = _rank_for_club_partner_fit(candidates)
    await state.update_data(**{_STATE_CANDIDATES: candidates, _STATE_INDEX: 0})
    text, has_more = _render_card(candidates, 0)
    await message.answer(
        text, reply_markup=partner_discovery_card_keyboard(candidates[0]["url"], has_more),
    )


@router.callback_query(F.data == PARTNER_DISCOVERY_NEXT)
async def on_partner_discovery_next(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    candidates: list[dict] = data.get(_STATE_CANDIDATES) or []
    index: int = data.get(_STATE_INDEX, 0)

    if not candidates or index + 1 >= len(candidates):
        if callback.message is not None:
            await callback.message.answer(
                _BATCH_EXHAUSTED, reply_markup=partner_discovery_retry_keyboard(),
            )
        return

    index += 1
    await state.update_data(**{_STATE_INDEX: index})
    text, has_more = _render_card(candidates, index)
    if callback.message is not None:
        await callback.message.answer(
            text, reply_markup=partner_discovery_card_keyboard(candidates[index]["url"], has_more),
        )


@router.callback_query(F.data == PARTNER_DISCOVERY_MORE)
async def on_partner_discovery_more(
    callback: CallbackQuery,
    state: FSMContext,
    web_search_service: WebSearchService | None = None,
    partner_enrichment_llm_provider: OpenAIChatProvider | None = None,
    workspace_context: WorkspaceContext | None = None,
    partner_repository: PartnerRepository | None = None,
) -> None:
    await callback.answer()
    candidates = await _run_discovery(web_search_service)

    if candidates is None:
        if callback.message is not None:
            await callback.message.answer(_SEARCH_FAILED)
        return
    if not candidates:
        if callback.message is not None:
            await callback.message.answer(_NO_RESULTS, reply_markup=partner_discovery_retry_keyboard())
        return

    ta_affiliated = (
        await _is_ta_affiliated(partner_repository, workspace_context)
        if partner_repository is not None else False
    )
    candidates = await _enrich_candidates(
        candidates, partner_enrichment_llm_provider, ta_affiliated=ta_affiliated,
    )
    candidates = _rank_for_club_partner_fit(candidates)
    await state.update_data(**{_STATE_CANDIDATES: candidates, _STATE_INDEX: 0})
    text, has_more = _render_card(candidates, 0)
    if callback.message is not None:
        await callback.message.answer(
            text, reply_markup=partner_discovery_card_keyboard(candidates[0]["url"], has_more),
        )


@router.callback_query(F.data == PARTNER_CONTACT_FIND)
async def on_partner_contact_find(
    callback: CallbackQuery,
    state: FSMContext,
    web_search_service: WebSearchService | None = None,
    partner_enrichment_llm_provider: OpenAIChatProvider | None = None,
) -> None:
    """Contact Discovery v0 for the CURRENT prospect card only - reads the
    already-chosen candidate out of the same FSM batch on-screen, never
    starts a new Partner Discovery search. Reuses the already-deployed
    LLM provider and web search provider - no new vendor, no DB write.

    app.services.contact_discovery.find_contact already resolves every
    internal failure to a safe ContactType.PROJECT fallback and never
    raises by design; this try/except is integration-level defense in
    depth only (e.g. a provider wiring error), per task requirement #8 -
    the user must never see a traceback."""
    await callback.answer()
    data = await state.get_data()
    candidates: list[dict] = data.get(_STATE_CANDIDATES) or []
    index: int = data.get(_STATE_INDEX, 0)
    if not candidates or index >= len(candidates):
        return

    prospect = _dict_to_prospect(candidates[index])
    web_search_provider = (
        web_search_service.provider if web_search_service is not None else None
    )
    try:
        contact = await find_contact(
            prospect,
            llm_provider=partner_enrichment_llm_provider,
            web_search_provider=web_search_provider,
        )
    except Exception:
        log.warning("partner_discovery: contact discovery failed", exc_info=True)
        if callback.message is not None:
            await callback.message.answer(_CONTACT_CHECK_FAILED)
        return

    contact_dict = _contact_to_dict(contact)
    await state.update_data(**{_STATE_CONTACT: contact_dict})
    text = _render_contact_card(contact_dict)
    url = (
        contact_dict["evidence_url"] if contact_dict["contact_type"] == "person"
        else contact_dict["public_url"]
    )
    if callback.message is not None:
        await callback.message.answer(
            text, reply_markup=partner_contact_card_keyboard(contact_dict["contact_type"], url),
        )


@router.callback_query(F.data == PARTNER_CONTACT_BACK)
async def on_partner_contact_back(callback: CallbackQuery, state: FSMContext) -> None:
    """Returns to the same prospect card already in FSM state - no new
    PartnerDiscoveryService.find_candidates() call (task requirement #7)."""
    await callback.answer()
    data = await state.get_data()
    candidates: list[dict] = data.get(_STATE_CANDIDATES) or []
    index: int = data.get(_STATE_INDEX, 0)
    if not candidates or index >= len(candidates):
        return

    text, has_more = _render_card(candidates, index)
    if callback.message is not None:
        await callback.message.answer(
            text,
            reply_markup=partner_discovery_card_keyboard(candidates[index]["url"], has_more),
        )


# ── Outreach draft v0 (2026-10-07): a human-approved DRAFT first-contact ────
# message for the PERSON already found by Contact Discovery (_STATE_CONTACT
# in FSM state). No existing outreach/draft mechanism in this codebase
# operates on ContactCandidate/ProspectCandidate (the client-reply draft
# pipeline in app/services/generation_request_builder.py and
# app/services/draft_sanitizer.py is a separate, unrelated domain - customer
# message replies, not partner outreach) - this is intentionally the first
# and only one for this use case, not a second mechanism for something that
# already existed.

_DRAFT_UNAVAILABLE = (
    "Нет найденного контакта для черновика. Сначала используйте "
    "«👤 Найти человека для контакта»."
)
_DRAFT_HEADER = (
    "✉️ Черновик первого сообщения - проверьте и отредактируйте перед отправкой:\n\n"
)

# Matches the task's own example phrasings ("Подготовь письмо Ярмоленко",
# "Подготовь сообщение этому контакту", "Напиши первое сообщение", "Что ей
# написать?", "Подготовь письмо к первому контакту Ярмоленко") without being
# a broad catch-all - see _looks_like_draft_command, which ALSO requires a
# PERSON contact already in FSM state before this ever fires, so an
# unrelated message elsewhere in the bot is never swallowed by this pattern.
_DRAFT_COMMAND_RE = re.compile(
    r"(подготов\w*|напиш\w*)\s.*(письм\w*|сообщени\w*|обращени\w*)"
    r"|что\s+(?:ей|ему|им)\s+написать",
    re.IGNORECASE,
)

_OUTREACH_DRAFT_INSTRUCTIONS = """Ты помогаешь подготовить ЧЕРНОВИК первого персонального сообщения для знакомства с конкретным человеком, найденным через Partner Discovery / Contact Discovery. Это ЧЕРНОВИК для ручной проверки человеком перед отправкой - НЕ автоматическая рассылка, отвечай только текстом сообщения.

Имя: {name}
Роль (публично подтверждена): {role}
Проект/сообщество: {project}
Почему найден (public evidence): {evidence}

Напиши короткое (3-5 предложений), вежливое, персонализированное первое сообщение на русском языке, которое открывает диалог. СТРОГО ЗАПРЕЩЕНО:
- утверждать или подразумевать, что человек уже заинтересован в партнёрстве, ищет дополнительный доход или хочет что-либо конкретное;
- обещать доход, результат, окупаемость или любые гарантии;
- придумывать детали о человеке сверх предоставленных имени/роли/проекта/evidence;
- писать развёрнутое коммерческое предложение - это только короткое знакомство, открывающее диалог.

Используй имя, роль и проект для персонализации (например, упомяни именно ту работу/роль, которая подтверждена).{ta_instruction}

Верни только текст сообщения на русском языке, без пояснений, без markdown, без кавычек вокруг всего текста."""

# Only appended when BusinessProfile.ta_affiliated - see _is_ta_affiliated.
# Still forbids inventing interest/need - only permits NAMING Travel
# Advantage as a possible tool for an introduction, same fail-closed posture
# as _TA_AFFILIATED_ENRICHMENT_CLAUSE above.
_TA_AFFILIATED_DRAFT_INSTRUCTION = (
    " Если уместно, можно кратко и фактически упомянуть Travel Advantage как "
    "возможный дополнительный инструмент для этого человека и его аудитории - "
    "только как повод для знакомства, не обещая конкретных результатов и не "
    "утверждая, что человек в этом уже заинтересован."
)


def _build_outreach_draft_prompt(contact: dict, ta_affiliated: bool) -> str:
    return _OUTREACH_DRAFT_INSTRUCTIONS.format(
        name=_strip_private_contact_data(contact.get("display_name") or ""),
        role=_strip_private_contact_data(contact.get("role") or "") or "не указана",
        project=_strip_private_contact_data(contact.get("project_name") or ""),
        evidence=_strip_private_contact_data(contact.get("evidence_text") or "")[:600] or "(нет)",
        ta_instruction=_TA_AFFILIATED_DRAFT_INSTRUCTION if ta_affiliated else "",
    )


def _fallback_outreach_draft(contact: dict, ta_affiliated: bool) -> str:
    """Deterministic, noise-free fallback - same safety posture as
    _apply_fallback_enrichment: no LLM required, never invents motive."""
    name = _strip_private_contact_data(contact.get("display_name") or "")
    role = _strip_private_contact_data(contact.get("role") or "")
    project = _strip_private_contact_data(contact.get("project_name") or "")
    role_clause = f" ({role})" if role else ""
    lines = [
        f"Здравствуйте, {name}!",
        "",
        f"Увидел(а) вашу публичную страницу{role_clause} в связи с «{project}» и решил(а) написать напрямую.",
    ]
    if ta_affiliated:
        lines.append(
            "Я работаю с Travel Advantage и был(а) бы рад(а) коротко рассказать, чем "
            "платформа может быть полезна вам и вашей аудитории, если это интересно."
        )
    lines.append("Было бы здорово познакомиться и обменяться парой слов.")
    lines.append("")
    lines.append("С уважением")
    return "\n".join(lines)


async def _generate_outreach_draft(
    contact: dict, llm_provider: OpenAIChatProvider | None, *, ta_affiliated: bool,
) -> str:
    """Always returns a usable draft string - never None, never raises past
    this function. Any failure (no provider, no credentials, transport
    error, empty response) falls back to the deterministic template."""
    if llm_provider is None or not llm_provider.config.api_key:
        return _fallback_outreach_draft(contact, ta_affiliated)

    prompt = _build_outreach_draft_prompt(contact, ta_affiliated)
    try:
        result = await asyncio.to_thread(llm_provider.generate, message=prompt)
    except Exception:
        log.warning("partner_discovery: outreach draft llm call failed", exc_info=True)
        return _fallback_outreach_draft(contact, ta_affiliated)

    draft = _sanitize_enrichment_field(result.text, 1000)
    if not draft:
        return _fallback_outreach_draft(contact, ta_affiliated)
    return draft


async def _draft_for_current_contact(
    state: FSMContext,
    partner_repository: PartnerRepository | None,
    workspace_context: WorkspaceContext | None,
    partner_enrichment_llm_provider: OpenAIChatProvider | None,
) -> str | None:
    """Returns the generated draft, or None when there is no PERSON contact
    currently in FSM state (caller decides how to respond to that case)."""
    data = await state.get_data()
    contact: dict | None = data.get(_STATE_CONTACT)
    if not contact or contact.get("contact_type") != "person":
        return None

    ta_affiliated = (
        await _is_ta_affiliated(partner_repository, workspace_context)
        if partner_repository is not None else False
    )
    return await _generate_outreach_draft(
        contact, partner_enrichment_llm_provider, ta_affiliated=ta_affiliated,
    )


@router.callback_query(F.data == PARTNER_CONTACT_DRAFT)
async def on_partner_contact_draft(
    callback: CallbackQuery,
    state: FSMContext,
    partner_repository: PartnerRepository | None = None,
    workspace_context: WorkspaceContext | None = None,
    partner_enrichment_llm_provider: OpenAIChatProvider | None = None,
) -> None:
    """"✉️ Подготовить первое сообщение" button on a PERSON contact card.
    Draft only - never sent automatically, always requires the human to
    review/edit/send it themselves."""
    await callback.answer()
    draft = await _draft_for_current_contact(
        state, partner_repository, workspace_context, partner_enrichment_llm_provider,
    )
    if callback.message is None:
        return
    if draft is None:
        await callback.message.answer(_DRAFT_UNAVAILABLE)
        return
    await callback.message.answer(_DRAFT_HEADER + draft)


async def _looks_like_draft_command(message: Message, state: FSMContext) -> bool:
    """Custom filter (not a handler): only matches when BOTH the message
    text looks like one of the task's own example phrasings AND a PERSON
    contact is already in FSM state - this is what lets natural free-text
    commands continue the current Partner Discovery -> Contact Discovery
    context instead of falling through to the generic task router (which
    would otherwise answer "не удалось уверенно определить маршрут").
    Returning False here lets aiogram try the next router in the chain
    (eventually app.handlers.tasks's general router) completely unaffected -
    this never swallows an unrelated message."""
    text = message.text or ""
    if not _DRAFT_COMMAND_RE.search(text):
        return False
    data = await state.get_data()
    contact = data.get(_STATE_CONTACT)
    return bool(contact and contact.get("contact_type") == "person")


@router.message(_looks_like_draft_command)
async def on_draft_command_text(
    message: Message,
    state: FSMContext,
    partner_repository: PartnerRepository | None = None,
    workspace_context: WorkspaceContext | None = None,
    partner_enrichment_llm_provider: OpenAIChatProvider | None = None,
) -> None:
    """Natural-language equivalent of on_partner_contact_draft - only ever
    invoked when _looks_like_draft_command already confirmed a PERSON
    contact is active, so `draft` here is never None in practice (the
    None-guard is defense in depth only, e.g. a race with on_partner_
    contact_back clearing state between the filter check and this call)."""
    draft = await _draft_for_current_contact(
        state, partner_repository, workspace_context, partner_enrichment_llm_provider,
    )
    if draft is None:
        await message.answer(_DRAFT_UNAVAILABLE)
        return
    await message.answer(_DRAFT_HEADER + draft)
