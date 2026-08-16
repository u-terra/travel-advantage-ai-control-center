from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.cards import build_card
from app.domain.partners import WorkspaceContext
from app.handlers.menu import AwaitTask
from app.keyboards import active_main_menu
from app.repositories.partner_repository import PartnerRepository
from app.routing.modules import Module
from app.routing.router import RouteDecision, route_for_button, route_text
from app.routing.safety import SafetyLevel
from app.services.generation_request_builder import build_provider_generation_request
from app.services.llm.base import LLMProvider
from app.services.material_orchestration import MaterialOrchestrationService
from app.storage import Journal

router = Router(name="tasks")

_CLIENT_QUESTION_MATERIAL_TYPE = "client_question"
_DRAFT_OUTPUT_FORMAT = "telegram"
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


@router.message(AwaitTask.waiting, F.text & ~F.text.startswith("/"))
async def on_task_after_button(
    message: Message,
    state: FSMContext,
    journal: Journal,
    llm_provider: LLMProvider,
    workspace_context: WorkspaceContext | None,
    partner_repository: PartnerRepository,
    v2_menu_enabled: bool = False,
) -> None:
    data = await state.get_data()
    forced_raw = data.get("forced_module")
    skip_route_card = bool(data.get("skip_route_card"))
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

    if forced_raw:
        forced = Module(forced_raw)
        decision = route_for_button(forced, task_text)
    else:
        decision = route_text(task_text)

    await journal.add(
        workspace_context.workspace_id,
        task_text=task_text,
        primary_module=decision.primary_module.value,
        secondary_modules=tuple(m.value for m in decision.secondary_modules),
        safety_level=decision.safety_level.value,
    )
    if skip_route_card:
        await _maybe_send_module_result(
            message, decision, llm_provider,
            workspace_context.workspace_id, partner_repository,
        )
        return

    await message.answer(
        build_card(decision), reply_markup=active_main_menu(v2_menu_enabled)
    )
    await _maybe_send_module_result(
        message, decision, llm_provider,
        workspace_context.workspace_id, partner_repository,
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
    await message.answer(
        build_card(decision), reply_markup=active_main_menu(v2_menu_enabled)
    )
    await _maybe_send_module_result(
        message, decision, llm_provider,
        workspace_context.workspace_id, partner_repository,
    )


async def _maybe_send_module_result(
    message: Message,
    decision: RouteDecision,
    provider: LLMProvider,
    workspace_id: int,
    partner_repository: PartnerRepository,
) -> None:
    if decision.primary_module is Module.SAFETY_LAYER:
        await _send_text_check(message, decision, provider)
        return

    if decision.primary_module is Module.PARTNER_PACKAGING:
        await _send_partner_package(
            message, decision, workspace_id, partner_repository,
        )
        return

    await _maybe_send_draft(
        message, decision, provider, workspace_id, partner_repository,
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
                "— не обещать окупаемость, доход или результат;",
                "— не представлять партнёрский формат как трудоустройство.",
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


def _draft_request_for(
    decision: RouteDecision,
) -> tuple[str, str, str] | None:
    """Определяет, нужен ли безопасный черновик и в каком формате."""
    if decision.primary_module is Module.TRAVEL_ASSISTANT:
        safety_instruction = ""
        if decision.safety_level is not SafetyLevel.NOT_REQUIRED:
            safety_instruction = (
                "\n\nЭто вопрос с обязательной Safety-проверкой. "
                "Не сообщай цены, тарифы, доступность, способы оплаты, "
                "варианты бронирования или сравнения как установленный факт. "
                "Дай только общее объяснение и прямо укажи, что конкретные "
                "условия нужно сверить вручную."
            )

        source_text = (
            "Нужен короткий личный ответ клиенту для Telegram.\n"
            f"Вопрос клиента: {decision.task_text}\n\n"
            "Ответь простыми словами и по существу. Не обещай доход, "
            "окупаемость или гарантированные скидки. Не утверждай, что "
            "формат подходит всем. Не используй фразу «без давления». "
            "Если точных данных недостаточно, не выдумывай: предложи "
            "уточнить детали или спокойно разобрать вопрос лично."
            + safety_instruction
        )
        return (
            source_text,
            _CLIENT_QUESTION_MATERIAL_TYPE,
            "💬 Черновик ответа клиенту — для ручной проверки",
        )

    return None


async def _maybe_send_draft(
    message: Message,
    decision: RouteDecision,
    provider: LLMProvider,
    workspace_id: int,
    partner_repository: PartnerRepository,
) -> None:
    if (
        decision.primary_module is Module.CONTENT_FACTORY
        and decision.safety_level is SafetyLevel.NOT_REQUIRED
        and _is_regular_post(decision.task_text.lower())
    ):
        profile = await partner_repository.get_business_profile(workspace_id)
        spec = MaterialOrchestrationService().build_free_text_generation_spec(
            workspace_id, decision.task_text, profile,
        )
        provider_request = build_provider_generation_request(spec)
        source_text = provider_request.source_text
        material_type = provider_request.material_type
        output_format = provider_request.output_format
        heading = "📝 Черновик для ручной проверки"
    else:
        request = _draft_request_for(decision)
        if request is None:
            return
        source_text, material_type, heading = request
        output_format = _DRAFT_OUTPUT_FORMAT

    draft = await asyncio.to_thread(
        provider.generate_draft,
        source_text=source_text,
        material_type=material_type,
        output_format=output_format,
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

    await message.answer("\n".join(lines))
