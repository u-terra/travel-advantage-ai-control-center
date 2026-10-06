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

Not yet built (separate step, after this UX is validated): "✍️ Подготовить
обращение"/outreach drafting, auto-send, persistence of discovered
candidates.
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
from app.domain.partners import WorkspaceContext
from app.domain.prospects import ProspectCandidate, ProspectType
from app.keyboards import (
    BTN_FIND_PARTNERS,
    PARTNER_DISCOVERY_MORE,
    PARTNER_DISCOVERY_NEXT,
    active_main_menu,
    partner_discovery_card_keyboard,
    partner_discovery_retry_keyboard,
)
from app.repositories.partner_repository import PartnerRepository
from app.services.partner_discovery import PartnerDiscoveryService
from app.services.web_search.service import WebSearchService

router = Router(name="partner_discovery")
log = logging.getLogger(__name__)

_STATE_CANDIDATES = "partner_discovery_candidates"
_STATE_INDEX = "partner_discovery_index"

_WORKSPACE_UNAVAILABLE = "Рабочее пространство недоступно."
_SEARCH_FAILED = "Не удалось выполнить поиск сейчас. Попробуйте немного позже."
_NO_RESULTS = (
    "По текущим запросам не нашлось новых потенциальных партнёров.\n\n"
    "Можно попробовать ещё раз немного позже."
)
_BATCH_EXHAUSTED = "Это все найденные кандидаты в этой подборке."

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
    "guide": "Работает гидом/экскурсоводом.",
    "travel_creator": "Ведёт публичный travel-блог/канал о путешествиях.",
    "travel_club": "Объединяет сообщество путешественников.",
    "other_travel_project": "Публичный travel-проект.",
}

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

- "display_name": реальное публичное название проекта/бренда/автора. Коротко (1-4 слова). Никогда не URL. Ничего не придумывай - бери только то, что явно следует из title/evidence. Если уверенно определить название нельзя - используй читаемый hostname (без www, без схемы).
- "reason": 1-2 коротких предложения, только на основе предоставленного public evidence. Опиши (а) чем публично занимается субъект и (б) почему это делает его релевантным потенциальным контактом для travel/club-partner use case. СТРОГО ЗАПРЕЩЕНО: утверждать или намекать, что субъект хочет партнёрство; упоминать интерес к доходу или выгоде; обещать какой-либо результат; упоминать Travel Advantage без явного фактического основания в evidence; включать телефоны, email, пункты меню, cookie/согласие, формы заявок, цены/рассрочки или любой служебный текст сайта.

Верни СТРОГО валидный JSON-массив из ровно {count} объектов, в ТОМ ЖЕ порядке, что и во входном списке, без markdown-разметки, без пояснений до или после JSON. Формат каждого элемента: {{"display_name": "...", "reason": "..."}}"""


def _build_enrichment_prompt(candidates: list[dict]) -> str:
    items = []
    for i, candidate in enumerate(candidates, 1):
        items.append(
            f"{i}. URL: {candidate['url']}\n"
            f"   Заголовок (title/search result): {candidate['name'] or '(нет)'}\n"
            f"   Предполагаемый тип (подсказка, не факт): {candidate['prospect_type']}\n"
            f"   Источник evidence: {candidate['evidence_source']}\n"
            f"   Evidence (public text): {candidate['evidence'][:600]}"
        )
    header = _ENRICHMENT_INSTRUCTIONS.format(count=len(candidates))
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


def _parse_enrichment_response(raw_text: str, expected_count: int) -> list[dict] | None:
    """Returns a list of {"display_name", "reason"} dicts, or None on ANY
    shape/content problem - the caller must then use the deterministic
    fallback for the WHOLE batch, never a partial mix."""
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, list) or len(parsed) != expected_count:
        return None

    result: list[dict] = []
    for item in parsed:
        if not isinstance(item, dict):
            return None
        display_name = _sanitize_enrichment_field(item.get("display_name"), 60)
        reason = _sanitize_enrichment_field(item.get("reason"), _ENRICHMENT_MAX_RESPONSE_LEN)
        if not display_name or not reason:
            return None
        result.append({"display_name": display_name, "reason": reason})
    return result


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
    candidates: list[dict], enrichment_llm_provider: OpenAIChatProvider | None,
) -> list[dict]:
    """Exactly one LLM call for the whole batch (never per-candidate, never
    on "Следующий" - callers only invoke this right after a fresh search).
    Any failure - no provider, no credentials, transport error, invalid/
    malformed JSON, wrong item count - falls back to the deterministic,
    already-noise-free cleanup for every candidate in the batch."""
    if not candidates:
        return candidates
    if enrichment_llm_provider is None or not enrichment_llm_provider.config.api_key:
        return _apply_fallback_enrichment(candidates)

    prompt = _build_enrichment_prompt(candidates)
    try:
        result = await asyncio.to_thread(enrichment_llm_provider.generate, message=prompt)
    except Exception:
        log.warning("partner_discovery: enrichment LLM call failed", exc_info=True)
        return _apply_fallback_enrichment(candidates)

    parsed = _parse_enrichment_response(result.text, len(candidates))
    if parsed is None:
        log.warning("partner_discovery: enrichment response was not valid, using fallback")
        return _apply_fallback_enrichment(candidates)

    return [
        {**candidate, **enriched}
        for candidate, enriched in zip(candidates, parsed, strict=True)
    ]


async def _is_club_partner(
    partner_repository: PartnerRepository, workspace_context: WorkspaceContext,
) -> bool:
    profile = await partner_repository.get_business_profile(workspace_context.workspace_id)
    return profile is not None and profile.business_type == "club_partner"


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
    enrichment_llm_provider: OpenAIChatProvider | None = None,
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

    candidates = await _enrich_candidates(candidates, enrichment_llm_provider)
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
    enrichment_llm_provider: OpenAIChatProvider | None = None,
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

    candidates = await _enrich_candidates(candidates, enrichment_llm_provider)
    await state.update_data(**{_STATE_CANDIDATES: candidates, _STATE_INDEX: 0})
    text, has_more = _render_card(candidates, 0)
    if callback.message is not None:
        await callback.message.answer(
            text, reply_markup=partner_discovery_card_keyboard(candidates[0]["url"], has_more),
        )
