from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.filters import MagicData
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, Message

from app.cards import build_card
from app.domain.partners import WorkspaceContext
from app.domain.work import WorkSubjectValidationError, work_item_revision
from app.handlers.menu import AwaitReplySubject, AwaitTask, BUTTON_HINTS
from app.keyboards import BTN_V2_CLIENT_REPLY, active_main_menu, reply_confirm_keyboard
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.partner_repository import PartnerRepository
from app.repositories.work_repository import WorkRepository
from app.routing.modules import Module
from app.routing.router import RouteDecision, route_for_button, route_text
from app.routing.safety import SafetyLevel
from app.services.generation_request_builder import build_provider_generation_request
from app.services.llm.base import LLMProvider
from app.services.material_orchestration import MaterialOrchestrationService
from app.services.reply_sync import ReplyBridgeContext, ReplyWorkSyncService
from app.services.user_style import UserStyleService
from app.storage import Journal

router = Router(name="tasks")

_DRAFT_MODE = "ai"

_NON_POST_FORMAT_MARKERS: tuple[str, ...] = (
    "reels", "рилс", "рилз",
    "stories", "сторис",
    "сценар",
    "контент-план", "контент план",
)
_POST_MARKERS: tuple[str, ...] = ("пост",)

_DRAFT_FAILURE_MESSAGE = (
    "Не удалось получить черновик автоматически. "
    "Можно открыть Travel Content Factory вручную."
)

_TEXT_CHECK_FAILURE_MESSAGE = (
    "Не удалось проверить текст автоматически. "
    "Попробуйте ещё раз или откройте Travel Content Factory вручную."
)

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
            message, journal, llm_provider, workspace_context, partner_repository,
            text, forced_module=Module.TRAVEL_ASSISTANT, skip_route_card=True,
            reply_subject_data=(None, None, None),
            work_repository=work_repository, artifact_repository=artifact_repository,
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
    v2_menu_enabled: bool = False,
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
        message, journal, llm_provider, workspace_context, partner_repository,
        task_text, forced_module=forced_module, skip_route_card=skip_route_card,
        reply_subject_data=(reply_work_item_id, reply_subject_id, reply_subject_name),
        work_repository=work_repository, artifact_repository=artifact_repository,
        v2_menu_enabled=v2_menu_enabled,
    )


@router.message(F.text & ~F.text.startswith("/"))
async def on_free_text(
    message: Message,
    journal: Journal,
    llm_provider: LLMProvider,
    workspace_context: WorkspaceContext | None,
    partner_repository: PartnerRepository,
    v2_menu_enabled: bool = False,
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
    decision = route_text(task_text)
    await journal.add(
        workspace_context.workspace_id,
        task_text=task_text,
        primary_module=decision.primary_module.value,
        secondary_modules=tuple(m.value for m in decision.secondary_modules),
        safety_level=decision.safety_level.value,
    )
    # UX polish: в v2 прямой свободный текст — самый частый вход в create
    # material/reply flow, и техническая «📌 Карточка маршрута» тут не нужна
    # (см. тот же принцип у skip_route_card в on_task_after_button/v2-кнопках)
    # — пользователь сразу должен увидеть результат/следующий шаг. В v1 карточка
    # остаётся как раньше.
    if not v2_menu_enabled:
        await message.answer(
            build_card(decision), reply_markup=active_main_menu(v2_menu_enabled)
        )
    await _maybe_send_module_result(
        message, decision, llm_provider,
        workspace_context, partner_repository,
    )


async def _route_and_dispatch(
    message: Message,
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
    v2_menu_enabled: bool = False,
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
        reply_context=reply_context,
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
    reply_context: ReplyBridgeContext | None = None,
) -> None:
    if decision.primary_module is Module.SAFETY_LAYER:
        await _send_text_check(message, decision, provider)
        return

    if decision.primary_module is Module.PARTNER_PACKAGING:
        await _send_partner_package(
            message, decision, workspace_context.workspace_id, partner_repository,
        )
        return

    await _maybe_send_draft(
        message, decision, provider, workspace_context, partner_repository,
        work_repository=work_repository, artifact_repository=artifact_repository,
        reply_context=reply_context,
    )



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




def _is_regular_post(text_lower: str) -> bool:
    if any(marker in text_lower for marker in _NON_POST_FORMAT_MARKERS):
        return False
    return any(marker in text_lower for marker in _POST_MARKERS)


_CLIENT_REPLY_HEADING = "💬 Черновик ответа клиенту — для ручной проверки"


async def _maybe_send_draft(
    message: Message,
    decision: RouteDecision,
    provider: LLMProvider,
    workspace_context: WorkspaceContext,
    partner_repository: PartnerRepository,
    *,
    work_repository: WorkRepository | None = None,
    artifact_repository: ArtifactRepository | None = None,
    reply_context: ReplyBridgeContext | None = None,
) -> None:
    workspace_id = workspace_context.workspace_id
    is_regular_post = (
        decision.primary_module is Module.CONTENT_FACTORY
        and decision.safety_level is SafetyLevel.NOT_REQUIRED
        and _is_regular_post(decision.task_text.lower())
    )
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
    else:
        spec = MaterialOrchestrationService().build_client_reply_generation_spec(
            workspace_id, decision.task_text, profile,
            safety_required=decision.safety_level is not SafetyLevel.NOT_REQUIRED,
            user_preferences=user_preferences,
        )
        heading = _CLIENT_REPLY_HEADING

    provider_request = build_provider_generation_request(spec)
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

    lines: list[str] = [heading, "", draft.text]

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
        sync_service = ReplyWorkSyncService(work_repository, artifact_repository)
        updated_item = await sync_service.sync(workspace_id, draft.text, reply_context)
        if updated_item is not None:
            reply_keyboard = reply_confirm_keyboard(
                updated_item.id, work_item_revision(updated_item),
            )

    await message.answer("\n".join(lines), reply_markup=reply_keyboard)
