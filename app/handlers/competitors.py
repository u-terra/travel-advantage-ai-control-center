"""Раздел «Мои конкуренты»: workspace-список ссылок на конкурентов.

Только хранение и просмотр ссылок, которые владелец workspace сам считает
конкурентами — база для будущего конкурентного анализа, не сам анализ.
Ничего не скачивается и не анализируется автоматически.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import MagicData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.domain.competitor_discovery import CandidateClassification, CandidateStatus
from app.domain.competitors import Competitor
from app.domain.partners import WorkspaceContext
from app.keyboards import (
    BTN_V2_COMPETITORS,
    BTN_V2_MAIN_MENU,
    COMPETITOR_REGISTRY_ADD,
    COMPETITOR_REGISTRY_RENAME_PREFIX,
    COMPETITOR_OPEN_PREFIX,
    COMPETITOR_ANALYZE_PREFIX,
    COMPETITOR_NEWS_PREFIX,
    COMPETITOR_IDEAS_PREFIX,
    COMPETITOR_CREATE_PREFIX,
    COMPETITOR_REFRESH_PREFIX,
    COMPETITOR_DISCOVERY_START,
    COMPETITOR_DISCOVERY_VIEW_PREFIX,
    COMPETITOR_DISCOVERY_ADD_PREFIX,
    COMPETITOR_DISCOVERY_IGNORE_PREFIX,
    active_main_menu,
    competitor_candidate_keyboard,
    competitor_card_keyboard,
    competitor_opportunities_keyboard,
    competitors_list_keyboard,
    v2_back_keyboard,
)
from app.repositories.competitor_repository import (
    CompetitorAddressError,
    CompetitorLabelError,
    CompetitorRepository,
)
from app.repositories.conversation_state_repository import ConversationStateRepository
from app.repositories.partner_repository import PartnerRepository
from app.repositories.usage_ledger_repository import UsageLedgerRepository
from app.repositories.workspace_signal_repository import WorkspaceSignalRepository
from app.services.competitor_discovery import CompetitorDiscoveryService
from app.services.competitor_intelligence import (
    CompetitorIntelligenceService,
    CompetitorIntelligenceUnavailable,
)
from app.services.generation_request_builder import build_provider_generation_request
from app.services.knowledge_service import KnowledgeService
from app.services.llm.base import LLMProvider
from app.services.material_orchestration import MaterialOrchestrationService
from app.services.user_style import UserStyleService

router = Router(name="competitors")
log = logging.getLogger(__name__)

_LIST_LIMIT = 20
_BUTTON_LABEL_MAX_CHARS = 40

_UNAVAILABLE = "Рабочее пространство недоступно."
_EMPTY = (
    "🎯 Мои конкуренты\n\n"
    "Пока не сохранено ни одной ссылки.\n"
    "Добавьте сайт или ресурс конкурента — Оркестратор сможет использовать "
    "его для анализа в вашем рабочем пространстве."
)
_ADD_PROMPT = (
    "Пришлите ссылку на сайт или ресурс конкурента "
    "(начинается с http:// или https://)."
)
_SAVED = (
    "Конкурент сохранён. Ссылка будет использоваться Оркестратором для "
    "анализа в вашем рабочем пространстве."
)
_RENAME_PROMPT = "Как назвать этого конкурента? Например: ТурКлуб"
_RENAME_NOT_FOUND = "Не удалось найти конкурента — возможно, он уже удалён."
_RENAME_SAVED = "Название сохранено."
_INTELLIGENCE_MISSING = "Сначала нажмите «🔎 Анализ конкурента» или «🔄 Обновить»."


class AddCompetitor(StatesGroup):
    waiting_for_url = State()


class RenameCompetitor(StatesGroup):
    waiting_for_label = State()


_RENAME_COMPETITOR_ID_KEY = "rename_competitor_id"
_RENAME_QUESTION_TYPE = "competitor_label"
# F2A: TTL for the persisted PendingQuestion mirror of this FSM step -
# generous enough to outlive a realistic "user steps away before answering".
_RENAME_QUESTION_TTL = timedelta(minutes=15)


def _line(competitor: Competitor) -> str:
    if competitor.label != competitor.url:
        return f"{competitor.label} — {competitor.url}"
    return competitor.label


def _summary(competitors: tuple[Competitor, ...]) -> str:
    lines = ["🎯 Мои конкуренты", "", f"Сохранено: {len(competitors)}", ""]
    for index, competitor in enumerate(competitors, start=1):
        lines.append(f"{index}. {_line(competitor)}")
    return "\n".join(lines)


def _button_label(competitor: Competitor) -> str:
    text = competitor.label
    if len(text) <= _BUTTON_LABEL_MAX_CHARS:
        return text
    return text[: _BUTTON_LABEL_MAX_CHARS - 1].rstrip() + "…"


def _list_keyboard(competitors: tuple[Competitor, ...]):
    return competitors_list_keyboard(
        tuple((competitor.id, _button_label(competitor)) for competitor in competitors)
    )


@router.message(MagicData(F.v2_menu_enabled), F.text == BTN_V2_COMPETITORS)
async def show_competitors(
    message: Message,
    state: FSMContext,
    competitor_repository: CompetitorRepository,
    workspace_context: WorkspaceContext | None,
) -> None:
    await state.clear()
    if workspace_context is None:
        await message.answer(_UNAVAILABLE, reply_markup=v2_back_keyboard())
        return

    competitors = tuple(
        await competitor_repository.list_for_workspace(
            workspace_context.workspace_id, limit=_LIST_LIMIT
        )
    )
    if not competitors:
        await message.answer(_EMPTY, reply_markup=competitors_list_keyboard())
        return

    await message.answer(
        _summary(competitors),
        reply_markup=_list_keyboard(competitors),
        disable_web_page_preview=True,
    )


@router.callback_query(
    MagicData(F.v2_menu_enabled), F.data == COMPETITOR_REGISTRY_ADD
)
async def start_add_competitor(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddCompetitor.waiting_for_url)
    await callback.answer()
    if callback.message is not None:
        await callback.message.answer(_ADD_PROMPT, reply_markup=v2_back_keyboard())


@router.message(
    MagicData(F.v2_menu_enabled),
    AddCompetitor.waiting_for_url,
    F.text == BTN_V2_MAIN_MENU,
)
async def cancel_add_competitor(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Главное меню. Выберите нужную задачу.", reply_markup=active_main_menu(True)
    )


@router.message(MagicData(F.v2_menu_enabled), AddCompetitor.waiting_for_url)
async def receive_competitor_url(
    message: Message,
    state: FSMContext,
    competitor_repository: CompetitorRepository,
    workspace_context: WorkspaceContext | None,
) -> None:
    address = (message.text or "").strip()

    if workspace_context is None:
        await message.answer(_UNAVAILABLE, reply_markup=v2_back_keyboard())
        return

    try:
        await competitor_repository.add_competitor(
            workspace_context.workspace_id, address
        )
    except CompetitorAddressError as exc:
        await message.answer(f"Не получилось: {exc}", reply_markup=v2_back_keyboard())
        return

    await state.clear()
    await message.answer(_SAVED, reply_markup=active_main_menu(True))


@router.callback_query(
    MagicData(F.v2_menu_enabled), F.data.startswith(COMPETITOR_REGISTRY_RENAME_PREFIX)
)
async def start_rename_competitor(
    callback: CallbackQuery,
    state: FSMContext,
    workspace_context: WorkspaceContext | None = None,
    conversation_state_repository: ConversationStateRepository | None = None,
) -> None:
    raw_id = (callback.data or "").removeprefix(COMPETITOR_REGISTRY_RENAME_PREFIX)
    await callback.answer()
    if not raw_id.isdigit() or int(raw_id) <= 0:
        return
    competitor_id = int(raw_id)
    await state.update_data(**{_RENAME_COMPETITOR_ID_KEY: competitor_id})
    await state.set_state(RenameCompetitor.waiting_for_label)
    # F2A PendingQuestion pilot: persisted in parallel to the FSM state above
    # - the FSM remains the actual mechanism driving this flow (see module
    # docstring/F2A report). Best-effort: a failure here must not stop the
    # existing rename flow from working exactly as before.
    if conversation_state_repository is not None and workspace_context is not None:
        try:
            await conversation_state_repository.create_question(
                workspace_context.workspace_id, workspace_context.telegram_user_id,
                _RENAME_QUESTION_TYPE, _RENAME_PROMPT,
                subject_ref_type="competitor", subject_ref_id=competitor_id,
                expires_at=(datetime.now(timezone.utc) + _RENAME_QUESTION_TTL).isoformat(),
            )
            # F2A subject_ref pilot: the user just opened a *known, already
            # persisted* competitor by id - not a guess derived from text
            # (see the F2A "не создавать fictitious subject_ref" constraint).
            await conversation_state_repository.patch_state(
                workspace_context.workspace_id, workspace_context.telegram_user_id,
                active_module="competitors", current_task="rename_competitor",
                current_subject_ref_type="competitor", current_subject_ref_id=competitor_id,
                last_action="rename_competitor_started",
            )
        except Exception:
            log.warning("competitors: conversation state bookkeeping failed for rename start")
    if callback.message is not None:
        await callback.message.answer(_RENAME_PROMPT, reply_markup=v2_back_keyboard())


@router.message(
    MagicData(F.v2_menu_enabled),
    RenameCompetitor.waiting_for_label,
    F.text == BTN_V2_MAIN_MENU,
)
async def cancel_rename_competitor(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Главное меню. Выберите нужную задачу.", reply_markup=active_main_menu(True)
    )


@router.message(MagicData(F.v2_menu_enabled), RenameCompetitor.waiting_for_label)
async def receive_competitor_label(
    message: Message,
    state: FSMContext,
    competitor_repository: CompetitorRepository,
    workspace_context: WorkspaceContext | None,
    conversation_state_repository: ConversationStateRepository | None = None,
) -> None:
    label = (message.text or "").strip()
    data = await state.get_data()
    competitor_id = data.get(_RENAME_COMPETITOR_ID_KEY)
    await state.clear()

    if workspace_context is None:
        await message.answer(_UNAVAILABLE, reply_markup=v2_back_keyboard())
        return
    if not isinstance(competitor_id, int):
        await message.answer(_RENAME_NOT_FOUND, reply_markup=active_main_menu(True))
        return

    try:
        updated = await competitor_repository.update_label(
            workspace_context.workspace_id, competitor_id, label,
        )
    except CompetitorLabelError as exc:
        await message.answer(f"Не получилось: {exc}", reply_markup=v2_back_keyboard())
        return

    if updated is None:
        await message.answer(_RENAME_NOT_FOUND, reply_markup=active_main_menu(True))
        return

    await _answer_rename_question_if_active(
        conversation_state_repository, workspace_context, competitor_id,
    )
    await message.answer(_RENAME_SAVED, reply_markup=active_main_menu(True))


async def _answer_rename_question_if_active(
    conversation_state_repository: ConversationStateRepository | None,
    workspace_context: WorkspaceContext,
    competitor_id: int,
) -> None:
    """Marks the F2A PendingQuestion pilot answered, if still active and
    matching this exact competitor. Best-effort - the FSM path above already
    completed the actual rename; this is bookkeeping only.

    Note (restart behaviour, per F2A spec section 3): FSM uses MemoryStorage,
    so a process restart between the button press and this handler wipes the
    FSM state - this function then simply never runs, and the persisted
    question is left to expire via its TTL rather than being answered. No
    catch-all resolver is added to handle that case in F2A - see the F2A
    report.
    """
    if conversation_state_repository is None:
        return
    try:
        question = await conversation_state_repository.get_active_question(
            workspace_context.workspace_id, workspace_context.telegram_user_id,
        )
        if (
            question is not None
            and question.question_type == _RENAME_QUESTION_TYPE
            and question.subject_ref_type == "competitor"
            and question.subject_ref_id == competitor_id
        ):
            await conversation_state_repository.answer_question(
                workspace_context.workspace_id, workspace_context.telegram_user_id,
                question.id,
            )
    except Exception:
        log.warning("competitors: conversation state bookkeeping failed for rename answer")


def _callback_id(data: str | None, prefix: str) -> int | None:
    raw = (data or "").removeprefix(prefix).split(":", 1)[0]
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


async def _competitor_for_callback(
    callback: CallbackQuery, prefix: str, repository: CompetitorRepository,
    workspace_context: WorkspaceContext | None,
) -> Competitor | None:
    competitor_id = _callback_id(callback.data, prefix)
    if workspace_context is None or competitor_id is None:
        return None
    return await repository.get_for_workspace(workspace_context.workspace_id, competitor_id)


@router.callback_query(MagicData(F.v2_menu_enabled), F.data.startswith(COMPETITOR_OPEN_PREFIX))
async def open_competitor(
    callback: CallbackQuery, competitor_repository: CompetitorRepository,
    workspace_context: WorkspaceContext | None,
) -> None:
    await callback.answer()
    competitor = await _competitor_for_callback(
        callback, COMPETITOR_OPEN_PREFIX, competitor_repository, workspace_context,
    )
    if competitor is None or callback.message is None:
        return
    snapshot = await competitor_repository.get_intelligence(
        competitor.workspace_id, competitor.id,
    )
    status = "анализ ещё не выполнялся" if snapshot is None else f"обновлено {snapshot.analyzed_at[:16]} UTC"
    await callback.message.answer(
        f"🎯 {competitor.label}\n{competitor.url}\n\nCompetitor Intelligence: {status}",
        reply_markup=competitor_card_keyboard(competitor.id), disable_web_page_preview=True,
    )


async def _refresh_competitor(
    callback: CallbackQuery, prefix: str, repository: CompetitorRepository,
    workspace_context: WorkspaceContext | None, provider: LLMProvider,
    knowledge_service: KnowledgeService,
    usage_ledger_repository: UsageLedgerRepository | None = None,
) -> None:
    competitor = await _competitor_for_callback(callback, prefix, repository, workspace_context)
    if competitor is None or callback.message is None or workspace_context is None:
        return
    await callback.message.answer("Собираю публичные источники и готовлю внутренний анализ…")
    try:
        intelligence = await CompetitorIntelligenceService(
            provider, knowledge_service, usage_ledger_repository=usage_ledger_repository,
        ).analyze(competitor)
    except CompetitorIntelligenceUnavailable as exc:
        await callback.message.answer(f"Не удалось обновить анализ: {exc}")
        return
    await repository.save_intelligence(workspace_context.workspace_id, intelligence)
    await callback.message.answer(
        _render_analysis(intelligence), reply_markup=competitor_card_keyboard(competitor.id),
        disable_web_page_preview=True,
    )


@router.callback_query(MagicData(F.v2_menu_enabled), F.data.startswith(COMPETITOR_ANALYZE_PREFIX))
async def analyze_competitor(callback: CallbackQuery, competitor_repository: CompetitorRepository,
    workspace_context: WorkspaceContext | None, llm_provider: LLMProvider,
    knowledge_service: KnowledgeService,
    usage_ledger_repository: UsageLedgerRepository | None = None) -> None:
    await callback.answer()
    await _refresh_competitor(callback, COMPETITOR_ANALYZE_PREFIX, competitor_repository,
        workspace_context, llm_provider, knowledge_service, usage_ledger_repository)


@router.callback_query(MagicData(F.v2_menu_enabled), F.data.startswith(COMPETITOR_REFRESH_PREFIX))
async def refresh_competitor(callback: CallbackQuery, competitor_repository: CompetitorRepository,
    workspace_context: WorkspaceContext | None, llm_provider: LLMProvider,
    knowledge_service: KnowledgeService,
    usage_ledger_repository: UsageLedgerRepository | None = None) -> None:
    await callback.answer()
    await _refresh_competitor(callback, COMPETITOR_REFRESH_PREFIX, competitor_repository,
        workspace_context, llm_provider, knowledge_service, usage_ledger_repository)


async def _snapshot(callback: CallbackQuery, prefix: str, repository: CompetitorRepository,
    workspace_context: WorkspaceContext | None):
    competitor = await _competitor_for_callback(callback, prefix, repository, workspace_context)
    if competitor is None or workspace_context is None:
        return None, None
    return competitor, await repository.get_intelligence(workspace_context.workspace_id, competitor.id)


@router.callback_query(MagicData(F.v2_menu_enabled), F.data.startswith(COMPETITOR_NEWS_PREFIX))
async def competitor_news(callback: CallbackQuery, competitor_repository: CompetitorRepository,
    workspace_context: WorkspaceContext | None) -> None:
    await callback.answer()
    competitor, data = await _snapshot(callback, COMPETITOR_NEWS_PREFIX, competitor_repository, workspace_context)
    if callback.message is None or competitor is None:
        return
    if data is None:
        await callback.message.answer(_INTELLIGENCE_MISSING); return
    lines = ["📰 Свежие сигналы", *[f"• {x}" for x in data.fresh_signals]]
    lines.extend(f"• {s.title}\n{s.final_url}\nобнаружено: {s.discovered_at[:10]}" for s in data.sources)
    await callback.message.answer("\n\n".join(lines), disable_web_page_preview=True)


@router.callback_query(MagicData(F.v2_menu_enabled), F.data.startswith(COMPETITOR_IDEAS_PREFIX))
async def competitor_ideas(callback: CallbackQuery, competitor_repository: CompetitorRepository,
    workspace_context: WorkspaceContext | None) -> None:
    await callback.answer()
    competitor, data = await _snapshot(callback, COMPETITOR_IDEAS_PREFIX, competitor_repository, workspace_context)
    if callback.message is None or competitor is None:
        return
    if data is None:
        await callback.message.answer(_INTELLIGENCE_MISSING); return
    await _show_opportunities(callback.message, data)


@router.callback_query(MagicData(F.v2_menu_enabled), F.data.startswith(COMPETITOR_CREATE_PREFIX))
async def create_from_competitor_opportunity(callback: CallbackQuery,
    competitor_repository: CompetitorRepository, workspace_context: WorkspaceContext | None,
    llm_provider: LLMProvider, partner_repository: PartnerRepository) -> None:
    await callback.answer()
    competitor, data = await _snapshot(callback, COMPETITOR_CREATE_PREFIX, competitor_repository, workspace_context)
    if callback.message is None or competitor is None or workspace_context is None:
        return
    if data is None:
        await callback.message.answer(_INTELLIGENCE_MISSING); return
    parts = (callback.data or "").removeprefix(COMPETITOR_CREATE_PREFIX).split(":", 1)
    if len(parts) == 1:
        await _show_opportunities(callback.message, data); return
    opportunity = next((x for x in data.opportunities if x.id == parts[1]), None)
    if opportunity is None:
        return
    profile = await partner_repository.get_business_profile(workspace_context.workspace_id)
    # Stage 3B1 parity: остальные Content Factory flow (material_generation.py,
    # tasks.py) всегда подмешивают личный стиль ТЕКУЩЕГО пользователя через
    # UserStyleService — без этого вызова build_competitor_signal_generation_spec
    # получает user_preferences=None и молча теряет avoid_phrases/example_posts,
    # даже если build_* сам умеет их принять.
    user_preferences = await UserStyleService(partner_repository).get(workspace_context)
    spec = MaterialOrchestrationService().build_competitor_signal_generation_spec(
        workspace_context.workspace_id, profile,
        competitor_signal=opportunity.topic, key_thesis=opportunity.key_thesis,
        own_post_angle=opportunity.own_post_angle, audience_value=opportunity.audience_value,
        source_title=opportunity.source_title, source_url=opportunity.source_url,
        travel_advantage_link=opportunity.travel_advantage_link,
        user_preferences=user_preferences,
    )
    request = build_provider_generation_request(spec, limit=6000)
    draft = await asyncio.to_thread(llm_provider.generate_draft,
        source_text=request.source_text, material_type=request.material_type,
        output_format=request.output_format, mode="ai")
    if draft is None:
        await callback.message.answer("Не удалось получить черновик автоматически."); return
    await callback.message.answer(
        "📝 Черновик по content opportunity — только для ручной проверки\n\n" + draft.text
    )


async def _show_opportunities(message: Message, data) -> None:
    lines = ["💡 Content opportunities"]
    for index, item in enumerate(data.opportunities, 1):
        lines.append(
            f"\n{index}. {item.topic}\nИсточник: {item.source_title}\n{item.source_url}\n"
            f"Тезис: {item.key_thesis}\nУгол: {item.own_post_angle}"
        )
    await message.answer("\n".join(lines)[:3900], reply_markup=competitor_opportunities_keyboard(
        data.competitor_id, tuple((x.id, x.topic) for x in data.opportunities),
    ), disable_web_page_preview=True)


def _render_analysis(data) -> str:
    def section(title, values):
        return "" if not values else "\n\n" + title + "\n" + "\n".join(f"• {x}" for x in values)
    text = f"🔎 Анализ: {data.competitor_label}"
    text += section("Позиционирование", data.positioning)
    text += section("Продукты и направления", (*data.products, *data.destinations_and_categories))
    text += section("Акции и механики", (*data.promotions, *data.loyalty_mechanics))
    text += section("Сервис и UX", data.service_and_ux)
    text += section("Сильные стороны", data.strengths)
    text += section("Travel Advantage — только verified KB", data.travel_advantage_comparison)
    text += "\n\nИсточники / provenance\n" + "\n".join(
        f"• {s.title} — {s.final_url} (обнаружено {s.discovered_at[:10]})" for s in data.sources
    )
    return text[:3900]


# --- Competitor Discovery Radar: turns already-synced public market signals
# into reviewable CompetitorCandidate rows (see app/services/
# competitor_discovery.py). Adding a candidate reuses the exact same
# competitor_repository.add_competitor() call as the manual "➕ Добавить
# конкурента" flow above, so the existing card (🔎 Анализ / 📰 Что нового /
# 💡 Идеи для постов / ✍️ Создать материал / 🔄 Обновить) works immediately.

_DISCOVERY_UNAVAILABLE = (
    "Discovery недоступен: источник рыночных сигналов не подключён."
)
_DISCOVERY_EMPTY = (
    "🌐 Competitor Discovery\n\n"
    "Новых рыночных сигналов пока не найдено. Оркестратор проверит "
    "источники ещё раз при следующем запуске."
)
_CANDIDATE_NOT_FOUND = "Кандидат не найден — возможно, он уже обработан."
_CANDIDATE_ADDED = "✅ «{label}» добавлен в конкуренты."
_CANDIDATE_IGNORED = "Скрыто. Больше не будет предложено."

_CLASSIFICATION_EMOJI = {
    CandidateClassification.DIRECT_COMPETITOR: "🔴",
    CandidateClassification.POTENTIAL_COMPETITOR: "🟠",
    CandidateClassification.MARKET_SIGNAL: "🔵",
}
_CLASSIFICATION_LABEL = {
    CandidateClassification.DIRECT_COMPETITOR: "Прямой конкурент",
    CandidateClassification.POTENTIAL_COMPETITOR: "Потенциальный конкурент",
    CandidateClassification.MARKET_SIGNAL: "Рыночный сигнал",
}


def _render_candidate_card(candidate) -> str:
    emoji = _CLASSIFICATION_EMOJI[candidate.classification]
    label = _CLASSIFICATION_LABEL[candidate.classification]
    return (
        f"{emoji} {candidate.name} — {label}\n\n"
        f"{candidate.description}\n\n"
        f"Почему важно:\n{candidate.why_it_matters}\n\n"
        f"Уверенность: {candidate.confidence.value}\n"
        f"Источник: {candidate.source_title} — {candidate.source_url}\n"
        f"Обнаружено: {candidate.discovered_at[:10]}"
    )[:3900]


def _render_candidate_detail(candidate) -> str:
    label = _CLASSIFICATION_LABEL[candidate.classification]
    lines = [
        f"{_CLASSIFICATION_EMOJI[candidate.classification]} {candidate.name}",
        "", candidate.description, "", "Признаки (evidence):",
        *[f"• {item}" for item in candidate.evidence],
        "", f"Почему классифицирован как «{label}»:", candidate.why_it_matters,
        "", f"Уверенность: {candidate.confidence.value}",
        f"Источник: {candidate.source_title}", candidate.source_url,
        f"Домен: {candidate.canonical_domain}",
        f"Обнаружено: {candidate.discovered_at[:10]}",
    ]
    return "\n".join(lines)[:3900]


async def _candidate_for_callback(
    callback: CallbackQuery, prefix: str, repository: CompetitorRepository,
    workspace_context: WorkspaceContext | None,
):
    candidate_id = _callback_id(callback.data, prefix)
    if workspace_context is None or candidate_id is None:
        return None
    candidate = await repository.get_candidate_for_workspace(
        workspace_context.workspace_id, candidate_id,
    )
    if candidate is None and callback.message is not None:
        await callback.message.answer(_CANDIDATE_NOT_FOUND)
    return candidate


@router.callback_query(MagicData(F.v2_menu_enabled), F.data == COMPETITOR_DISCOVERY_START)
async def discover_new_competitors(
    callback: CallbackQuery,
    competitor_repository: CompetitorRepository,
    workspace_context: WorkspaceContext | None,
    workspace_signal_repository: WorkspaceSignalRepository | None,
    llm_provider: LLMProvider,
    partner_repository: PartnerRepository,
) -> None:
    await callback.answer()
    if callback.message is None or workspace_context is None:
        return
    if workspace_signal_repository is None:
        await callback.message.answer(_DISCOVERY_UNAVAILABLE)
        return

    profile = await partner_repository.get_business_profile(workspace_context.workspace_id)
    own_domain = profile.context.public_contacts.get("website") if profile is not None else None

    service = CompetitorDiscoveryService(
        workspace_signal_repository, competitor_repository, llm_provider,
    )
    candidates = await service.discover(workspace_context.workspace_id, own_domain=own_domain)
    if not candidates:
        await callback.message.answer(_DISCOVERY_EMPTY)
        return

    await callback.message.answer(
        f"🌐 Competitor Discovery\n\nНайдено {len(candidates)} новых рыночных сигналов."
    )
    for candidate in candidates:
        offer_add = candidate.classification is not CandidateClassification.MARKET_SIGNAL
        await callback.message.answer(
            _render_candidate_card(candidate),
            reply_markup=competitor_candidate_keyboard(candidate.candidate_id, offer_add=offer_add),
            disable_web_page_preview=True,
        )


@router.callback_query(
    MagicData(F.v2_menu_enabled), F.data.startswith(COMPETITOR_DISCOVERY_VIEW_PREFIX)
)
async def view_competitor_candidate(
    callback: CallbackQuery,
    competitor_repository: CompetitorRepository,
    workspace_context: WorkspaceContext | None,
) -> None:
    await callback.answer()
    candidate = await _candidate_for_callback(
        callback, COMPETITOR_DISCOVERY_VIEW_PREFIX, competitor_repository, workspace_context,
    )
    if callback.message is None or candidate is None:
        return
    await callback.message.answer(_render_candidate_detail(candidate), disable_web_page_preview=True)


@router.callback_query(
    MagicData(F.v2_menu_enabled), F.data.startswith(COMPETITOR_DISCOVERY_ADD_PREFIX)
)
async def add_competitor_candidate(
    callback: CallbackQuery,
    competitor_repository: CompetitorRepository,
    workspace_context: WorkspaceContext | None,
) -> None:
    await callback.answer()
    candidate = await _candidate_for_callback(
        callback, COMPETITOR_DISCOVERY_ADD_PREFIX, competitor_repository, workspace_context,
    )
    if callback.message is None or candidate is None or workspace_context is None:
        return
    competitor = await competitor_repository.add_competitor(
        workspace_context.workspace_id, candidate.discovered_url, label=candidate.name,
    )
    await competitor_repository.update_candidate_status(
        workspace_context.workspace_id, candidate.candidate_id, CandidateStatus.ADDED,
    )
    await callback.message.answer(
        _CANDIDATE_ADDED.format(label=competitor.label),
        reply_markup=competitor_card_keyboard(competitor.id),
    )


@router.callback_query(
    MagicData(F.v2_menu_enabled), F.data.startswith(COMPETITOR_DISCOVERY_IGNORE_PREFIX)
)
async def ignore_competitor_candidate(
    callback: CallbackQuery,
    competitor_repository: CompetitorRepository,
    workspace_context: WorkspaceContext | None,
) -> None:
    await callback.answer()
    candidate = await _candidate_for_callback(
        callback, COMPETITOR_DISCOVERY_IGNORE_PREFIX, competitor_repository, workspace_context,
    )
    if callback.message is None or candidate is None or workspace_context is None:
        return
    await competitor_repository.update_candidate_status(
        workspace_context.workspace_id, candidate.candidate_id, CandidateStatus.IGNORED,
    )
    await callback.message.answer(_CANDIDATE_IGNORED)
