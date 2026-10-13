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

Cross-tenant claim guard (2026-10-07): starting outreach to a PERSON
(on_partner_contact_draft/on_draft_command_text/on_invitation_link_command_text)
claims that contact for this workspace via ContactClaimRepository for
CLAIM_COOLDOWN_DAYS - see _check_and_claim_contact. Partner Discovery
(prospect cards) and Contact Discovery alone (finding a PERSON without
starting outreach) never claim anything. A different workspace hitting an
active claim gets a neutral message (_CONTACT_CLAIMED_BY_OTHER) - never the
claiming workspace's identity. Still not built: auto-send, persistence of
discovered candidates/drafts themselves (only the claim fact is persisted).
"""

from __future__ import annotations

import asyncio
import logging
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.chat_provider import OpenAIChatProvider
from app.domain.partner_leads import PartnerLeadStatus, compute_lead_identity_key
from app.domain.partners import WorkspaceContext
from app.keyboards import (
    BTN_FIND_PARTNERS,
    BTN_PARTNER_LEADS,
    PARTNER_CONTACT_BACK,
    PARTNER_CONTACT_DRAFT,
    PARTNER_CONTACT_FIND,
    PARTNER_DISCOVERY_MORE,
    PARTNER_DISCOVERY_NEXT,
    PARTNER_LEAD_APPROVE,
    PARTNER_LEAD_OPEN_PREFIX,
    PARTNER_LEAD_QUEUE_DRAFT_PREFIX,
    PARTNER_LEAD_DRAFT,
    PARTNER_LEAD_SAVE,
    PARTNER_LEAD_SKIP,
    active_main_menu,
    partner_contact_card_keyboard,
    partner_discovery_card_keyboard,
    partner_discovery_retry_keyboard,
    partner_lead_card_keyboard,
    partner_lead_queue_keyboard,
)
from app.repositories.contact_claim_repository import ContactClaimRepository
from app.repositories.operational_event_repository import OperationalEventRepository
from app.repositories.partner_lead_repository import PartnerLeadRepository
from app.repositories.partner_repository import PartnerRepository
from app.services.contact_discovery import find_contact
from app.services.lead_qualification import QualificationUnavailable
from app.services.partner_pool import qualify_pool
from app.services.lead_qualification import qualify_candidates as _qualify_candidates
from app.services.partner_discovery import PartnerDiscoveryService
from app.services.telemetry import record_event
from app.services.partner_outreach import (
    NO_CHANNEL_TEXT,
    contact_status_note,
    format_channel_line,
    split_channels,
    build_explanation as _build_explanation,
    build_outreach_draft_prompt as _build_outreach_draft_prompt,
    candidate_to_dict as _candidate_to_dict,
    check_and_claim_contact as _check_and_claim_contact,
    clean_name as _clean_name,
    contact_identity_key as _contact_identity_key,
    contact_to_dict as _contact_to_dict,
    dict_to_prospect as _dict_to_prospect,
    apply_fallback_enrichment as _apply_fallback_enrichment,
    enrich_candidates as _enrich_candidates,
    fallback_outreach_draft as _fallback_outreach_draft,
    generate_outreach_draft as _generate_outreach_draft,
    greeting_name as _greeting_name,
    rank_for_club_partner_fit as _rank_for_club_partner_fit,
    sanitize_enrichment_field as _sanitize_enrichment_field,
    strip_private_contact_data as _strip_private_contact_data,
)
from app.services.partner_outreach import _ENRICHMENT_INSTRUCTIONS, _TA_AFFILIATED_ENRICHMENT_CLAUSE
from app.services.partner_outreach import _build_enrichment_prompt
from app.services.web_search.service import WebSearchService

router = Router(name="partner_discovery")
log = logging.getLogger(__name__)

_STATE_CANDIDATES = "partner_discovery_candidates"
_STATE_INDEX = "partner_discovery_index"
_STATE_CONTACT = "partner_discovery_contact"

_WORKSPACE_UNAVAILABLE = "Рабочее пространство недоступно."
_SEARCH_FAILED = "Не удалось выполнить поиск сейчас. Попробуйте немного позже."
_QUALIFICATION_FAILED = (
    "Не удалось завершить проверку кандидатов: сервис оценки не ответил. "
    "Это сбой, а не отсутствие подходящих партнёров — попробуйте запустить поиск ещё раз."
)
_NO_RESULTS = (
    "По текущим запросам не нашлось новых потенциальных партнёров.\n\n"
    "Можно попробовать ещё раз немного позже."
)
_BATCH_EXHAUSTED = "Это все найденные кандидаты в этой подборке."
_CONTACT_CHECK_FAILED = (
    "Не удалось проверить публичные контакты сейчас. Попробуйте немного позже."
)
# Cross-tenant claim guard (2026-10-07) - deliberately generic. Never names
# the other workspace/tenant/user in any way - see _check_and_claim_contact.
_CONTACT_CLAIMED_BY_OTHER = (
    "Этот контакт сейчас уже находится в работе. Попробую подобрать другого человека."
)

# Partner Discovery 2.0 lead pipeline (2026-10-11) - see
# app/domain/partner_leads.py / app/repositories/partner_lead_repository.py.
_LEAD_FEATURE_UNAVAILABLE = "Эта функция сейчас недоступна. Попробуйте немного позже."
_LEAD_SAVED = "📌 Сохранено в «Лиды партнёров»."
_LEAD_APPROVED = "✅ Одобрено. Сообщение пока не отправлено - подготовьте черновик, когда будете готовы."
_LEAD_SKIPPED = "⏭ Пропущено."
_LEAD_TRANSITION_BLOCKED = "Это действие сейчас недоступно для этого лида."
_NO_LEADS = (
    "Сохранённых лидов пока нет. Найдите кандидата и нажмите "
    "«✅ Одобрить» или «📌 Сохранить»."
)
# Russian display labels only - the stored status value (app.domain.
# partner_leads.PartnerLeadStatus) is never translated/changed, see
# PartnerLeadRepository's own enum-backed "status" column.
_LEAD_STATUS_LABELS = {
    "new": "Новый", "reviewed": "Просмотрен", "approved": "Одобрен",
    "contacted": "Связались", "replied": "Ответил", "interested": "Заинтересован",
    "declined": "Отклонён", "follow_up": "Повторный контакт",
}

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
    # Partner Discovery 2.0: only present once _qualify_if_v2 actually ran
    # (candidate carries "total_score") - absent with the flag off, so v0
    # cards render exactly as before.
    if "total_score" in candidate:
        why_fit = candidate.get("why_fit") or []
        hook = candidate.get("hook") or ""
        risk_flags = [f for f in (candidate.get("risk_flags") or []) if f != "no_public_contact"]
        extra = [
            f"Score: {candidate['total_score']}/100 · "
            f"Confidence: {candidate.get('confidence', 0):.2f}"
        ]
        if why_fit:
            extra.append("Почему подходит:\n" + "\n".join(f"• {item}" for item in why_fit))
        if hook:
            extra.append(f"Зацепка:\n{hook}")
        extra.append("Связаться:\n" + _render_contact_line(candidate))
        extra.append("Источники:\n" + "\n".join(_render_sources(candidate)))
        if risk_flags:
            extra.append("Риск/сомнение:\n" + ", ".join(risk_flags))
        text += "\n\n" + "\n\n".join(extra)
    has_more = index + 1 < len(candidates)
    return text, has_more


_CONTACT_TYPE_LABELS = {
    "telegram": "Telegram", "vk": "VK", "website": "Сайт", "email": "Email",
    "phone": "Телефон",
}


def _render_contact_line(candidate: dict) -> str:
    """Answers "как с ним связаться?" (task requirement) - explicitly says
    "Публичный контакт не найден" rather than silently omitting the line,
    so a strong candidate without any confirmed channel is never mistaken
    for one that simply wasn't checked. Shows the best channel plus up to
    2 more distinct ones found in the same evidence (task: "Telegram +
    email + сайт" style, never duplicates) - never more than 3 total, so
    the card never gets overloaded."""
    contact_type = candidate.get("contact_type")
    if not contact_type:
        return "Публичный контакт не найден."
    lines = [_format_contact(candidate.get("contact_type"), candidate.get("contact_value") or candidate.get("contact_url") or "")]
    for extra in (candidate.get("additional_contacts") or [])[:2]:
        lines.append(_format_contact(extra["contact_type"], extra.get("contact_value") or extra.get("contact_url") or ""))
    return "\n".join(lines)


def _format_contact(contact_type: str | None, value: str) -> str:
    label = _CONTACT_TYPE_LABELS.get(contact_type, contact_type or "")
    return f"{label}: {value}"


def _render_sources(candidate: dict) -> list[str]:
    """Every public URL the card's claims/contact actually trace back to -
    always at least the candidate's own url, see task requirement #2."""
    sources = [candidate["url"]]
    for item in candidate.get("evidence_claims") or []:
        url = item.get("source_url")
        if url and url not in sources:
            sources.append(url)
    contact_source = candidate.get("contact_source_url")
    if contact_source and contact_source not in sources:
        sources.append(contact_source)
    return sources


# ── Contact Discovery v0 integration (read-only layer on top of the same
# FSM batch; app/services/contact_discovery.py itself is not modified here) ──

def _render_channels(contact: dict) -> str:
    """Personal vs. project channels, each with its source; an honest
    "Канал связи не найден" when the page published none. A website or a
    community is never listed here as a channel."""
    personal, project = split_channels(contact)
    lines: list[str] = []
    if personal:
        lines.append("Личные контакты (названы рядом с этим человеком):")
        lines.extend(f"- {format_channel_line(c)}" for c in personal)
    if project:
        lines.append("Общие контакты проекта:")
        lines.extend(f"- {format_channel_line(c)}" for c in project)
    if not lines:
        lines.append(NO_CHANNEL_TEXT)
    elif not personal and contact.get("contact_type") == "person":
        lines.append("Личный контакт этого человека не найден.")
    return "\n".join(lines)


def _render_contact_card(contact: dict) -> str:
    if contact["contact_type"] == "person":
        name = _strip_private_contact_data(contact["display_name"])
        role = _strip_private_contact_data(contact.get("role"))
        project = _strip_private_contact_data(contact["project_name"])
        evidence = _strip_private_contact_data(contact["evidence_text"])
        confidence_label = _CONTACT_CONFIDENCE_LABELS.get(
            contact.get("confidence"), contact.get("confidence") or "",
        )
        link_label = contact.get("link_source_label") or ""
        return (
            "Найден публичный контакт\n\n"
            f"{name}\n"
            f"Роль: {role}\n"
            f"Проект: {project}\n\n"
            f"Почему выбран:\n{evidence}\n\n"
            f"Источник связи с проектом"
            f"{f' ({link_label})' if link_label else ''}:\n{contact['evidence_url']}\n\n"
            f"Подтверждение: {confidence_label}\n\n"
            f"{_render_channels(contact)}"
        )

    # UX fix (2026-10-07): the URL used to appear both here in the card
    # text AND as the "🌐 Открыть проект" button below it (same URL,
    # contact_dict["public_url"] in both places) - shown once now, via the
    # button only.
    project = _strip_private_contact_data(contact["project_name"])
    note = contact_status_note(contact) or (
        "Конкретный публичный представитель проекта не найден."
    )
    # The site/community address itself stays on the button below (shown once,
    # see the 2026-10-07 UX fix); the text only says what it is.
    return (
        f"{project}\n\n{note}\n\n"
        f"{_render_channels(contact)}\n\n"
        "Сайт проекта - по кнопке ниже. Это страница проекта, а не канал связи."
    )


async def _run_discovery(
    web_search_service: WebSearchService | None, *,
    rotation_seed: int | None = None, use_all_variants: bool = False,
    pool_limit: int | None = None,
) -> list[dict] | None:
    """Returns a list of candidate dicts, an empty list on zero results, or
    None on a search failure - the three cases the caller must render
    differently. rotation_seed (2026-10-07, the calling workspace_id) is
    passed straight through to PartnerDiscoveryService.find_candidates() -
    see that function's own docstring; None reproduces the exact original
    fixed query set. use_all_variants (Partner Discovery 2.0) runs every
    specialized query variant per category instead of one - only passed
    True when partner_discovery_v2_enabled."""
    if web_search_service is None or web_search_service.provider is None:
        return None
    service = PartnerDiscoveryService(web_search_service.provider)
    try:
        candidates = await service.find_candidates(
            rotation_seed=rotation_seed, use_all_variants=use_all_variants,
            **({"max_results": pool_limit} if pool_limit else {}),
        )
    except Exception:
        log.warning("partner_discovery: find_candidates failed", exc_info=True)
        return None
    return [_candidate_to_dict(c) for c in candidates]


async def _qualify_if_v2(
    candidates: list[dict],
    partner_enrichment_llm_provider: OpenAIChatProvider | None,
    *,
    partner_discovery_v2_enabled: bool,
    partner_min_score: int,
    partner_min_confidence: float,
    partner_max_results: int,
) -> list[dict] | None:
    """No-op (returns candidates unchanged) unless partner_discovery_v2_enabled
    - existing v0 card rendering/behavior stays byte-for-byte unchanged with
    the flag off. See app.services.lead_qualification.

    Returns None when the LLM qualification did not complete: the fallback
    score is not a completed check, so the caller must show a failure
    message, not "no candidates"."""
    if not partner_discovery_v2_enabled:
        return candidates
    try:
        return await _qualify_candidates(
            candidates, partner_enrichment_llm_provider,
            min_score=partner_min_score,
            min_confidence=partner_min_confidence,
            max_results=partner_max_results,
            require_llm=True,
        )
    except QualificationUnavailable as exc:
        log.warning("partner_discovery: qualification unavailable reason=%s", exc.reason)
        return None
    except Exception:
        log.warning("partner_discovery: qualification failed", exc_info=True)
        return None


_POOL_LIMIT = 10
_PARTIAL_CHECK_NOTE = (
    "Проверка неполная: оценено {checked} из {total} найденных кандидатов, "
    "остальные не успели проверить. Можно запустить поиск ещё раз."
)


async def _enrich_and_qualify(
    candidates: list[dict],
    partner_enrichment_llm_provider: OpenAIChatProvider | None,
    *,
    ta_affiliated: bool,
    partner_discovery_v2_enabled: bool,
    partner_min_score: int,
    partner_min_confidence: float,
    partner_max_results: int,
) -> tuple[list[dict] | None, str | None]:
    """(candidates, note). candidates is None when the LLM qualification did
    not complete (a failure, not "no candidates"); note is set when only
    part of the verified pool could be checked. With V2 off this is the
    unchanged enrich + display-rank path."""
    async def _enrich(batch: list[dict]) -> list[dict]:
        return await _enrich_candidates(
            batch, partner_enrichment_llm_provider, ta_affiliated=ta_affiliated,
        )

    if not partner_discovery_v2_enabled:
        return _rank_for_club_partner_fit(await _enrich(candidates)), None
    try:
        outcome = await qualify_pool(
            candidates, partner_enrichment_llm_provider,
            enrich=_enrich, enrich_fallback=_apply_fallback_enrichment,
            rank=_rank_for_club_partner_fit,
            min_score=partner_min_score, min_confidence=partner_min_confidence,
            max_results=partner_max_results,
        )
    except QualificationUnavailable as exc:
        log.warning("partner_discovery: qualification unavailable reason=%s", exc.reason)
        return None, None
    except Exception:
        log.warning("partner_discovery: qualification failed", exc_info=True)
        return None, None
    note = (
        _PARTIAL_CHECK_NOTE.format(checked=outcome.checked, total=outcome.total)
        if outcome.incomplete else None
    )
    return outcome.candidates, note


@router.message(F.text == BTN_FIND_PARTNERS)
async def on_find_partners(
    message: Message,
    state: FSMContext,
    workspace_context: WorkspaceContext | None,
    partner_repository: PartnerRepository,
    web_search_service: WebSearchService | None = None,
    partner_enrichment_llm_provider: OpenAIChatProvider | None = None,
    v2_menu_enabled: bool = False,
    partner_discovery_v2_enabled: bool = False,
    partner_min_score: int = 65,
    partner_min_confidence: float = 0.60,
    partner_max_results: int = 7,
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

    candidates = await _run_discovery(
        web_search_service, rotation_seed=workspace_context.workspace_id,
        use_all_variants=partner_discovery_v2_enabled,
        pool_limit=_POOL_LIMIT if partner_discovery_v2_enabled else None,
    )
    if candidates is None:
        await message.answer(_SEARCH_FAILED)
        return
    if not candidates:
        await message.answer(_NO_RESULTS, reply_markup=partner_discovery_retry_keyboard())
        return

    ta_affiliated = await _is_ta_affiliated(partner_repository, workspace_context)
    candidates, note = await _enrich_and_qualify(
        candidates, partner_enrichment_llm_provider, ta_affiliated=ta_affiliated,
        partner_discovery_v2_enabled=partner_discovery_v2_enabled,
        partner_min_score=partner_min_score,
        partner_min_confidence=partner_min_confidence,
        partner_max_results=partner_max_results,
    )
    if candidates is None:
        await message.answer(_QUALIFICATION_FAILED, reply_markup=partner_discovery_retry_keyboard())
        return
    if not candidates:
        await message.answer(
            _NO_RESULTS + (f"{chr(10)}{chr(10)}{note}" if note else ""),
            reply_markup=partner_discovery_retry_keyboard(),
        )
        return
    if note:
        await message.answer(note)
    await state.update_data(**{_STATE_CANDIDATES: candidates, _STATE_INDEX: 0})
    text, has_more = _render_card(candidates, 0)
    await message.answer(
        text, reply_markup=partner_discovery_card_keyboard(
            candidates[0]["url"], has_more, show_lead_actions="total_score" in candidates[0],
        ),
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
            text, reply_markup=partner_discovery_card_keyboard(
                candidates[index]["url"], has_more, show_lead_actions="total_score" in candidates[index],
            ),
        )


@router.callback_query(F.data == PARTNER_DISCOVERY_MORE)
async def on_partner_discovery_more(
    callback: CallbackQuery,
    state: FSMContext,
    web_search_service: WebSearchService | None = None,
    partner_enrichment_llm_provider: OpenAIChatProvider | None = None,
    workspace_context: WorkspaceContext | None = None,
    partner_repository: PartnerRepository | None = None,
    partner_discovery_v2_enabled: bool = False,
    partner_min_score: int = 65,
    partner_min_confidence: float = 0.60,
    partner_max_results: int = 7,
) -> None:
    await callback.answer()
    rotation_seed = workspace_context.workspace_id if workspace_context is not None else None
    candidates = await _run_discovery(
        web_search_service, rotation_seed=rotation_seed,
        use_all_variants=partner_discovery_v2_enabled,
        pool_limit=_POOL_LIMIT if partner_discovery_v2_enabled else None,
    )

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
    candidates, note = await _enrich_and_qualify(
        candidates, partner_enrichment_llm_provider, ta_affiliated=ta_affiliated,
        partner_discovery_v2_enabled=partner_discovery_v2_enabled,
        partner_min_score=partner_min_score,
        partner_min_confidence=partner_min_confidence,
        partner_max_results=partner_max_results,
    )
    if candidates is None:
        if callback.message is not None:
            await callback.message.answer(
                _QUALIFICATION_FAILED, reply_markup=partner_discovery_retry_keyboard(),
            )
        return
    if not candidates:
        if callback.message is not None:
            await callback.message.answer(
                _NO_RESULTS + (f"{chr(10)}{chr(10)}{note}" if note else ""),
                reply_markup=partner_discovery_retry_keyboard(),
            )
        return
    if note and callback.message is not None:
        await callback.message.answer(note)
    await state.update_data(**{_STATE_CANDIDATES: candidates, _STATE_INDEX: 0})
    text, has_more = _render_card(candidates, 0)
    if callback.message is not None:
        await callback.message.answer(
            text, reply_markup=partner_discovery_card_keyboard(
            candidates[0]["url"], has_more, show_lead_actions="total_score" in candidates[0],
        ),
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
            reply_markup=partner_discovery_card_keyboard(
                candidates[index]["url"], has_more,
                show_lead_actions="total_score" in candidates[index],
            ),
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
    "✉️ Черновик первого сообщения - проверьте и отредактируйте перед отправкой.\n"
    "Адресат и канал связи не подтверждены автоматически: прежде чем "
    "отправлять, убедитесь сами, кому и куда пишете.\n\n"
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

async def _draft_for_current_contact(
    state: FSMContext,
    partner_repository: PartnerRepository | None,
    workspace_context: WorkspaceContext | None,
    partner_enrichment_llm_provider: OpenAIChatProvider | None,
    contact_claim_repository: ContactClaimRepository | None = None,
) -> tuple[dict | None, str | None, bool]:
    """Returns (contact, draft_text, claimed_by_other).
    - contact is None: no PERSON contact currently in FSM state.
    - claimed_by_other True: a DIFFERENT workspace holds an active claim on
      this contact (2026-10-07) - draft_text is then also None, and the
      caller must show _CONTACT_CLAIMED_BY_OTHER, never run generation.
    - Otherwise draft_text is the generated draft - and THIS call is what
      claims the contact for this workspace (starting outreach), per the
      task's own policy: Partner Discovery and Contact Discovery alone
      never claim anything, only actually drafting a first message does.

    One profile fetch for both ta_affiliated and the sender's own
    business_name (2026-10-07) - same BusinessProfile, same authoritative,
    fail-closed source as _is_ta_affiliated; business_name is never
    hardcoded, it comes from THIS workspace's own profile, so a different
    tenant naturally gets a different value from the exact same code."""
    data = await state.get_data()
    contact: dict | None = data.get(_STATE_CONTACT)
    if not contact or contact.get("contact_type") != "person":
        return None, None, False

    if not await _check_and_claim_contact(contact, workspace_context, contact_claim_repository):
        return contact, None, True

    ta_affiliated = False
    sender_business_name: str | None = None
    if partner_repository is not None and workspace_context is not None:
        profile = await partner_repository.get_business_profile(workspace_context.workspace_id)
        if profile is not None:
            ta_affiliated = bool(getattr(profile, "ta_affiliated", False))
            sender_business_name = getattr(profile, "business_name", None) or None

    draft = await _generate_outreach_draft(
        contact, partner_enrichment_llm_provider,
        ta_affiliated=ta_affiliated, sender_business_name=sender_business_name,
    )
    return contact, draft, False


@router.callback_query(F.data == PARTNER_CONTACT_DRAFT)
async def on_partner_contact_draft(
    callback: CallbackQuery,
    state: FSMContext,
    partner_repository: PartnerRepository | None = None,
    workspace_context: WorkspaceContext | None = None,
    partner_enrichment_llm_provider: OpenAIChatProvider | None = None,
    contact_claim_repository: ContactClaimRepository | None = None,
) -> None:
    """"✉️ Подготовить первое сообщение" button on a PERSON contact card.
    Draft only - never sent automatically, always requires the human to
    review/edit/send it themselves."""
    await callback.answer()
    contact, draft, claimed_by_other = await _draft_for_current_contact(
        state, partner_repository, workspace_context, partner_enrichment_llm_provider,
        contact_claim_repository,
    )
    if callback.message is None:
        return
    if contact is None:
        await callback.message.answer(_DRAFT_UNAVAILABLE)
        return
    if claimed_by_other:
        await callback.message.answer(_CONTACT_CLAIMED_BY_OTHER)
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
    contact_claim_repository: ContactClaimRepository | None = None,
) -> None:
    """Natural-language equivalent of on_partner_contact_draft - only ever
    invoked when _looks_like_draft_command already confirmed a PERSON
    contact is active, so `contact` here is never None in practice (the
    None-guard is defense in depth only, e.g. a race with on_partner_
    contact_back clearing state between the filter check and this call)."""
    contact, draft, claimed_by_other = await _draft_for_current_contact(
        state, partner_repository, workspace_context, partner_enrichment_llm_provider,
        contact_claim_repository,
    )
    if contact is None:
        await message.answer(_DRAFT_UNAVAILABLE)
        return
    if claimed_by_other:
        await message.answer(_CONTACT_CLAIMED_BY_OTHER)
        return
    await message.answer(_DRAFT_HEADER + draft)


# ── Invitation WITH enrollment link (2026-10-07) - explicit intent only ────
# Separate from the cold-open draft above by design (task requirement #1/#2):
# the FIRST message to a freshly-found PERSON never includes a link, even
# when BusinessProfile.ta_enrollment_url is set. This second, explicitly-
# triggered intent is the ONLY place that URL is ever surfaced - and only
# for the workspace it belongs to (BusinessProfile.ta_enrollment_url is read
# via THIS workspace_context.workspace_id alone, so one tenant's link can
# never appear in another tenant's outreach). No LLM call here on purpose -
# a confirmed URL must never pass through a model that could alter/invent
# it; this is a plain deterministic template over already-confirmed fields.

_INVITATION_LINK_MISSING = (
    "Чтобы подготовить приглашение со ссылкой, сначала укажите свою "
    "персональную ссылку Travel Advantage: «⚙️ Профиль» → «🏢 Профиль "
    "компании» → «🔗 Моя ссылка Travel Advantage»."
)

_INVITATION_LINK_COMMAND_RE = re.compile(
    r"(дай\w*|пришли\w*|отправ\w*|подготов\w*).*ссылк"
    r"|приглашени\w*.*ссылк"
    r"|ссылк\w*.*регистрац",
    re.IGNORECASE,
)


def _fallback_invitation_with_link(contact: dict, enrollment_url: str) -> str:
    """2026-10-07 wording fix: no "вижу, что вы {role}" context clause and
    no "Регистрация по ссылке" (reads as mechanically pushing straight to
    sign-up) - just the link, framed as "learn more", with a low-pressure
    offer to answer questions. Never "как и обещал" or similar - this
    system has no way to know whether the recipient actually replied to
    the earlier cold message. enrollment_url is always THIS workspace's
    own (see _invitation_link_for_current_contact) - unchanged."""
    name = _strip_private_contact_data(contact.get("display_name") or "")
    greeting_name = _greeting_name(name)

    lines = [
        f"Здравствуйте, {greeting_name}!",
        "",
        "Отправляю ссылку, где можно подробнее познакомиться с Travel Advantage:",
        enrollment_url,
        "",
        "Если формат покажется интересным, могу коротко рассказать подробнее и "
        "ответить на вопросы.",
    ]
    return "\n".join(lines)


async def _invitation_link_for_current_contact(
    state: FSMContext,
    partner_repository: PartnerRepository | None,
    workspace_context: WorkspaceContext | None,
    contact_claim_repository: ContactClaimRepository | None = None,
) -> tuple[dict | None, str | None, bool]:
    """Returns (contact, invitation_text, claimed_by_other).
    - contact is None: no PERSON contact in FSM state.
    - claimed_by_other True: a DIFFERENT workspace holds an active claim
      (2026-10-07) - invitation_text is then also None; caller must show
      _CONTACT_CLAIMED_BY_OTHER, never _INVITATION_LINK_MISSING in that
      case (the claim check runs first - see _check_and_claim_contact).
    - invitation_text is None (claimed_by_other False): a contact exists
      and this workspace may proceed, but THIS workspace has no
      ta_enrollment_url set - the caller must show _INVITATION_LINK_MISSING,
      never substitute another workspace's link or a global default."""
    data = await state.get_data()
    contact: dict | None = data.get(_STATE_CONTACT)
    if not contact or contact.get("contact_type") != "person":
        return None, None, False

    if not await _check_and_claim_contact(contact, workspace_context, contact_claim_repository):
        return contact, None, True

    enrollment_url: str | None = None
    if partner_repository is not None and workspace_context is not None:
        profile = await partner_repository.get_business_profile(workspace_context.workspace_id)
        if profile is not None:
            enrollment_url = getattr(profile, "ta_enrollment_url", None) or None

    if not enrollment_url:
        return contact, None, False
    return contact, _fallback_invitation_with_link(contact, enrollment_url), False


async def _looks_like_invitation_link_command(message: Message, state: FSMContext) -> bool:
    """Same gating pattern as _looks_like_draft_command - only matches with
    a PERSON contact already in FSM state, so an unrelated message always
    falls through unaffected."""
    text = message.text or ""
    if not _INVITATION_LINK_COMMAND_RE.search(text):
        return False
    data = await state.get_data()
    contact = data.get(_STATE_CONTACT)
    return bool(contact and contact.get("contact_type") == "person")


@router.message(_looks_like_invitation_link_command)
async def on_invitation_link_command_text(
    message: Message,
    state: FSMContext,
    partner_repository: PartnerRepository | None = None,
    workspace_context: WorkspaceContext | None = None,
    contact_claim_repository: ContactClaimRepository | None = None,
) -> None:
    contact, invitation, claimed_by_other = await _invitation_link_for_current_contact(
        state, partner_repository, workspace_context, contact_claim_repository,
    )
    if contact is None:
        await message.answer(_DRAFT_UNAVAILABLE)
        return
    if claimed_by_other:
        await message.answer(_CONTACT_CLAIMED_BY_OTHER)
        return
    if invitation is None:
        await message.answer(_INVITATION_LINK_MISSING)
        return
    await message.answer(_DRAFT_HEADER + invitation)


# ── Partner Discovery 2.0 lead pipeline (2026-10-11) ─────────────────────
# Turns a shown candidate card into a persisted, status-tracked lead - but
# ONLY once the user actually presses one of the buttons below (minimal
# mutation, see partner_discovery_card_keyboard's own docstring): viewing
# a card, paging through the batch, or running Contact Discovery alone
# never writes anything. No outgoing message is ever sent without the
# explicit human action that already existed before this (✉️ Подготовить
# первое сообщение still requires its own tap) - this layer only adds a
# status/queue on top of that unchanged approval gate.

def _current_candidate(candidates: list[dict], index: int) -> dict | None:
    if not candidates or index >= len(candidates):
        return None
    return candidates[index]


async def _ensure_lead_saved(
    candidate: dict,
    workspace_context: WorkspaceContext | None,
    partner_lead_repository: PartnerLeadRepository | None,
):
    """Upserts the current candidate as a lead for this workspace - a
    re-save of the same candidate (same identity_key) refreshes its
    enrichment/score/confidence/contact fields but never resets an
    already-advanced status back to "new" (see
    PartnerLeadRepository.upsert_from_candidate)."""
    if partner_lead_repository is None or workspace_context is None:
        return None
    identity_key = compute_lead_identity_key(candidate.get("url") or "")
    return await partner_lead_repository.upsert_from_candidate(
        workspace_context.workspace_id, identity_key, candidate,
    )


async def _transition_current_lead(
    callback: CallbackQuery,
    state: FSMContext,
    workspace_context: WorkspaceContext | None,
    partner_lead_repository: PartnerLeadRepository | None,
    operational_event_repository: OperationalEventRepository | None,
    new_status: PartnerLeadStatus,
    success_message: str,
):
    await callback.answer()
    if partner_lead_repository is None or workspace_context is None:
        if callback.message is not None:
            await callback.message.answer(_LEAD_FEATURE_UNAVAILABLE)
        return
    data = await state.get_data()
    candidates: list[dict] = data.get(_STATE_CANDIDATES) or []
    index: int = data.get(_STATE_INDEX, 0)
    candidate = _current_candidate(candidates, index)
    if candidate is None:
        return

    lead = await _ensure_lead_saved(candidate, workspace_context, partner_lead_repository)
    if lead is None:
        if callback.message is not None:
            await callback.message.answer(_LEAD_FEATURE_UNAVAILABLE)
        return

    old_status = lead.status
    result = await partner_lead_repository.set_status(
        lead.lead_id, workspace_context.workspace_id, new_status,
    )
    if not result.ok:
        if callback.message is not None:
            await callback.message.answer(_LEAD_TRANSITION_BLOCKED)
        return

    await record_event(
        operational_event_repository, module="partner_leads", event_type="status_changed",
        success=True, workspace_id=workspace_context.workspace_id,
        telegram_user_id=workspace_context.telegram_user_id,
        metadata={
            "lead_id": lead.lead_id, "old_status": old_status.value,
            "new_status": new_status.value, "action_source": "telegram",
        },
    )
    if callback.message is not None:
        await callback.message.answer(success_message)


@router.callback_query(F.data == PARTNER_LEAD_SAVE)
async def on_partner_lead_save(
    callback: CallbackQuery,
    state: FSMContext,
    workspace_context: WorkspaceContext | None = None,
    partner_lead_repository: PartnerLeadRepository | None = None,
) -> None:
    """📌 Сохранить - persists the candidate as a lead (status NEW if this
    is the first save) without changing its status otherwise."""
    await callback.answer()
    if partner_lead_repository is None or workspace_context is None:
        if callback.message is not None:
            await callback.message.answer(_LEAD_FEATURE_UNAVAILABLE)
        return
    data = await state.get_data()
    candidates: list[dict] = data.get(_STATE_CANDIDATES) or []
    index: int = data.get(_STATE_INDEX, 0)
    candidate = _current_candidate(candidates, index)
    if candidate is None:
        return
    lead = await _ensure_lead_saved(candidate, workspace_context, partner_lead_repository)
    if lead is None:
        if callback.message is not None:
            await callback.message.answer(_LEAD_FEATURE_UNAVAILABLE)
        return
    if callback.message is not None:
        await callback.message.answer(_LEAD_SAVED)


@router.callback_query(F.data == PARTNER_LEAD_APPROVE)
async def on_partner_lead_approve(
    callback: CallbackQuery,
    state: FSMContext,
    workspace_context: WorkspaceContext | None = None,
    partner_lead_repository: PartnerLeadRepository | None = None,
    operational_event_repository: OperationalEventRepository | None = None,
) -> None:
    """✅ Одобрить - status -> approved. Never sends anything; outreach
    still requires the separate, pre-existing draft+human-approval flow."""
    await _transition_current_lead(
        callback, state, workspace_context, partner_lead_repository,
        operational_event_repository, PartnerLeadStatus.APPROVED, _LEAD_APPROVED,
    )


@router.callback_query(F.data == PARTNER_LEAD_SKIP)
async def on_partner_lead_skip(
    callback: CallbackQuery,
    state: FSMContext,
    workspace_context: WorkspaceContext | None = None,
    partner_lead_repository: PartnerLeadRepository | None = None,
    operational_event_repository: OperationalEventRepository | None = None,
) -> None:
    """⏭ Пропустить - status -> declined. History (the row) is kept, not
    deleted - a later ✅/📌 on the same candidate can still reinstate it
    (DECLINED -> REVIEWED is an allowed transition)."""
    await callback.answer()
    if partner_lead_repository is None or workspace_context is None:
        if callback.message is not None:
            await callback.message.answer(_LEAD_FEATURE_UNAVAILABLE)
        return
    data = await state.get_data()
    candidates: list[dict] = data.get(_STATE_CANDIDATES) or []
    index: int = data.get(_STATE_INDEX, 0)
    candidate = _current_candidate(candidates, index)
    if candidate is None:
        return
    lead = await _ensure_lead_saved(candidate, workspace_context, partner_lead_repository)
    if lead is None:
        if callback.message is not None:
            await callback.message.answer(_LEAD_FEATURE_UNAVAILABLE)
        return
    old_status = lead.status
    # NEW -> DECLINED is an allowed transition (see ALLOWED_TRANSITIONS);
    # a lead already further along (e.g. CONTACTED) simply keeps its
    # current status - "skip" never demotes progress, it only has an
    # effect on a lead still at NEW/REVIEWED.
    result = await partner_lead_repository.set_status(
        lead.lead_id, workspace_context.workspace_id, PartnerLeadStatus.DECLINED,
    )
    if result.ok:
        await record_event(
            operational_event_repository, module="partner_leads", event_type="status_changed",
            success=True, workspace_id=workspace_context.workspace_id,
            telegram_user_id=workspace_context.telegram_user_id,
            metadata={
                "lead_id": lead.lead_id, "old_status": old_status.value,
                "new_status": PartnerLeadStatus.DECLINED.value, "action_source": "telegram",
            },
        )
    if callback.message is not None:
        await callback.message.answer(_LEAD_SKIPPED)


@router.callback_query(F.data == PARTNER_LEAD_DRAFT)
async def on_partner_lead_draft(
    callback: CallbackQuery,
    state: FSMContext,
    web_search_service: WebSearchService | None = None,
    partner_enrichment_llm_provider: OpenAIChatProvider | None = None,
    partner_repository: PartnerRepository | None = None,
    workspace_context: WorkspaceContext | None = None,
    contact_claim_repository: ContactClaimRepository | None = None,
) -> None:
    """📝 Черновик сообщения - reuses the EXISTING Contact Discovery +
    outreach-draft chain verbatim (find_contact -> _draft_for_current_contact),
    never a new drafting mechanism. Never sends anything - shows the draft
    for manual review/edit, exactly like ✉️ Подготовить первое сообщение."""
    await callback.answer()
    data = await state.get_data()
    candidates: list[dict] = data.get(_STATE_CANDIDATES) or []
    index: int = data.get(_STATE_INDEX, 0)
    candidate = _current_candidate(candidates, index)
    if candidate is None:
        return

    prospect = _dict_to_prospect(candidate)
    web_search_provider = (
        web_search_service.provider if web_search_service is not None else None
    )
    try:
        contact = await find_contact(
            prospect, llm_provider=partner_enrichment_llm_provider,
            web_search_provider=web_search_provider,
        )
    except Exception:
        log.warning("partner_discovery: contact discovery failed", exc_info=True)
        if callback.message is not None:
            await callback.message.answer(_CONTACT_CHECK_FAILED)
        return

    contact_dict = _contact_to_dict(contact)
    if contact_dict.get("contact_type") != "person":
        if callback.message is not None:
            await callback.message.answer(_DRAFT_UNAVAILABLE)
        return
    await state.update_data(**{_STATE_CONTACT: contact_dict})

    _, draft, claimed_by_other = await _draft_for_current_contact(
        state, partner_repository, workspace_context, partner_enrichment_llm_provider,
        contact_claim_repository,
    )
    if claimed_by_other:
        if callback.message is not None:
            await callback.message.answer(_CONTACT_CLAIMED_BY_OTHER)
        return
    if draft is None:
        if callback.message is not None:
            await callback.message.answer(_DRAFT_UNAVAILABLE)
        return
    if callback.message is not None:
        await callback.message.answer(_DRAFT_HEADER + draft)


def _render_lead_queue(leads: list) -> str:
    """Russian status labels are display-only (_LEAD_STATUS_LABELS) - the
    stored PartnerLeadStatus enum value is never touched, see
    PartnerLeadRepository."""
    lines = ["📋 Лиды партнёров"]
    for lead in leads:
        status_label = _LEAD_STATUS_LABELS.get(lead.status.value, lead.status.value)
        contact = (
            f"{lead.contact_type}: {lead.contact_value}" if lead.contact_type else "контакт не найден"
        )
        score = lead.score if lead.score is not None else "-"
        card = (
            f"\n{lead.name}\n"
            f"Статус: {status_label} · Score: {score}\n"
            f"Контакт: {contact}"
        )
        if lead.next_action:
            card += f"\nСледующий шаг: {lead.next_action}"
        lines.append(card)
    return "\n".join(lines)


@router.message(F.text == BTN_PARTNER_LEADS)
async def on_partner_leads_queue(
    message: Message,
    workspace_context: WorkspaceContext | None,
    partner_lead_repository: PartnerLeadRepository | None = None,
    v2_menu_enabled: bool = False,
) -> None:
    """📋 Лиды партнёров - a simple review queue, no analytics: new/
    reviewed/approved/follow_up leads for this workspace, most recently
    updated first. Minimal filter set per the task's own scope."""
    if workspace_context is None or partner_lead_repository is None:
        await message.answer(
            _LEAD_FEATURE_UNAVAILABLE, reply_markup=active_main_menu(v2_menu_enabled),
        )
        return
    leads = await partner_lead_repository.list_for_workspace(
        workspace_context.workspace_id,
        statuses=[
            PartnerLeadStatus.NEW, PartnerLeadStatus.REVIEWED,
            PartnerLeadStatus.APPROVED, PartnerLeadStatus.FOLLOW_UP,
        ],
    )
    if not leads:
        await message.answer(_NO_LEADS)
        return
    await message.answer(_render_lead_queue(leads), reply_markup=partner_lead_queue_keyboard(leads))


# ── Saved lead card + draft (from the 📋 queue) ─────────────────────────────
# Works ONLY from the stored PartnerLead row: no new search, no contact
# discovery. Drafting reuses _generate_outreach_draft verbatim; sending stays
# entirely manual.

_LEAD_NOT_FOUND = "Лид не найден."


def _lead_id_from_callback(data: str | None, prefix: str) -> int | None:
    if not data or not data.startswith(prefix):
        return None
    tail = data[len(prefix):]
    return int(tail) if tail.isdigit() else None


async def _owned_lead(
    callback: CallbackQuery, prefix: str, workspace_context: WorkspaceContext | None,
    partner_lead_repository: PartnerLeadRepository | None,
):
    """The lead, but only if it belongs to the caller workspace (the
    repository query filters by workspace_id); otherwise None."""
    lead_id = _lead_id_from_callback(callback.data, prefix)
    if lead_id is None or workspace_context is None or partner_lead_repository is None:
        return None
    return await partner_lead_repository.get(lead_id, workspace_context.workspace_id)


def _render_lead_card(lead) -> str:
    status_label = _LEAD_STATUS_LABELS.get(lead.status.value, lead.status.value)
    contact = f"{lead.contact_type}: {lead.contact_value}" if lead.contact_type else "контакт не найден"
    confidence = f"{lead.confidence:.2f}" if lead.confidence is not None else "-"
    score = lead.score if lead.score is not None else "-"
    lines = [
        f"📂 {lead.name}",
        f"Статус: {status_label} · Score: {score} · Уверенность: {confidence}",
        f"Контакт: {contact}",
    ]
    if lead.why_fit:
        lines.append("Почему подходит:\n" + "\n".join(f"- {item}" for item in lead.why_fit))
    if lead.hook:
        lines.append(f"Зацепка: {lead.hook}")
    lines.append(f"Источник: {lead.source_url}")
    if lead.next_action:
        lines.append(f"Следующий шаг: {lead.next_action}")
    return "\n".join(lines)


@router.callback_query(F.data.startswith(PARTNER_LEAD_OPEN_PREFIX))
async def on_partner_lead_open(
    callback: CallbackQuery,
    workspace_context: WorkspaceContext | None = None,
    partner_lead_repository: PartnerLeadRepository | None = None,
) -> None:
    await callback.answer()
    if callback.message is None:
        return
    lead = await _owned_lead(callback, PARTNER_LEAD_OPEN_PREFIX, workspace_context, partner_lead_repository)
    if lead is None:
        await callback.message.answer(_LEAD_NOT_FOUND)
        return
    await callback.message.answer(
        _render_lead_card(lead),
        reply_markup=partner_lead_card_keyboard(lead.lead_id, lead.source_url),
    )


def _contact_dict_from_lead(lead) -> dict:
    """Stored lead -> the contact dict shape the shared outreach-draft
    generator expects. Only stored fields are used."""
    return {
        "contact_type": "person",
        "addressee_confirmed": False,  # shared mailbox / company page, no named person
        "display_name": lead.name,
        "role": "",
        "project_name": lead.name,
        "hook": lead.hook or "",
        "evidence_text": "; ".join(item for item in lead.why_fit if item),
        "evidence_url": lead.source_url,
        "public_url": lead.source_url,
    }


@router.callback_query(F.data.startswith(PARTNER_LEAD_QUEUE_DRAFT_PREFIX))
async def on_partner_lead_queue_draft(
    callback: CallbackQuery,
    partner_enrichment_llm_provider: OpenAIChatProvider | None = None,
    partner_repository: PartnerRepository | None = None,
    workspace_context: WorkspaceContext | None = None,
    partner_lead_repository: PartnerLeadRepository | None = None,
    contact_claim_repository: ContactClaimRepository | None = None,
) -> None:
    """Подготовить черновик on a saved lead card. Draft only - shown next to
    the stored contact for the human to send manually; nothing is sent and
    no search / contact discovery is run.

    The callback is acknowledged exactly once, FIRST - before any DB or LLM
    work - because Telegram expires a callback query after a short time; the
    result is then delivered as a normal message, never via a second answer()
    on the same callback."""
    try:
        await callback.answer()
    except TelegramBadRequest:
        # Update delivered late (e.g. after a polling reconnect): the query is
        # already expired, but the draft can still be sent as a message.
        log.info("partner_discovery: lead draft callback already expired")
    if callback.message is None:
        return
    lead = await _owned_lead(
        callback, PARTNER_LEAD_QUEUE_DRAFT_PREFIX, workspace_context, partner_lead_repository,
    )
    if lead is None:
        await callback.message.answer(_LEAD_NOT_FOUND)
        return

    contact = _contact_dict_from_lead(lead)
    if not await _check_and_claim_contact(contact, workspace_context, contact_claim_repository):
        await callback.message.answer(_CONTACT_CLAIMED_BY_OTHER)
        return
    await callback.message.answer("Готовлю черновик…")

    ta_affiliated = False
    sender_business_name: str | None = None
    if partner_repository is not None:
        profile = await partner_repository.get_business_profile(workspace_context.workspace_id)
        if profile is not None:
            ta_affiliated = bool(getattr(profile, "ta_affiliated", False))
            sender_business_name = getattr(profile, "business_name", None) or None

    draft = await _generate_outreach_draft(
        contact, partner_enrichment_llm_provider,
        ta_affiliated=ta_affiliated, sender_business_name=sender_business_name,
    )
    contact_line = (
        f"{lead.contact_type}: {lead.contact_value}" if lead.contact_type
        else "публичный контакт не сохранён - используйте страницу источника"
    )
    await callback.message.answer(
        f"{_DRAFT_HEADER}{draft}\n\n"
        f"Контакт для отправки: {contact_line}\n"
        f"Источник: {lead.source_url}\n"
        "Отправка - только вручную."
    )
