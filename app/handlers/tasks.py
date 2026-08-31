from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import MagicData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.cards import build_card
from app.domain.orchestration import OutputFormat
from app.domain.partners import WorkspaceContext
from app.domain.work import WorkSubjectValidationError, work_item_revision
from app.handlers.menu import AwaitReplySubject, AwaitTask, BUTTON_HINTS
from app.handlers.source_analysis import run_source_analysis
from app.keyboards import (
    BTN_V2_CLIENT_REPLY,
    TASK_CONFIRM_PUBLICATION_ANALYSIS,
    active_main_menu,
    reply_confirm_keyboard,
    uncertain_route_publication_keyboard,
)
from app.orchestration.context import record_turn
from app.orchestration.provider import OrchestrationLLMProvider
from app.orchestration.shadow import run_shadow_orchestration
from app.planner.context import PlannerExecutionContext
from app.planner.cost import DEFAULT_MAX_LLM_CALLS_PER_PLANNER_RUN
from app.planner.eligibility import is_planner_allowed_for_user, is_planner_eligible
from app.planner.provider import PlannerLLMProvider
from app.planner.service import run_planner_for_task
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.competitor_repository import CompetitorRepository
from app.repositories.conversation_state_repository import ConversationStateRepository
from app.repositories.partner_repository import PartnerRepository
from app.repositories.source_analysis_repository import SourceAnalysisRepository
from app.repositories.work_repository import WorkRepository
from app.repositories.workspace_signal_repository import WorkspaceSignalRepository
from app.routing.modules import Module
from app.routing.router import RouteDecision, route_for_button, route_text
from app.routing.safety import SafetyLevel
from app.services.action_contract_adapter import build_action_contract
from app.services.assistant_tail_cleanup import strip_assistant_tail
from app.services.conversation_state_service import ConversationStateService
from app.services.daily_actions import DailyActionsService
from app.services.generation_request_builder import build_provider_generation_request
from app.services.lead_radar import LeadRadarConfig
from app.services.knowledge_service import KnowledgeBundle
from app.services.llm.base import LLMProvider
from app.services.material_orchestration import MaterialOrchestrationService
from app.services.reference_resolver import ReferenceResolver, ResolvedActionContext
from app.services.reply_sync import ReplyBridgeContext, ReplyWorkSyncService
from app.services.user_style import UserStyleService
from app.storage import Journal
from app.telegram_chunks import chunk_text

router = Router(name="tasks")
log = logging.getLogger(__name__)

_DRAFT_MODE = "ai"

_DRAFT_FAILURE_MESSAGE = (
    "Не удалось получить черновик автоматически. "
    "Можно открыть Travel Content Factory вручную."
)

_DRAFT_SEND_FAILURE_MESSAGE = (
    "Черновик сгенерирован, но не удалось отправить его в Telegram. "
    "Попробуйте ещё раз или откройте Travel Content Factory вручную."
)

_LONG_TASK_ACK_MESSAGE = (
    "⏳ Готовлю материал. Для большого объёма ответ может занять немного "
    "больше времени."
)

# Stage 3 Planner MVP (see app.planner.service): sent at most once, only
# after a plan has actually been accepted (see _try_planner_flow's
# on_plan_accepted callback) - never a spurious ack for a request that will
# fall back to the old router anyway.
_PLANNER_ACK_MESSAGE = (
    "⏳ Собираю и анализирую информацию — это может занять больше времени, "
    "чем обычно."
)

_TEXT_CHECK_FAILURE_MESSAGE = (
    "Не удалось проверить текст автоматически. "
    "Попробуйте ещё раз или откройте Travel Content Factory вручную."
)

_UNCERTAIN_ROUTE_MESSAGE = (
    "⚠️ Не удалось уверенно определить маршрут для этой задачи. "
    "Выберите категорию задачи кнопкой главного меню или переформулируйте запрос."
)

_KNOWLEDGE_UNAVAILABLE_MESSAGE = (
    "Не удалось безопасно проверить информацию в базе знаний. "
    "Пожалуйста, попробуйте позже или уточните данные по официальному источнику."
)

_CURRENT_SOURCE_REQUIRED_MESSAGE = (
    "Для ответа нужна проверка текущего официального источника. "
    "Статическая база знаний не подтверждает актуальную доступность или условия."
)

_KNOWLEDGE_ELIGIBLE_MODULES = frozenset({
    Module.TRAVEL_ASSISTANT,
    Module.SAFETY_LAYER,
    Module.PARTNER_PACKAGING,
})

# Fix: длинный текст, вставленный из главного меню (например, целая
# публикация или пост), обычно не содержит ни одного из routing keywords —
# route_text() честно возвращает is_uncertain, и раньше пользователь
# получал тупиковое предупреждение вместо реального разбора. Структурный
# признак («это скорее вставленный текст публикации, а не короткая
# нераспознанная команда») — длина, а не список ключевых слов: короткие
# неразобранные фразы («что-то непонятное») по-прежнему получают обычное
# предупреждение и не перехватываются.
#
# Review fix: длина одна НЕ отличает публикацию от длинного техзадания,
# письма или вопроса в поддержку — все они одинаково не содержат routing
# keywords и одинаково длинные (проверено на реальных примерах). Поэтому
# больше НЕ вызываем run_source_analysis автоматически: только предлагаем
# кнопкой, а исходный текст ждёт подтверждения в FSM state. Если фактически
# это было письмо/вопрос — пользователь просто не нажимает кнопку и жмёт
# «Выбрать другую задачу», вместо того чтобы бот тихо создал в БД ложный
# Source и потратил вызов analyze_source впустую.
_PUBLICATION_LOOKALIKE_MIN_CHARS = 400

_PENDING_SOURCE_TEXT_KEY = "pending_source_analysis_text"
# Chat/message id of the "Разобрать его как публикацию?" prompt itself, so a
# later unrelated flow can strip its inline keyboard - see
# invalidate_pending_publication_offer() below.
_PENDING_SOURCE_OFFER_MESSAGE_KEY = "pending_source_analysis_offer_message"

_PUBLICATION_CONFIRM_PROMPT = (
    "Похоже, вы прислали большой фрагмент текста. Разобрать его как публикацию?"
)

_PENDING_TEXT_LOST_MESSAGE = (
    "Не удалось найти сохранённый текст. Пришлите его ещё раз."
)


async def invalidate_pending_publication_offer(state: FSMContext, bot) -> None:
    """Strips the inline keyboard of an outstanding "Разобрать его как
    публикацию?" offer, if any, before an unrelated flow takes over the FSM
    state that offer's confirm button depends on.

    Prod bug: a user could tap the "🛡 Проверить и улучшить текст" reply
    button while that offer's inline keyboard was still visible from an
    earlier message. start_free_text_review's state.clear() wiped
    _PENDING_SOURCE_TEXT_KEY, so tapping the now-stale confirm button
    afterwards produced "Не удалось найти сохранённый текст. Пришлите его
    ещё раз." layered right on top of the Safety Layer flow's own "Пришли
    текст..." prompt - two unrelated flows visibly mixed in one transcript.
    Best-effort and silent on failure (message too old/already edited/gone):
    the existing fail-closed message in on_confirm_publication_analysis
    still covers whatever this race doesn't catch.
    """
    data = await state.get_data()
    ref = data.get(_PENDING_SOURCE_OFFER_MESSAGE_KEY)
    if not ref:
        return
    chat_id, message_id = ref
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=None,
        )
    except TelegramAPIError:
        pass


def _looks_like_pasted_publication(text: str) -> bool:
    return len(text.strip()) >= _PUBLICATION_LOOKALIKE_MIN_CHARS


_REPLY_SUBJECT_EMPTY = (
    "Сообщение не должно быть пустым. Пришлите вопрос клиента или короткое "
    "имя/обозначение, например: Иван."
)
_REPLY_WORKSPACE_UNAVAILABLE = "Рабочее пространство недоступно."

# UX polish: короткая метка/имя клиента vs уже присланное сообщение клиента —
# детерминированная эвристика без LLM-классификатора. Метки вида «Иван»,
# «Клиент по Турции», «Клиент по отелю в Питере», «Семья Ивановых Турция
# июнь» — до 5 слов, без «?», короче лимита символов. Порог в 4 слова
# ошибочно ловил «Клиент по отелю в Питере» (5 слов) как сообщение — поднят
# до 5, все реальные вопросы клиента (см. тесты) остаются далеко за порогом
# и по словам, и по символам. Всё остальное (вопрос, знак «?», длинное/
# многословное сообщение) сразу считаем сообщением клиента, чтобы не
# заставлять вводить его дважды.
_SUBJECT_NAME_MAX_CHARS = 30
_SUBJECT_NAME_MAX_WORDS = 5


def _looks_like_client_message(text: str) -> bool:
    if "?" in text:
        return True
    if len(text) > _SUBJECT_NAME_MAX_CHARS:
        return True
    if len(text.split()) > _SUBJECT_NAME_MAX_WORDS:
        return True
    return False


@router.message(
    MagicData(F.v2_menu_enabled), AwaitReplySubject.waiting,
    F.text & ~F.text.startswith("/"),
)
async def on_reply_subject_received(
    message: Message,
    state: FSMContext,
    journal: Journal,
    llm_provider: LLMProvider,
    work_repository: WorkRepository,
    workspace_context: WorkspaceContext | None,
    partner_repository: PartnerRepository,
    artifact_repository: ArtifactRepository | None = None,
    conversation_state_repository: ConversationStateRepository | None = None,
    reference_resolver: ReferenceResolver | None = None,
) -> None:
    """Первый шаг «Ответить клиенту» (v2): «Кому отвечаем?» -> WorkSubject.

    Имя/метка необязательны: если текст похож на уже присланное сообщение
    клиента (см. _looks_like_client_message), обрабатываем его сразу тем же
    путём, что и обычный AwaitTask.waiting (_route_and_dispatch), без
    отдельного WorkSubject — ReplyWorkSyncService корректно работает и без
    subject_id (см. app/services/reply_sync.py). Иначе — прежнее поведение:
    получаем/создаём WorkSubject и передаём управление на AwaitTask.waiting.
    """
    text = (message.text or "").strip()
    if not text:
        await message.answer(_REPLY_SUBJECT_EMPTY, reply_markup=active_main_menu(True))
        return
    if workspace_context is None:
        await message.answer(_REPLY_WORKSPACE_UNAVAILABLE, reply_markup=active_main_menu(True))
        await state.clear()
        return

    if _looks_like_client_message(text):
        await state.clear()
        await _route_and_dispatch(
            message, state, journal, llm_provider, workspace_context, partner_repository,
            text, forced_module=Module.TRAVEL_ASSISTANT, skip_route_card=True,
            reply_subject_data=(None, None, None),
            work_repository=work_repository, artifact_repository=artifact_repository,
            conversation_state_repository=conversation_state_repository,
            reference_resolver=reference_resolver,
        )
        return

    try:
        subject = await work_repository.get_or_create_subject(
            workspace_context.workspace_id, text,
        )
    except WorkSubjectValidationError:
        await message.answer(_REPLY_SUBJECT_EMPTY, reply_markup=active_main_menu(True))
        return

    await state.update_data(
        daily_action_subject_id=subject.id,
        daily_action_subject_name=subject.name,
    )
    await state.set_state(AwaitTask.waiting)
    await message.answer(
        BUTTON_HINTS[BTN_V2_CLIENT_REPLY], reply_markup=active_main_menu(True),
    )


@router.message(AwaitTask.waiting, F.text & ~F.text.startswith("/"))
async def on_task_after_button(
    message: Message,
    state: FSMContext,
    journal: Journal,
    llm_provider: LLMProvider,
    workspace_context: WorkspaceContext | None,
    partner_repository: PartnerRepository,
    work_repository: WorkRepository | None = None,
    artifact_repository: ArtifactRepository | None = None,
    conversation_state_repository: ConversationStateRepository | None = None,
    v2_menu_enabled: bool = False,
    reference_resolver: ReferenceResolver | None = None,
) -> None:
    data = await state.get_data()
    forced_raw = data.get("forced_module")
    skip_route_card = bool(data.get("skip_route_card"))
    # Заполняются либо on_reply_subject_received (новый subject), либо
    # daily_actions.on_daily_action_prompt (уже существующий work_item) —
    # для любого другого входа (обычная кнопка/свободный текст) их просто
    # нет в data, и reply_context ниже останется None.
    reply_work_item_id = data.get("daily_action_work_item_id")
    reply_subject_id = data.get("daily_action_subject_id")
    reply_subject_name = data.get("daily_action_subject_name")
    task_text = (message.text or "").strip()
    await state.clear()

    if not task_text:
        await message.answer(
            "Пустой запрос. Опишите задачу.",
            reply_markup=active_main_menu(v2_menu_enabled),
        )
        return
    if workspace_context is None:
        await message.answer(
            "Рабочее пространство недоступно.",
            reply_markup=active_main_menu(v2_menu_enabled),
        )
        return

    forced_module = Module(forced_raw) if forced_raw else None
    await _route_and_dispatch(
        message, state, journal, llm_provider, workspace_context, partner_repository,
        task_text, forced_module=forced_module, skip_route_card=skip_route_card,
        reply_subject_data=(reply_work_item_id, reply_subject_id, reply_subject_name),
        work_repository=work_repository, artifact_repository=artifact_repository,
        conversation_state_repository=conversation_state_repository,
        v2_menu_enabled=v2_menu_enabled,
        reference_resolver=reference_resolver,
    )


@router.message(F.text & ~F.text.startswith("/"))
async def on_free_text(
    message: Message,
    journal: Journal,
    llm_provider: LLMProvider,
    workspace_context: WorkspaceContext | None,
    partner_repository: PartnerRepository,
    v2_menu_enabled: bool = False,
    state: FSMContext | None = None,
    orchestration_llm_provider: OrchestrationLLMProvider | None = None,
    planner_llm_provider: PlannerLLMProvider | None = None,
    planner_enabled: bool = False,
    planner_allowed_telegram_user_ids: frozenset[int] = frozenset(),
    planner_max_llm_calls: int = DEFAULT_MAX_LLM_CALLS_PER_PLANNER_RUN,
    competitor_repository: CompetitorRepository | None = None,
    work_repository: WorkRepository | None = None,
    artifact_repository: ArtifactRepository | None = None,
    conversation_state_repository: ConversationStateRepository | None = None,
    workspace_signal_repository: WorkspaceSignalRepository | None = None,
    lead_radar_config: LeadRadarConfig | None = None,
    reference_resolver: ReferenceResolver | None = None,
) -> None:
    task_text = (message.text or "").strip()
    if not task_text:
        await message.answer(
            "Пустой запрос. Опишите задачу.",
            reply_markup=active_main_menu(v2_menu_enabled),
        )
        return
    if workspace_context is None:
        await message.answer(
            "Рабочее пространство недоступно.",
            reply_markup=active_main_menu(v2_menu_enabled),
        )
        return

    # Stage 3 Planner MVP - gated BEFORE route_text(), but route_text() and
    # everything below is completely untouched: this only ever short-
    # circuits the function early (via `return`) when the Planner path
    # fully handled the message. Every check here is cheap/local (bool,
    # allowlist membership, a regex-based eligibility check) - none of them
    # is itself an LLM call, so an ineligible/disallowed/disabled request
    # costs nothing extra (see app.planner.cost / the Stage 3 cost addendum).
    telegram_user_id = getattr(getattr(message, "from_user", None), "id", None)
    if (
        planner_enabled
        and is_planner_allowed_for_user(telegram_user_id, planner_allowed_telegram_user_ids)
        and planner_llm_provider is not None
        and planner_llm_provider.is_configured
        and is_planner_eligible(task_text)
    ):
        log.info(
            "planner_flow: eligible workspace_id=%s user_id=%s",
            workspace_context.workspace_id, telegram_user_id,
        )
        handled = await _try_planner_flow(
            message, task_text, workspace_context, partner_repository,
            llm_provider, planner_llm_provider, competitor_repository,
            work_repository, artifact_repository, workspace_signal_repository,
            lead_radar_config, state, planner_max_llm_calls,
        )
        if handled:
            return
        # Controlled fallback: fall through to the existing router flow
        # below exactly as if Planner had never been attempted.

    decision = route_text(task_text)
    await journal.add(
        workspace_context.workspace_id,
        task_text=task_text,
        primary_module=decision.primary_module.value,
        secondary_modules=tuple(m.value for m in decision.secondary_modules),
        safety_level=decision.safety_level.value,
    )
    await record_turn(state, role="user", text=task_text)
    # UX polish: в v2 прямой свободный текст — самый частый вход в create
    # material/reply flow, и техническая «📌 Карточка маршрута» тут не нужна
    # (см. тот же принцип у skip_route_card в on_task_after_button/v2-кнопках)
    # — пользователь сразу должен увидеть результат/следующий шаг. В v1 карточка
    # остаётся как раньше.
    if not v2_menu_enabled:
        await message.answer(
            build_card(decision), reply_markup=active_main_menu(v2_menu_enabled)
        )
    knowledge_controlled = await _maybe_send_module_result(
        message, decision, llm_provider,
        workspace_context, partner_repository,
        # F2B: on_free_text (plain typed text, no button) previously never
        # forwarded these into _maybe_send_draft at all - the regular-post
        # Artifact-creation gap the F2B report opens with is exactly this
        # path ("User: Напиши пост... Bot: [пост]" is on_free_text, not the
        # button-driven on_task_after_button flow). reply_context stays
        # None here as before - on_free_text has no reply-subject step, so
        # the client-reply branch in _maybe_send_draft remains inert.
        work_repository=work_repository, artifact_repository=artifact_repository,
        conversation_state_repository=conversation_state_repository,
        state=state, v2_menu_enabled=v2_menu_enabled,
        reference_resolver=reference_resolver,
    )
    await record_turn(
        state, role="assistant",
        text=f"[{decision.primary_module.value}] ответ отправлен",
        module=decision.primary_module.value,
    )
    if knowledge_controlled:
        return
    # LLM orchestration shadow mode (Phase 1): runs strictly AFTER the reply
    # above, never before and never blocking it - see
    # app.orchestration.shadow for the fail-closed contract. Defaults to the
    # inert NullOrchestrationLLMProvider (is_configured=False) when not
    # wired, so existing callers/tests are entirely unaffected.
    if orchestration_llm_provider is not None:
        await _run_orchestration_shadow(
            task_text, decision, workspace_context, partner_repository,
            state, orchestration_llm_provider,
        )


async def _run_orchestration_shadow(
    task_text: str,
    decision: RouteDecision,
    workspace_context: WorkspaceContext,
    partner_repository: PartnerRepository,
    state: FSMContext | None,
    orchestration_llm_provider: OrchestrationLLMProvider,
) -> None:
    """Defense-in-depth wrapper: run_shadow_orchestration already never
    raises, but shadow mode affecting the user is exactly the one outcome
    that must be structurally impossible, not just "usually fine"."""
    if not orchestration_llm_provider.is_configured:
        return
    try:
        profile = await partner_repository.get_business_profile(
            workspace_context.workspace_id
        )
        await run_shadow_orchestration(
            task_text=task_text,
            old_decision=decision,
            workspace_id=workspace_context.workspace_id,
            provider=orchestration_llm_provider,
            state=state,
            business_profile=profile,
        )
    except Exception:
        log.debug("orchestration_shadow: wrapper caught unexpected failure", exc_info=True)


async def _try_planner_flow(
    message: Message,
    task_text: str,
    workspace_context: WorkspaceContext,
    partner_repository: PartnerRepository,
    llm_provider: LLMProvider,
    planner_llm_provider: PlannerLLMProvider,
    competitor_repository: CompetitorRepository | None,
    work_repository: WorkRepository | None,
    artifact_repository: ArtifactRepository | None,
    workspace_signal_repository: WorkspaceSignalRepository | None,
    lead_radar_config: LeadRadarConfig | None,
    state: FSMContext | None,
    max_llm_calls: int,
) -> bool:
    """Defense-in-depth wrapper around app.planner.service.run_planner_for_task
    (same reasoning as _run_orchestration_shadow above): that function
    already never raises, but a Planner-caused crash reaching a real user
    message is exactly the one outcome that must be structurally impossible,
    not just "usually fine".

    Returns True if the Planner path fully handled the message (a reply was
    sent) - False means the caller MUST fall back to the existing router,
    continuing exactly as if this had never been attempted. Never raises.
    """
    try:
        profile = await partner_repository.get_business_profile(
            workspace_context.workspace_id
        )
        daily_actions_service = None
        if work_repository is not None and artifact_repository is not None:
            # Same assembly app.handlers.daily_actions uses for "Что делать
            # сегодня" - reused as-is, not reimplemented.
            daily_actions_service = DailyActionsService(
                work_repository, partner_repository, artifact_repository,
                workspace_signal_repository,
            )
        execution_context = PlannerExecutionContext(
            workspace_id=workspace_context.workspace_id,
            llm_provider=llm_provider,
            competitor_repository=competitor_repository,
            partner_repository=partner_repository,
            lead_radar_config=lead_radar_config,
            daily_actions_service=daily_actions_service,
        )

        acked = False

        async def _on_plan_accepted(_plan) -> None:
            # Sent at most once, and only once a real plan is about to
            # execute - never a spurious ack for a request that fails
            # before that point and falls back to the old router anyway.
            nonlocal acked
            if not acked:
                acked = True
                await message.answer(_PLANNER_ACK_MESSAGE)

        outcome = await run_planner_for_task(
            task_text,
            provider=planner_llm_provider,
            execution_context=execution_context,
            business_profile=profile,
            advisory_route_decision=route_text(task_text),
            on_plan_accepted=_on_plan_accepted,
            max_llm_calls=max_llm_calls,
        )
    except Exception:
        log.warning("planner_flow: wrapper caught unexpected failure", exc_info=True)
        return False

    if not outcome.success or not outcome.reply_text:
        log.info(
            "planner_flow: falling back to router workspace_id=%s reason=%s",
            workspace_context.workspace_id, outcome.fallback_reason,
        )
        return False

    await _send_chunked(message, outcome.reply_text)
    await record_turn(state, role="user", text=task_text)
    await record_turn(
        state, role="assistant", text="[Planner] ответ отправлен", module="Planner",
    )
    return True


@router.callback_query(MagicData(F.v2_menu_enabled), F.data == TASK_CONFIRM_PUBLICATION_ANALYSIS)
async def on_confirm_publication_analysis(
    callback: CallbackQuery,
    state: FSMContext,
    workspace_context: WorkspaceContext | None,
    artifact_repository: ArtifactRepository | None,
    source_analysis_repository: SourceAnalysisRepository | None,
    llm_provider: LLMProvider,
) -> None:
    """Подтверждение из uncertain-route fallback: реальный разбор источника
    запускается только здесь, по явному нажатию, а не автоматически по
    длине текста (см. _looks_like_pasted_publication выше и review-отчёт).
    """
    data = await state.get_data()
    text = data.get(_PENDING_SOURCE_TEXT_KEY)
    await callback.answer()
    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)
    if (
        not text
        or workspace_context is None
        or artifact_repository is None
        or source_analysis_repository is None
    ):
        await state.clear()
        if callback.message is not None:
            await callback.message.answer(
                _PENDING_TEXT_LOST_MESSAGE, reply_markup=active_main_menu(True),
            )
        return
    if callback.message is None:
        return
    await run_source_analysis(
        callback.message, state, workspace_context, artifact_repository,
        source_analysis_repository, llm_provider, text,
    )


async def _route_and_dispatch(
    message: Message,
    state: FSMContext,
    journal: Journal,
    llm_provider: LLMProvider,
    workspace_context: WorkspaceContext,
    partner_repository: PartnerRepository,
    task_text: str,
    *,
    forced_module: Module | None,
    skip_route_card: bool,
    reply_subject_data: tuple[int | None, int | None, str | None],
    work_repository: WorkRepository | None = None,
    artifact_repository: ArtifactRepository | None = None,
    conversation_state_repository: ConversationStateRepository | None = None,
    v2_menu_enabled: bool = False,
    reference_resolver: ReferenceResolver | None = None,
) -> None:
    """Общий хвост on_task_after_button и «сообщение вместо имени» в
    on_reply_subject_received: маршрутизация, Journal, показ/скип карточки
    маршрута и диспатч в _maybe_send_module_result. Вынесено, чтобы оба входа
    вели себя идентично и не дублировали routing/journal/reply_context код.
    """
    decision = (
        route_for_button(forced_module, task_text)
        if forced_module is not None
        else route_text(task_text)
    )

    await journal.add(
        workspace_context.workspace_id,
        task_text=task_text,
        primary_module=decision.primary_module.value,
        secondary_modules=tuple(m.value for m in decision.secondary_modules),
        safety_level=decision.safety_level.value,
    )

    reply_context = (
        ReplyBridgeContext(*reply_subject_data)
        if decision.primary_module is Module.TRAVEL_ASSISTANT
        else None
    )

    if not skip_route_card:
        await message.answer(
            build_card(decision), reply_markup=active_main_menu(v2_menu_enabled)
        )
    await _maybe_send_module_result(
        message, decision, llm_provider,
        workspace_context, partner_repository,
        work_repository=work_repository, artifact_repository=artifact_repository,
        conversation_state_repository=conversation_state_repository,
        reply_context=reply_context, state=state, v2_menu_enabled=v2_menu_enabled,
        reference_resolver=reference_resolver,
    )


async def _maybe_send_module_result(
    message: Message,
    decision: RouteDecision,
    provider: LLMProvider,
    workspace_context: WorkspaceContext,
    partner_repository: PartnerRepository,
    *,
    work_repository: WorkRepository | None = None,
    artifact_repository: ArtifactRepository | None = None,
    conversation_state_repository: ConversationStateRepository | None = None,
    reply_context: ReplyBridgeContext | None = None,
    state: FSMContext | None = None,
    v2_menu_enabled: bool = False,
    reference_resolver: ReferenceResolver | None = None,
) -> bool:
    # Slice 1: one turn-local retrieval after the existing route decision and
    # before any generation.  Known non-KB modules bypass even the resolver;
    # Planner is handled earlier in on_free_text and never reaches this point
    # when it accepts the request.
    knowledge_bundle: KnowledgeBundle | None = None
    if (
        reference_resolver is not None
        and decision.primary_module in _KNOWLEDGE_ELIGIBLE_MODULES
    ):
        action_contract = build_action_contract(decision)
        try:
            resolved = await reference_resolver.resolve(
                question=decision.task_text,
                action_contract=action_contract,
                primary_module=decision.primary_module,
            )
        except Exception:
            log.warning("reference_resolver: unexpected failure", exc_info=True)
            await message.answer(_KNOWLEDGE_UNAVAILABLE_MESSAGE)
            return True
        if resolved.need_knowledge and resolved.knowledge_bundle is None:
            await message.answer(_KNOWLEDGE_UNAVAILABLE_MESSAGE)
            return True
        if resolved.needs_clarification:
            await message.answer(_knowledge_clarification_message(resolved))
            return True
        if resolved.requires_current_source:
            await message.answer(_CURRENT_SOURCE_REQUIRED_MESSAGE)
            return True
        if resolved.need_knowledge:
            knowledge_bundle = resolved.knowledge_bundle

    if decision.primary_module is Module.SAFETY_LAYER:
        await _send_text_check(message, decision, provider)
        return False

    if decision.primary_module is Module.PARTNER_PACKAGING:
        await _send_partner_package(
            message, decision, workspace_context.workspace_id, partner_repository,
        )
        return False

    # Prod bug: в v2 UI карточка маршрута («📌 Карточка маршрута», содержащая
    # предупреждение "Маршрут не определён уверенно") не показывается
    # (skip_route_card/v2_menu_enabled) — она была единственным местом, где
    # это предупреждение доходило до пользователя. Ниже по стеку ни один из
    # веток (_maybe_send_draft) не обрабатывает Module.ORCHESTRATOR, поэтому
    # запрос молча оставался без единого ответа бота. Это не проблема
    # конкретной формулировки: любой запрос, который router не смог уверенно
    # классифицировать, должен получить явный ответ, а не тишину.
    if decision.is_uncertain:
        # Review fix: раньше здесь автоматически запускался разбор источника
        # для любого длинного текста — но длина одна не отличает публикацию
        # от техзадания/письма/вопроса в поддержку (см. review отчёт).
        # Теперь — только предложение с подтверждением: исходный текст ждёт
        # в FSM state, реальный разбор происходит только по нажатию кнопки
        # (см. on_confirm_publication_analysis ниже). Ограничено v2 UI: в v1
        # этого сценария нет.
        if (
            v2_menu_enabled
            and state is not None
            and _looks_like_pasted_publication(decision.task_text)
        ):
            await state.update_data(**{_PENDING_SOURCE_TEXT_KEY: decision.task_text})
            offer_message = await message.answer(
                _PUBLICATION_CONFIRM_PROMPT,
                reply_markup=uncertain_route_publication_keyboard(),
            )
            if offer_message is not None:
                await state.update_data(**{
                    _PENDING_SOURCE_OFFER_MESSAGE_KEY: (
                        offer_message.chat.id, offer_message.message_id,
                    ),
                })
            return False
        await message.answer(_UNCERTAIN_ROUTE_MESSAGE)
        return False

    await _maybe_send_draft(
        message, decision, provider, workspace_context, partner_repository,
        work_repository=work_repository, artifact_repository=artifact_repository,
        conversation_state_repository=conversation_state_repository,
        reply_context=reply_context,
        knowledge_bundle=knowledge_bundle,
    )
    return False


def _knowledge_clarification_message(resolved: ResolvedActionContext) -> str:
    """Render turn-local stable options without persisting an offer."""
    titles_by_key = {
        item.stable_key: item.title
        for item in resolved.knowledge_bundle.primary_items
    } if resolved.knowledge_bundle is not None else {}
    options = [
        titles_by_key.get(stable_key, stable_key)
        for stable_key in resolved.clarification_options
    ]
    if not options:
        return (
            "Не удалось однозначно определить, какая информация вам нужна. "
            "Пожалуйста, уточните вопрос."
        )
    rendered = "\n".join(f"• {option}" for option in options)
    return f"Уточните, пожалуйста, какой вариант вы имеете в виду:\n{rendered}"



async def _send_partner_package(
    message: Message,
    decision: RouteDecision,
    workspace_id: int,
    partner_repository: PartnerRepository,
) -> None:
    """Формирует лёгкий MVP-комплект материалов без AI и внешних вызовов.

    Tenant-aware: TA-формулировки допустимы только когда Business Profile
    workspace явно помечен ta_affiliated=True. Во всех остальных случаях
    (обычный сторонний workspace или отсутствующий профиль — fail-closed)
    комплект универсален и не упоминает Travel Advantage.
    """
    task = decision.task_text.strip()
    short_task = task if len(task) <= 900 else f"{task[:897]}..."
    task_lower = task.lower()

    profile = await partner_repository.get_business_profile(workspace_id)
    ta_affiliated = profile is not None and profile.ta_affiliated

    lines: list[str] = [
        "📦 Черновик комплекта материалов для партнёра",
        "",
        "Основа запроса:",
        short_task,
        "",
    ]

    if ta_affiliated:
        lines.extend(
            [
                "Рекомендуемый состав комплекта:",
                "",
                "1. Короткое объяснение Travel Advantage",
                "— что это за формат и для каких задач его можно рассматривать;",
                "— без обещаний гарантированной выгоды, скидок или дохода.",
                "",
                "2. FAQ для новых партнёров",
                "— как спокойно объяснять общий принцип;",
                "— какие вопросы нужно уточнять вручную;",
                "— что не стоит обещать клиентам.",
                "",
                "3. Инструкция по безопасным ответам",
                "— не подтверждать цены, тарифы, оплату и доступность без проверки;",
                "— не обещать окупаемость, доход или результат;",
                "— не представлять партнёрский формат как трудоустройство.",
                "",
                "4. Ручной следующий шаг",
                "— утвердить состав;",
                "— подготовить материалы по одному;",
                "— проверить все конкретные факты перед передачей партнёру.",
            ]
        )
    else:
        business_name = profile.business_name.strip() if profile is not None else ""
        header = (
            f"Рекомендуемый состав комплекта для «{business_name}»:"
            if business_name
            else "Рекомендуемый состав комплекта:"
        )
        lines.extend(
            [
                header,
                "",
                "1. Короткое представление вашего бизнеса",
                "— чем вы занимаетесь и что важно рассказать партнёру;",
                "— без обещаний гарантированной выгоды, скидок или дохода.",
                "",
                "2. FAQ для новых партнёров",
                "— как спокойно объяснять общий принцип сотрудничества;",
                "— какие вопросы нужно уточнять вручную;",
                "— что не стоит обещать клиентам.",
                "",
                "3. Инструкция по безопасным ответам",
                "— не подтверждать цены, тарифы, оплату и доступность без проверки;",
                "— не обещать условия или результат, которые не подтверждены;",
                "— ясно описывать формат сотрудничества и роли сторон.",
                "",
                "4. Ручной следующий шаг",
                "— утвердить состав;",
                "— подготовить материалы по одному;",
                "— проверить все конкретные факты перед передачей партнёру.",
            ]
        )

    variable_terms = (
        "оплат",
        "брониров",
        "крипт",
        "тариф",
        "цен",
        "скидк",
        "доступност",
    )

    if any(term in task_lower for term in variable_terms):
        lines.extend(
            [
                "",
                "⚠️ Обязательный FAQ по переменным условиям:",
                "— способы оплаты зависят от конкретного варианта и требуют проверки;",
                "— доступность бронирования меняется по датам и маршруту;",
                "— цены, тарифы, скидки и условия нельзя называть как постоянный факт;",
                "— по вопросам криптооплаты не делать общих обещаний без проверки.",
            ]
        )

    lines.extend(
        [
            "",
            "🛡 Перед передачей партнёру вручную сверить факты, "
            "условия, цены, тарифы, доступность, оплату, бронирование "
            "и возможные риски.",
        ]
    )

    await message.answer("\n".join(lines))


async def _send_text_check(
    message: Message,
    decision: RouteDecision,
    provider: LLMProvider,
) -> None:
    result = await asyncio.to_thread(
        provider.check_text,
        source_text=decision.task_text,
    )
    if result is None:
        await message.answer(_TEXT_CHECK_FAILURE_MESSAGE)
        return

    lines: list[str] = ["🛡 Проверка текста", ""]

    if result.warnings:
        lines.append("Найдены рискованные формулировки:")
        for finding in result.warnings:
            lines.append(f"— «{finding.phrase}»: {finding.warning}")
    else:
        lines.append(
            "Рискованных формулировок по текущим правилам не найдено."
        )

    if result.rewritten_text:
        lines.extend(
            [
                "",
                "✍️ Безопасная переработанная версия — черновик:",
                "",
                result.rewritten_text,
            ]
        )

    if result.rewrite_warnings:
        lines.extend(
            [
                "",
                "⚠️ В переработанной версии ещё есть замечания:",
            ]
        )
        for finding in result.rewrite_warnings:
            lines.append(f"— «{finding.phrase}»: {finding.warning}")

    if result.ai_note:
        lines.extend(["", f"ℹ️ {result.ai_note}"])

    lines.extend(
        [
            "",
            (
                "🛡 Перед публикацией или отправкой вручную сверить факты, "
                "условия, цены, тарифы, доступность, оплату, бронирование "
                "и возможные риски."
            ),
        ]
    )

    await message.answer("\n".join(lines))




_CLIENT_REPLY_HEADING = "💬 Черновик ответа клиенту — для ручной проверки"
_FREE_TEXT_ARTIFACT_TITLE_MAX_LEN = 80


def _free_text_artifact_title(task_text: str) -> str:
    value = task_text.strip() or "пост"
    if len(value) <= _FREE_TEXT_ARTIFACT_TITLE_MAX_LEN:
        return f"Telegram: {value}"
    return f"Telegram: {value[:_FREE_TEXT_ARTIFACT_TITLE_MAX_LEN - 1].rstrip()}…"


async def _send_chunked(
    message: Message, text: str, *, reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Отправляет длинный результат несколькими Telegram-сообщениями.

    Telegram отклоняет одно сообщение длиннее ~4096 символов
    (``TelegramBadRequest: message is too long``); текст режется тем же
    механизмом, что и app/handlers/materials.py (chunk_text), без потери и
    без сокращения содержимого. reply_markup прикрепляется только к
    последнему чанку, чтобы не дублироваться после каждой части. Если сама
    отправка в Telegram падает (после успешной генерации), пользователь
    получает явное сообщение об ошибке вместо тишины.
    """
    chunks = chunk_text(text)
    try:
        for index, chunk in enumerate(chunks):
            is_last = index == len(chunks) - 1
            await message.answer(chunk, reply_markup=reply_markup if is_last else None)
    except TelegramAPIError:
        await message.answer(_DRAFT_SEND_FAILURE_MESSAGE)


async def _maybe_send_draft(
    message: Message,
    decision: RouteDecision,
    provider: LLMProvider,
    workspace_context: WorkspaceContext,
    partner_repository: PartnerRepository,
    *,
    work_repository: WorkRepository | None = None,
    artifact_repository: ArtifactRepository | None = None,
    conversation_state_repository: ConversationStateRepository | None = None,
    reply_context: ReplyBridgeContext | None = None,
    knowledge_bundle: KnowledgeBundle | None = None,
) -> None:
    workspace_id = workspace_context.workspace_id
    # Radar UX / free-text fix: раньше сюда дополнительно требовалось буквальное
    # слово "пост" (_is_regular_post) — из-за этого корректно
    # классифицированные CONTENT_FACTORY-задачи без слова "пост" ("разработай
    # стратегию...", "сделай контент-план...") молча отбрасывались без ответа
    # пользователю. Теперь единственный gate — сам primary_module.
    #
    # Fix: safety_level больше НЕ входит в этот gate. Раньше
    # primary=CONTENT_FACTORY + safety_level != NOT_REQUIRED (например,
    # «Перепиши этот пост: <текст с упоминанием тарифа>») делал is_regular_post
    # False, а is_client_reply тоже False (primary не TRAVEL_ASSISTANT) — и
    # функция тихо возвращалась ДО любого generate_draft/check_text вызова:
    # пользователь не получал вообще ничего, ни черновика, ни проверки.
    # Content Factory должна выполнить основную задачу (rewrite/generate)
    # первой; если safety_level требует проверки — Safety валидирует уже
    # готовый черновик ниже (см. content_safety_result), а не заменяет собой
    # генерацию.
    is_regular_post = decision.primary_module is Module.CONTENT_FACTORY
    is_client_reply = decision.primary_module is Module.TRAVEL_ASSISTANT
    if not is_regular_post and not is_client_reply:
        return

    profile = await partner_repository.get_business_profile(workspace_id)
    # Stage 3B1: личный стиль ТЕКУЩЕГО пользователя — UserStyleService читает
    # свою же запись по (workspace_id, telegram_user_id) из workspace_context,
    # структурно не может получить чужую (см. app/services/user_style.py).
    user_preferences = await UserStyleService(partner_repository).get(workspace_context)

    if is_regular_post:
        spec = MaterialOrchestrationService().build_free_text_generation_spec(
            workspace_id, decision.task_text, profile,
            user_preferences=user_preferences,
        )
        heading = "📝 Черновик для ручной проверки"
        # UX: multi-item/weekly_plan запросы (несколько дней/постов сразу)
        # реально занимают больше времени в Content Factory (удвоенный
        # max_output_tokens) — пользователь не должен решить, что бот завис,
        # пока идёт единственный долгий вызов ниже. Обычный одиночный пост
        # (output_format == "telegram") такого подтверждения не получает —
        # переиспользуем уже вычисленный spec.output_format, отдельного
        # regex/intent для "multi-item" не заводим.
        if spec.output_format is OutputFormat.WEEKLY_PLAN:
            await message.answer(_LONG_TASK_ACK_MESSAGE)
    else:
        spec = MaterialOrchestrationService().build_client_reply_generation_spec(
            workspace_id, decision.task_text, profile,
            safety_required=decision.safety_level is not SafetyLevel.NOT_REQUIRED,
            user_preferences=user_preferences,
            knowledge_bundle=knowledge_bundle,
        )
        heading = _CLIENT_REPLY_HEADING

    provider_request = build_provider_generation_request(spec, limit=6000)
    draft = await asyncio.to_thread(
        provider.generate_draft,
        source_text=provider_request.source_text,
        material_type=provider_request.material_type,
        output_format=provider_request.output_format,
        mode=_DRAFT_MODE,
    )
    if draft is None:
        await message.answer(_DRAFT_FAILURE_MESSAGE)
        return

    # UX polish: второй защитный слой поверх anti-AI-tail constraints в
    # prompt (см. commit b718685) — модель иногда всё равно заканчивает
    # черновик ассистентским self-offer'ом ("Могу сравнить варианты.",
    # "Если хотите, можем вместе проверить конкретный отель и даты.")
    # несмотря на инструкцию. Режем только это, не меняя середину текста
    # (см. app/services/assistant_tail_cleanup.py).
    draft_text = strip_assistant_tail(draft.text)

    # Content Factory всегда выполняет rewrite/generate первой (см. gate выше).
    # Если тема черновика требует Safety (RECOMMENDED/MANDATORY), Safety Layer
    # проверяет уже ГОТОВЫЙ результат — не подменяет собой генерацию и не
    # блокирует её. TRAVEL_ASSISTANT ниже сохраняет прежнее поведение
    # (статическая памятка без вызова check_text).
    content_safety_result = None
    needs_content_safety_check = (
        is_regular_post and decision.safety_level is not SafetyLevel.NOT_REQUIRED
    )
    if needs_content_safety_check:
        content_safety_result = await asyncio.to_thread(
            provider.check_text, source_text=draft_text,
        )

    lines: list[str] = [heading, "", draft_text]

    if (
        decision.primary_module is Module.TRAVEL_ASSISTANT
        and decision.safety_level is not SafetyLevel.NOT_REQUIRED
    ):
        lines.extend(
            [
                "",
                "🛡 Safety Layer: перед отправкой вручную сверить факты, "
                "условия, цены, доступность и риски.",
            ]
        )

    if draft.warnings:
        lines.append("")
        lines.append("⚠️ Предупреждения Content Factory:")
        for warning in draft.warnings:
            lines.append(f"— {warning}")
        lines.append("")
        lines.append("Текст требует ручной проверки перед отправкой.")

    if needs_content_safety_check:
        lines.append("")
        if content_safety_result is None:
            lines.append(
                "⚠️ Не удалось автоматически проверить черновик через Safety "
                "Layer. Проверьте вручную перед публикацией."
            )
        elif content_safety_result.warnings:
            lines.append("🛡 Safety Layer нашёл рискованные формулировки в черновике:")
            for finding in content_safety_result.warnings:
                lines.append(f"— «{finding.phrase}»: {finding.warning}")
            lines.append("")
        else:
            lines.append(
                "🛡 Safety Layer: рискованных формулировок по текущим правилам "
                "не найдено."
            )
        lines.append(
            "Перед публикацией вручную сверить факты, условия, цены, тарифы, "
            "доступность, оплату, бронирование и возможные риски."
        )

    if is_regular_post and artifact_repository is not None:
        # F2B: closes the structural gap confirmed in the F2A report - this
        # branch generated and showed text but never created an Artifact, so
        # Working State had no stable current_artifact_id for it. content is
        # exactly draft_text - the same variable embedded into `lines` above
        # and sent to the user below, no second LLM call, no re-derivation.
        # Not reachable when draft is None (see the early `return` above), so
        # a failed generation never creates an Artifact.
        try:
            artifact, _ = await artifact_repository.create_artifact_with_initial_version(
                workspace_id,
                artifact_type=spec.artifact_type,
                title=_free_text_artifact_title(decision.task_text),
                content=draft_text,
                generation_note=(
                    f"Content Factory (free text): "
                    f"{provider_request.material_type}/{provider_request.output_format}"
                ),
            )
        except Exception:
            # Best-effort bookkeeping only, same policy as ConversationStateService:
            # a technical persistence failure here must not turn an already-
            # generated, already-about-to-be-shown draft into an error for the
            # user (see the F2B report, failure policy). Unlike material_generation.py,
            # this flow never promised "saved to Мои материалы" to the user, so no
            # user-facing warning is added here either - the text below is
            # unchanged either way.
            log.warning("tasks: free-text Content Factory artifact persistence failed")
        else:
            await ConversationStateService(conversation_state_repository).record_artifact(
                workspace_id, workspace_context.telegram_user_id, artifact.id,
                active_module="content_factory", current_task="content_factory_free_text",
                last_action="generate_content",
            )

    reply_keyboard: InlineKeyboardMarkup | None = None
    if (
        decision.primary_module is Module.TRAVEL_ASSISTANT
        and reply_context is not None
        and work_repository is not None
    ):
        # Бизнес-правило («переиспользовать существующий work_item vs
        # создать новый + Artifact») живёт в ReplyWorkSyncService — transport-
        # independent, ничего не знает про InlineKeyboardMarkup. Клавиатуру
        # строим здесь же, сразу после: это Telegram-специфика.
        sync_service = ReplyWorkSyncService(
            work_repository, artifact_repository, conversation_state_repository,
        )
        updated_item = await sync_service.sync(
            workspace_id, workspace_context.telegram_user_id, draft_text, reply_context,
        )
        if updated_item is not None:
            reply_keyboard = reply_confirm_keyboard(
                updated_item.id, work_item_revision(updated_item),
            )

    await _send_chunked(message, "\n".join(lines), reply_markup=reply_keyboard)
