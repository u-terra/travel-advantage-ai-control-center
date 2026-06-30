from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.cards import build_card
from app.handlers.menu import AwaitTask
from app.keyboards import main_menu
from app.routing.modules import Module
from app.routing.router import RouteDecision, route_for_button, route_text
from app.routing.safety import SafetyLevel
from app.services.content_factory import (
    ContentFactoryConfig,
    check_text_sync,
    generate_draft_sync,
)
from app.storage import Journal

router = Router(name="tasks")

_POST_MATERIAL_TYPE = "market_offer"
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
    content_factory_config: ContentFactoryConfig,
) -> None:
    data = await state.get_data()
    forced_raw = data.get("forced_module")
    task_text = (message.text or "").strip()
    await state.clear()

    if not task_text:
        await message.answer("Пустой запрос. Опишите задачу.", reply_markup=main_menu())
        return

    if forced_raw:
        forced = Module(forced_raw)
        decision = route_for_button(forced, task_text)
    else:
        decision = route_text(task_text)

    await journal.add(
        task_text=task_text,
        primary_module=decision.primary_module.value,
        secondary_modules=tuple(m.value for m in decision.secondary_modules),
        safety_level=decision.safety_level.value,
    )
    await message.answer(build_card(decision), reply_markup=main_menu())
    await _maybe_send_module_result(message, decision, content_factory_config)


@router.message(F.text & ~F.text.startswith("/"))
async def on_free_text(
    message: Message,
    journal: Journal,
    content_factory_config: ContentFactoryConfig,
) -> None:
    task_text = (message.text or "").strip()
    if not task_text:
        await message.answer("Пустой запрос. Опишите задачу.", reply_markup=main_menu())
        return
    decision = route_text(task_text)
    await journal.add(
        task_text=task_text,
        primary_module=decision.primary_module.value,
        secondary_modules=tuple(m.value for m in decision.secondary_modules),
        safety_level=decision.safety_level.value,
    )
    await message.answer(build_card(decision), reply_markup=main_menu())
    await _maybe_send_module_result(message, decision, content_factory_config)


async def _maybe_send_module_result(
    message: Message,
    decision: RouteDecision,
    config: ContentFactoryConfig,
) -> None:
    if decision.primary_module is Module.SAFETY_LAYER:
        await _send_text_check(message, decision, config)
        return

    if decision.primary_module is Module.PARTNER_PACKAGING:
        await _send_partner_package(message, decision)
        return

    await _maybe_send_draft(message, decision, config)



async def _send_partner_package(
    message: Message,
    decision: RouteDecision,
) -> None:
    """Формирует лёгкий MVP-комплект материалов без AI и внешних вызовов."""
    task = decision.task_text.strip()
    short_task = task if len(task) <= 900 else f"{task[:897]}..."
    task_lower = task.lower()

    lines: list[str] = [
        "📦 Черновик комплекта материалов для партнёра",
        "",
        "Основа запроса:",
        short_task,
        "",
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
    config: ContentFactoryConfig,
) -> None:
    result = await asyncio.to_thread(
        check_text_sync,
        config,
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
    if decision.primary_module is Module.CONTENT_FACTORY:
        if decision.safety_level is not SafetyLevel.NOT_REQUIRED:
            return None
        if not _is_regular_post(decision.task_text.lower()):
            return None
        return (
            decision.task_text,
            _POST_MATERIAL_TYPE,
            "📝 Черновик для ручной проверки",
        )

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
    config: ContentFactoryConfig,
) -> None:
    request = _draft_request_for(decision)
    if request is None:
        return

    source_text, material_type, heading = request

    draft = await asyncio.to_thread(
        generate_draft_sync,
        config,
        source_text=source_text,
        material_type=material_type,
        output_format=_DRAFT_OUTPUT_FORMAT,
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
