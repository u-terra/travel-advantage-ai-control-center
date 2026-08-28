from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import MagicData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.domain.partners import WorkspaceContext
from app.keyboards import (
    BTN_CHECK_TEXT,
    BTN_CLIENT_QUESTION,
    BTN_CREATE_CONTENT,
    BTN_FIND_SIGNALS,
    BTN_LAST_TASK,
    BTN_PACKAGE_MATERIALS,
    BTN_UNSURE,
    BTN_WEB_RESOURCES,
    BTN_V2_CHECK_TEXT,
    BTN_V2_CLIENT_REPLY,
    BTN_V2_HELP,
    BTN_V2_CREATE_MATERIAL,
    BTN_V2_ANALYZE_LINK,
    BTN_V2_FIND_SIGNALS,
    BTN_V2_MAIN_MENU,
    CATEGORY_BUTTONS,
    MATERIAL_ENTRY_ANALYZE,
    MATERIAL_ENTRY_FIND_SIGNALS,
    TA_WEB_RESOURCE_LINKS,
    V2_CATEGORY_BUTTONS,
    V2_PLACEHOLDER_BUTTONS,
    WEB_RESOURCES_BACK,
    active_main_menu,
    main_menu,
    material_entry_keyboard,
    material_result_keyboard,
    v2_back_keyboard,
    web_resources_keyboard,
)
from app.domain.action_contract import ActionContract, ActionContractValidationError
from app.domain.conversation_state import OfferItem
from app.handlers.source_analysis import start_source_analysis
from app.orchestration.context import record_turn
from app.routing.modules import Module
from app.routing.safety import SafetyLevel
from app.services.lead_radar import (
    LeadRadarConfig,
    build_summary,
    build_workspace_signals,
    route_card,
    unavailable_summary,
)
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.conversation_state_repository import (
    ConversationStateConflictError,
    ConversationStateRepository,
)
from app.repositories.partner_repository import PartnerRepository
from app.repositories.workspace_signal_repository import WorkspaceSignalRepository
from app.services.conversation_state_service import ConversationStateService
from app.services.draft_sanitizer import sanitize_draft_text
from app.services.generation_request_builder import build_provider_generation_request
from app.services.llm.base import LLMProvider
from app.services.material_orchestration import MaterialOrchestrationService
from app.services.user_style import UserStyleService
from app.storage import Journal

router = Router(name="menu")
log = logging.getLogger(__name__)

_RADAR_CONTENT_PREFIX = "radar_content:"
_RADAR_CONTENT_LIMIT = 3
# F2A: TTL for the persisted PendingOffer/PendingQuestion mirrors of these
# buttons - generous enough to outlive a realistic "user steps away", short
# enough that a stale offer/question does not linger indefinitely.
_CONVERSATION_TTL = timedelta(minutes=30)
_DRAFT_MODE = "ai"
_RADAR_ARTIFACT_FAILURE = (
    "⚠️ Материал создан, но сохранить его в «Мои материалы» не удалось. "
    "Скопируйте текст ниже, чтобы не потерять его."
)
# Fail-closed (не fail-open): без Source Analysis нет disputed_claims и Stage 1
# Content Quality Gate не может дать никаких гарантий про черновик — значит
# черновик вообще не генерируется, а не генерируется непроверенным.
_RADAR_ANALYSIS_UNAVAILABLE = (
    "⚠️ Сейчас не удалось проверить исходный материал.\n"
    "Черновик не создан, чтобы не передавать вам непроверенные сведения.\n"
    "Попробуйте ещё раз позже."
)

_SAFETY_CLEAN = "🛡 Проверка\nСущественных замечаний нет."
_SAFETY_HEADER = "🛡 Проверка\nПеред публикацией лучше перепроверить:"
# Fail-closed, как и в draft_sanitizer: сам текст disputed_claim уже вырезан
# из sanitized_text и НЕ должен попадать пользователю ни в каком виде, в том
# числе как "цитата" в предупреждении — иначе непроверенный факт всё равно
# долетает до пользователя, просто через другое поле сообщения. Поэтому здесь
# только обезличенное уведомление о факте вырезки, без цитирования claim.
_SAFETY_UNVERIFIED_NOTE = (
    "часть фактов из источника не подтверждена и не вошла в черновик — "
    "при необходимости уточните детали отдельно"
)


def _format_radar_safety(
    draft_warnings: tuple[str, ...], disputed_claims: tuple[str, ...]
) -> str:
    """Единый компактный блок «🛡 Проверка» вместо технических «Предупреждения
    Content Factory» и вместо возможных дублирующихся «Существенных замечаний
    нет» — по одному короткому сообщению на каждый исход, без имён внутренних
    валидаторов.
    """
    items: list[str] = []
    seen: set[str] = set()
    for warning in draft_warnings:
        warning = warning.strip()
        if not warning:
            continue
        key = warning.lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(warning)
    if disputed_claims:
        items.append(_SAFETY_UNVERIFIED_NOTE)
    if not items:
        return _SAFETY_CLEAN
    lines = [_SAFETY_HEADER, *(f"— {item}" for item in items)]
    return "\n".join(lines)


class AwaitTask(StatesGroup):
    waiting = State()


class AwaitReplySubject(StatesGroup):
    """Промежуточный шаг перед AwaitTask.waiting только для TRAVEL_ASSISTANT:
    «Кому отвечаем?» — до того, как получить сам вопрос/сообщение клиента.
    См. on_reply_subject_received в app/handlers/tasks.py."""

    waiting = State()


BUTTON_TO_MODULE: dict[str, Module] = {
    BTN_CREATE_CONTENT: Module.CONTENT_FACTORY,
    BTN_CLIENT_QUESTION: Module.TRAVEL_ASSISTANT,
    BTN_CHECK_TEXT: Module.SAFETY_LAYER,
    BTN_PACKAGE_MATERIALS: Module.PARTNER_PACKAGING,
    BTN_V2_CLIENT_REPLY: Module.TRAVEL_ASSISTANT,
}


BUTTON_HINTS: dict[str, str] = {
    BTN_CREATE_CONTENT: (
        "Опишите задачу для контента. Примеры:\n"
        "— Нужен Telegram-пост о сомнениях перед поездкой.\n"
        "— Сделай сценарий Reels о сравнении вариантов.\n"
        "— Напиши ответ на возражение «Это сетевой маркетинг?»."
    ),
    BTN_CLIENT_QUESTION: (
        "Опишите вопрос клиента. Примеры:\n"
        "— Чем Travel Advantage отличается от обычного поиска отелей?\n"
        "— Можно ли оплатить бронирование из России?\n"
        "— Какие есть варианты тарифа?"
    ),
    BTN_CHECK_TEXT: (
        "Пришлите текст, который нужно проверить перед публикацией или отправкой человеку."
    ),
    BTN_PACKAGE_MATERIALS: (
        "Опишите, какие материалы нужно подготовить для партнёра. Примеры:\n"
        "— Инструкция по Travel Content Factory.\n"
        "— Коммерческое предложение по настройке AI-инструмента.\n"
        "— Презентация продукта для нового партнёра."
    ),
    BTN_V2_CLIENT_REPLY: (
        "Опишите вопрос клиента или сообщение, на которое нужно подготовить ответ."
    ),
    BTN_UNSURE: (
        "Опишите задачу обычным языком — даже если она смешанная. "
        "Бот попробует разложить её на отдельные маршруты."
    ),
}

# Шаг перед вопросом клиента: имя/метка нужны только чтобы связать между
# собой несколько сообщений одного человека в «Что делать сегодня» — не
# контакт, не CRM-поле, поэтому телефон/email/канал не спрашиваются. Имя
# необязательно: если вместо имени сразу прислать сообщение/вопрос клиента,
# бот распознает это и обработает сразу (см. _looks_like_client_message в
# app/handlers/tasks.py) — повторно вводить его не нужно.
_REPLY_SUBJECT_PROMPT = (
    "Кому отвечаем?\n"
    "Можно сразу прислать сообщение или вопрос клиента — отвечу на него.\n"
    "Имя или короткое обозначение указывать необязательно, но если хотите "
    "их отдельно отметить, напишите перед сообщением, например:\n"
    "Иван\n"
    "Мария\n"
    "Клиент по Турции"
)


def _short_title(text: str, max_len: int = 42) -> str:
    value = (text or "").strip() or "Идея без заголовка"
    if len(value) <= max_len:
        return value
    return value[: max_len - 1].rstrip() + "…"


def _radar_content_keyboard(
    ideas: list[dict[str, str]],
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"📝 {index}. {_short_title(idea['title'])}",
                    callback_data=f"{_RADAR_CONTENT_PREFIX}{idea['interpretation_id']}",
                )
            ]
            for index, idea in enumerate(ideas, start=1)
        ]
    )


def _radar_content_ideas(signals) -> list[dict[str, str]]:
    return [
        {
            "interpretation_id": str(signal.id),
            "title": signal.title,
            "reason": signal.action_reason,
            "url": signal.url,
        }
        for signal in signals
        if signal.recommended_action == "content"
    ][:_RADAR_CONTENT_LIMIT]


def _conversation_expiry() -> str:
    return (datetime.now(timezone.utc) + _CONVERSATION_TTL).isoformat()


def _radar_content_action_contract(raw_data: str) -> ActionContract | None:
    """F2A button->ActionContract pilot for the Radar content-idea buttons.

    Deterministic callback_data -> ActionContract translation, nothing more:
    no resolver, no free-text, no guessing. ``interpretation_id`` cannot be
    modeled as ActionContract.subject_ref (CONVERSATION_SUBJECT_REF_TYPES has
    no "signal" noun - see app.domain.conversation_state), so it travels in
    slots instead; that is exactly what slots are for.
    """
    raw_id = raw_data.removeprefix(_RADAR_CONTENT_PREFIX)
    if not raw_id.isdigit() or int(raw_id) <= 0:
        return None
    try:
        return ActionContract(
            intent="select_radar_content_idea",
            action="generate_radar_draft",
            subject_ref_type=None,
            subject_ref_id=None,
            slots={"interpretation_id": raw_id},
            source="button",
            confidence=1.0,
        )
    except ActionContractValidationError:
        return None


async def _record_radar_content_offer(
    conversation_state_repository: ConversationStateRepository | None,
    workspace_id: int,
    telegram_user_id: int,
    ideas: list[dict[str, str]],
) -> None:
    """F2A PendingOffer pilot: mirrors the already-rendered Radar content-idea
    keyboard into persisted storage, additively. ``ideas`` is already the
    same structured Python list used to build the keyboard (see
    ``on_find_signals`` below) - nothing here is parsed out of rendered text
    or invented from a free-text reply (see the F2A "не парсить LLM-ответ"
    constraint). Best-effort/never blocks the actual keyboard the user sees.
    """
    if conversation_state_repository is None or not ideas:
        return
    try:
        items = tuple(
            OfferItem(
                id=idea["interpretation_id"],
                label=_short_title(idea["title"]),
                # Small ref/payload only - no large text copied in, per the
                # F2A "не копировать огромные тексты в payload" constraint.
                payload={"reason": idea.get("reason") or "", "url": idea.get("url") or ""},
            )
            for idea in ideas
        )
        await conversation_state_repository.create_offer(
            workspace_id, telegram_user_id, "radar_content_ideas", items,
            expires_at=_conversation_expiry(),
        )
    except ConversationStateConflictError:
        # A still-live offer already exists for this user (e.g. Radar was
        # run twice in quick succession) - the new keyboard above is still
        # shown either way; the persisted mirror simply keeps the older one
        # until it is consumed or expires.
        log.info("menu: radar content PendingOffer already active, skipping")
    except Exception:
        log.warning("menu: radar content PendingOffer persistence failed")


async def _consume_radar_content_offer(
    conversation_state_repository: ConversationStateRepository | None,
    workspace_id: int,
    telegram_user_id: int,
    interpretation_id: int,
) -> None:
    """F2B: marks the PendingOffer item the user just selected as consumed.

    Fails open, not closed: a missing/expired/foreign-offer/stale-item
    situation is only ever logged, never surfaced to the user - this button
    was already a valid, working Telegram control before F2A/F2B existed,
    and persisted-offer bookkeeping must not regress that.
    """
    if conversation_state_repository is None:
        return
    try:
        # F2D: offer_type is now required - radar_content_ideas and
        # content_topics offers can be active for the same user at once
        # (see the F2D unique-index fix), so this must ask specifically for
        # the Radar offer, never whichever offer happens to be active.
        offer = await conversation_state_repository.get_active_offer(
            workspace_id, telegram_user_id, "radar_content_ideas"
        )
        if offer is None:
            log.info("menu: no active radar_content_ideas offer to consume")
            return
        if str(interpretation_id) not in {item.id for item in offer.items}:
            log.info("menu: selected idea is not part of the active offer, skipping consume")
            return
        await conversation_state_repository.consume_offer(
            workspace_id, telegram_user_id, offer.id
        )
    except Exception:
        log.warning("menu: radar content PendingOffer consume failed")


@router.message(F.text.in_(CATEGORY_BUTTONS))
async def on_category(message: Message, state: FSMContext) -> None:
    module = BUTTON_TO_MODULE[message.text]
    await state.update_data(forced_module=module.value)
    await state.set_state(AwaitTask.waiting)
    await message.answer(BUTTON_HINTS[message.text], reply_markup=main_menu())


@router.message(MagicData(F.v2_menu_enabled), F.text.in_(V2_CATEGORY_BUTTONS))
async def on_v2_category(message: Message, state: FSMContext) -> None:
    module = BUTTON_TO_MODULE[message.text]
    await state.update_data(forced_module=module.value, skip_route_card=True)
    if module is Module.TRAVEL_ASSISTANT:
        # «Ответить клиенту» сначала спрашивает «Кому отвечаем?» — вопрос
        # клиента ждём только на следующем шаге, см. AwaitReplySubject и
        # on_reply_subject_received в app/handlers/tasks.py.
        await state.set_state(AwaitReplySubject.waiting)
        await message.answer(_REPLY_SUBJECT_PROMPT, reply_markup=active_main_menu(True))
        return
    await state.set_state(AwaitTask.waiting)
    await message.answer(
        BUTTON_HINTS[message.text],
        reply_markup=active_main_menu(True),
    )

@router.message(MagicData(F.v2_menu_enabled), F.text == BTN_V2_MAIN_MENU)
async def on_v2_main_menu(
    message: Message, state: FSMContext
) -> None:
    await state.clear()
    await message.answer(
        "Главное меню. Выберите нужную задачу.",
        reply_markup=active_main_menu(True),
    )


@router.message(MagicData(F.v2_menu_enabled), F.text == BTN_V2_HELP)
async def on_v2_help(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "ℹ️ Что можно сделать:\n\n"
        "✍️ Создать материал — выбрать способ подготовки нового материала.\n"
        "💬 Ответить клиенту — подготовить черновик ответа на вопрос клиента.\n"
        "📡 Найти сигналы и идеи — найти свежие сигналы и идеи для контента.\n"
        "📝 Разобрать публикацию — разобрать вставленный текст новости, поста "
        "или публикации и подготовить материал на его основе.\n"
        "🛡 Проверить и улучшить текст — проверить текст перед публикацией.\n"
        "📚 Мои материалы — открыть последние сохранённые материалы.\n"
        "📚 Источники — управлять источниками, за которыми следит бот.\n"
        "🎯 Мои конкуренты — сохранить ссылки на конкурентов для анализа.\n"
        "⚙️ Профиль — посмотреть бизнес-профиль, который использует Оркестратор.",
        reply_markup=v2_back_keyboard(),
    )


@router.message(MagicData(F.v2_menu_enabled), F.text.in_(V2_PLACEHOLDER_BUTTONS))
async def on_v2_placeholder(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Раздел подготовлен в новой структуре. Рабочий сценарий будет подключён "
        "на следующем этапе.",
        reply_markup=v2_back_keyboard(),
    )


@router.message(MagicData(F.v2_menu_enabled), F.text == BTN_V2_CREATE_MATERIAL)
async def on_v2_create_material(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Как хотите создать материал?\n\n"
        "Можно разобрать текст публикации и подготовить материал на его "
        "основе — либо найти свежие сигналы и идеи для контента.",
        reply_markup=v2_back_keyboard(),
    )
    await message.answer(
        "Выберите способ:",
        reply_markup=material_entry_keyboard(),
    )


@router.callback_query(MagicData(F.v2_menu_enabled), F.data == MATERIAL_ENTRY_FIND_SIGNALS)
async def on_material_entry_find_signals(
    callback: CallbackQuery,
    state: FSMContext,
    lead_radar_config: LeadRadarConfig,
    workspace_signal_repository: WorkspaceSignalRepository,
    workspace_context: WorkspaceContext | None,
) -> None:
    """«Создать материал» → «Найти сигналы» переиспользует on_find_signals
    напрямую: тот же результат, что и у прямой кнопки главного меню, без
    какой-либо технической карточки маршрута."""
    await callback.answer()
    if callback.message is None:
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await on_find_signals(
        callback.message, state, lead_radar_config,
        workspace_signal_repository, workspace_context, True,
    )


@router.callback_query(MagicData(F.v2_menu_enabled), F.data == MATERIAL_ENTRY_ANALYZE)
async def on_material_entry_analyze(
    callback: CallbackQuery, state: FSMContext,
) -> None:
    """«Создать материал» → «Разобрать публикацию» переиспользует
    start_source_analysis напрямую — тот же сценарий, что и прямая кнопка."""
    await callback.answer()
    if callback.message is None:
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await start_source_analysis(callback.message, state)


@router.message(F.text == BTN_UNSURE)
async def on_unsure(message: Message, state: FSMContext) -> None:
    await state.update_data(forced_module=None)
    await state.set_state(AwaitTask.waiting)
    await message.answer(BUTTON_HINTS[BTN_UNSURE], reply_markup=main_menu())


_WEB_RESOURCES_EMPTY = (
    "🌐 Веб-ресурсы\n\n"
    "Для вашего рабочего пространства пока не настроены дополнительные "
    "веб-ресурсы. Обратитесь к администратору сервиса, чтобы добавить "
    "ссылки в профиль."
)


def _own_workspace_links(profile) -> tuple[tuple[str, str], ...]:
    """Ссылки из public_contacts самого workspace — никогда не TA."""
    links: list[tuple[str, str]] = []
    for key, value in profile.context.public_contacts.items():
        address = (value or "").strip()
        if address.startswith("http://") or address.startswith("https://"):
            title = key.strip().replace("_", " ").capitalize() or "Ссылка"
            links.append((f"🔗 {title}", address))
    return tuple(links)


async def _resolve_web_resource_links(
    partner_repository: PartnerRepository,
    workspace_context: WorkspaceContext | None,
) -> tuple[tuple[str, str], ...]:
    """Ссылки для конкретного workspace — никакого общего fallback на TA.

    Ресурсы Travel Advantage видит только workspace с явным признаком
    profile.ta_affiliated. business_type описывает тип бизнеса (в т.ч. у
    сторонних клубов/партнёров) и сам по себе НЕ означает аффилиацию с
    Travel Advantage — поэтому здесь он не участвует в решении. Остальные
    workspace видят только ссылки собственного профиля.
    """
    if workspace_context is None:
        return ()
    profile = await partner_repository.get_business_profile(
        workspace_context.workspace_id
    )
    if profile is None:
        return ()
    if profile.ta_affiliated:
        return TA_WEB_RESOURCE_LINKS
    return _own_workspace_links(profile)


@router.message(F.text == BTN_WEB_RESOURCES)
async def on_web_resources(
    message: Message,
    workspace_context: WorkspaceContext | None,
    partner_repository: PartnerRepository,
) -> None:
    links = await _resolve_web_resource_links(partner_repository, workspace_context)
    if not links:
        await message.answer(_WEB_RESOURCES_EMPTY, reply_markup=web_resources_keyboard(links))
        return
    await message.answer(
        "🌐 Веб-ресурсы. Откройте нужный сайт в браузере.",
        reply_markup=web_resources_keyboard(links),
    )


@router.callback_query(F.data == WEB_RESOURCES_BACK)
async def on_web_resources_back(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            "Главное меню. Выберите кнопку или напишите задачу текстом.",
            reply_markup=main_menu(),
        )


@router.message(F.text == BTN_FIND_SIGNALS)
@router.message(MagicData(F.v2_menu_enabled), F.text == BTN_V2_FIND_SIGNALS)
async def on_find_signals(
    message: Message,
    state: FSMContext,
    lead_radar_config: LeadRadarConfig,
    workspace_signal_repository: WorkspaceSignalRepository,
    workspace_context: WorkspaceContext | None,
    v2_menu_enabled: bool = False,
    conversation_state_repository: ConversationStateRepository | None = None,
) -> None:
    await state.clear()
    if workspace_context is None:
        await message.answer(
            "Рабочее пространство недоступно.",
            reply_markup=active_main_menu(v2_menu_enabled),
        )
        return
    await message.answer(route_card(), reply_markup=active_main_menu(v2_menu_enabled))
    await workspace_signal_repository.sync_eligible()
    records = await workspace_signal_repository.list_for_workspace(
        workspace_context.workspace_id, limit=200
    )
    signals = build_workspace_signals(lead_radar_config, records, limit=5)
    if signals is None:
        await message.answer(unavailable_summary())
        return

    await message.answer(build_summary(signals), disable_web_page_preview=True)

    ideas = _radar_content_ideas(signals)
    if not ideas:
        return

    await _record_radar_content_offer(
        conversation_state_repository, workspace_context.workspace_id,
        workspace_context.telegram_user_id, ideas,
    )

    await message.answer(
        "💡 Выберите идею, чтобы подготовить черновик Telegram-поста. "
        "Ничего не публикуется автоматически.",
        reply_markup=_radar_content_keyboard(ideas),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith(_RADAR_CONTENT_PREFIX))
async def on_radar_content_selected(
    callback: CallbackQuery,
    state: FSMContext,
    journal: Journal,
    llm_provider: LLMProvider,
    workspace_context: WorkspaceContext | None,
    workspace_signal_repository: WorkspaceSignalRepository,
    lead_radar_config: LeadRadarConfig,
    partner_repository: PartnerRepository,
    artifact_repository: ArtifactRepository,
    conversation_state_repository: ConversationStateRepository | None = None,
) -> None:
    # F2A button->ActionContract pilot: the callback_data is deterministically
    # translated into a validated ActionContract before anything else runs.
    # This is intentionally the ONLY thing the contract changes here - once
    # built, interpretation_id is read back out of it and every line below is
    # the exact same existing business logic as before (same repository
    # calls, same draft generation, same Artifact persistence). No second
    # executor/business path is introduced - see _radar_content_action_contract.
    contract = _radar_content_action_contract(callback.data or "")
    if contract is None:
        await callback.answer(
            "Не удалось определить выбранную идею.",
            show_alert=True,
        )
        return
    interpretation_id = int(contract.slots["interpretation_id"])

    if workspace_context is None:
        await callback.answer("Рабочее пространство недоступно.", show_alert=True)
        return

    # F2B: consume the PendingOffer created in on_find_signals, if it is
    # still the active one and actually contains this item. Best-effort
    # bookkeeping only - a missing/expired/foreign offer must never turn
    # this already-valid Telegram button into an error (see F2B report,
    # failure policy).
    await _consume_radar_content_offer(
        conversation_state_repository, workspace_context.workspace_id,
        workspace_context.telegram_user_id, interpretation_id,
    )

    record = await workspace_signal_repository.get_for_workspace(
        workspace_context.workspace_id, interpretation_id
    )
    if record is None:
        await callback.answer("Сигнал недоступен.", show_alert=True)
        return
    authorized = build_workspace_signals(lead_radar_config, [record], limit=1)
    if not authorized:
        await callback.answer("Сигнал недоступен.", show_alert=True)
        return
    signal = authorized[0]

    profile = await partner_repository.get_business_profile(
        workspace_context.workspace_id
    )
    # Stage 3B1: личный стиль ТЕКУЩЕГО пользователя (не workspace) — читает
    # свою же запись по (workspace_id, telegram_user_id) из workspace_context.
    user_preferences = await UserStyleService(partner_repository).get(workspace_context)

    # Stage 1 Content Quality Gate: переиспользуем существующий Source
    # Analysis (тот же llm_provider.analyze_source(), что и в обычном
    # Content Factory flow) до генерации черновика, а не отдельный
    # параллельный механизм.
    radar_source_text = "\n".join(
        value for value in (record.item_title, record.item_summary) if value
    )
    analysis = await asyncio.to_thread(
        llm_provider.analyze_source, source_text=radar_source_text
    )
    if analysis is None:
        # Fail-closed: сеть/таймаут/ошибка анализа — генерация не запускается
        # вообще (generate_draft() ниже не вызывается), Artifact не создаётся.
        # Пользователю — короткое сообщение без технических деталей.
        await callback.answer("Готовлю черновик…")
        if callback.message is not None:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer(_RADAR_ANALYSIS_UNAVAILABLE)
        return

    spec = MaterialOrchestrationService().build_radar_generation_spec(
        workspace_context.workspace_id,
        profile,
        title=record.item_title,
        summary=record.item_summary,
        source_type=record.source_type,
        origin_type=record.origin_type,
        url=record.item_url,
        category=record.ai_category or "",
        reason=record.ai_reason or signal.action_reason,
        analysis=analysis,
        user_preferences=user_preferences,
    )
    request = build_provider_generation_request(spec)
    task_text = f"Radar draft: {signal.title}"

    await callback.answer("Готовлю черновик…")
    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)

    await journal.add(
        workspace_context.workspace_id,
        task_text=task_text,
        primary_module=Module.CONTENT_FACTORY.value,
        secondary_modules=(),
        safety_level=SafetyLevel.NOT_REQUIRED.value,
    )

    draft = await asyncio.to_thread(
        llm_provider.generate_draft,
        source_text=request.source_text,
        material_type=request.material_type,
        output_format=request.output_format,
        mode=_DRAFT_MODE,
    )

    if callback.message is None:
        return

    if draft is None:
        await callback.message.answer(
            "Не удалось получить черновик автоматически. "
            "Можно открыть Travel Content Factory вручную."
        )
        return

    # Stage 1 Content Quality Gate: детерминированная зачистка ДО показа и ДО
    # сохранения Artifact — не warning постфактум, а реальное удаление
    # ассистентских концовок, мета-фраз о процессе и предложений, дословно
    # пересказывающих disputed_claims (см. app/services/draft_sanitizer.py).
    sanitized_text = sanitize_draft_text(
        draft.text,
        disputed_claims=analysis.disputed_claims if analysis is not None else (),
    )
    if not sanitized_text:
        await callback.message.answer(
            "Не удалось получить черновик автоматически. "
            "Можно открыть Travel Content Factory вручную."
        )
        return

    safety_block = _format_radar_safety(
        draft.warnings,
        analysis.disputed_claims if analysis is not None else (),
    )
    lines: list[str] = [
        "📝 Черновик по идее из Radar — для ручной проверки",
        "",
        sanitized_text,
        "",
        safety_block,
    ]
    draft_text = "\n".join(lines)

    # Phase 1 LLM orchestration (shadow mode): records this as a PAST
    # ASSISTANT RESULT so a follow-up free-text reaction ("зачем ты
    # предлагаешь этот никчёмный повод...") can be recognized as feedback on
    # THIS idea rather than a new content request. Does not touch the Radar
    # quality gate above - purely additive bookkeeping. Best-effort/silent by
    # construction - see app.orchestration.context.record_turn.
    await record_turn(
        state, role="assistant",
        text=f"Radar предложил пост-идею: {signal.title}",
        module=Module.LEAD_RADAR.value,
    )

    try:
        artifact, _ = await artifact_repository.create_artifact_with_initial_version(
            workspace_context.workspace_id,
            artifact_type=spec.artifact_type,
            title=f"Telegram: {signal.title}",
            content=sanitized_text,
            generation_note=f"Lead Radar: {request.material_type}/{request.output_format}",
        )
    except Exception:
        log.warning("menu: radar artifact persistence failed")
        await callback.message.answer(_RADAR_ARTIFACT_FAILURE)
        await callback.message.answer(draft_text, reply_markup=v2_back_keyboard())
        return

    await ConversationStateService(conversation_state_repository).record_artifact(
        workspace_context.workspace_id, workspace_context.telegram_user_id, artifact.id,
        active_module="lead_radar", current_task="radar_content_draft",
        last_action="radar_content_draft_created",
    )

    await callback.message.answer(
        draft_text, reply_markup=material_result_keyboard(artifact.id)
    )


@router.message(F.text == BTN_LAST_TASK)
async def on_last_task(
    message: Message,
    journal: Journal,
    workspace_context: WorkspaceContext | None,
) -> None:
    if workspace_context is None:
        await message.answer(
            "Рабочее пространство недоступно.", reply_markup=main_menu()
        )
        return
    entry = await journal.last(workspace_context.workspace_id)
    if entry is None:
        await message.answer(
            "Журнал пуст. Поставьте задачу через меню или текстом.",
            reply_markup=main_menu(),
        )
        return
    text = (
        "📋 Последняя задача\n\n"
        f"Дата (UTC): {entry.created_at}\n"
        f"Задача: {entry.task_text}\n"
        f"Основной модуль: {entry.primary_module}\n"
        f"Дополнительный модуль: {entry.secondary_modules or 'не требуется'}\n"
        f"Safety Layer: {entry.safety_level}\n"
        f"Статус: {entry.status}\n"
        f"Заметка: {entry.note or '—'}"
    )
    await message.answer(text, reply_markup=main_menu())
