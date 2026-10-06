"""Partner Discovery v0 Telegram UI: read-only front end over the already-
stable app.services.partner_discovery.PartnerDiscoveryService.

This module changes NOTHING about discovery itself - no search queries, no
scoring/ranking, no dedupe, no denylist, no provider, no page verification.
It only renders what the service returns, one candidate card at a time, and
walks the user through the batch already in hand.

Universal Orchestrator function, not Travel Advantage-specific: the button
is shown for BusinessProfile.business_type == "club_partner" alone.
ta_affiliated plays no role in this button's visibility - see
app/handlers/menu.py's own _resolve_web_resource_links docstring for the
established business_type-vs-ta_affiliated distinction this reuses.

Stateless by design: candidates for the current batch live only in FSM
state (state.update_data), never in a database. "⏭ Следующий" only walks
that in-memory batch - it triggers no new search. Only "🔄 Найти ещё"
starts a fresh PartnerDiscoveryService.find_candidates() call.

Not yet built (separate step, after this UX is validated): "✍️ Подготовить
обращение"/outreach drafting, auto-send, persistence of discovered
candidates.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.domain.partners import WorkspaceContext
from app.domain.prospects import ProspectCandidate
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
    """Returns (text, has_more_in_batch)."""
    candidate = candidates[index]
    type_label = _TYPE_LABELS.get(candidate["prospect_type"], candidate["prospect_type"])
    evidence_label = _EVIDENCE_SOURCE_LABELS.get(
        candidate["evidence_source"], candidate["evidence_source"],
    )
    text = (
        f"{index + 1}/{len(candidates)}\n\n"
        f"{candidate['name']}\n"
        f"Тип: {type_label}\n"
        f"Fit: {candidate['fit']}\n\n"
        f"Почему найден:\n{candidate['evidence']}\n\n"
        f"Источник:\n{candidate['url']}\n\n"
        f"Основание: {evidence_label}"
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

    await state.update_data(**{_STATE_CANDIDATES: candidates, _STATE_INDEX: 0})
    text, has_more = _render_card(candidates, 0)
    if callback.message is not None:
        await callback.message.answer(
            text, reply_markup=partner_discovery_card_keyboard(candidates[0]["url"], has_more),
        )
