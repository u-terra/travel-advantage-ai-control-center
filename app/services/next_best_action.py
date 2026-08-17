"""«Что делать сегодня»: rule-based выбор 1-3 действий, без LLM.

Чистая функция от уже полученных, workspace-scoped данных: сервис не
открывает БД и ничего не знает про aiosqlite/Telegram/Lead Radar. Репозитории
достают и скоуп-фильтруют данные (WorkRepository.list_open_actionable и т.д.),
этот сервис только ранжирует уже авторизованный набор.

Приоритет (низкое число — выше в списке), в БД не хранится и пересчитывается
каждый раз:
  1. active_dialog                — открытый диалог, ваш ход
  2. due_follow_up                — waiting_reply с наступившим due_at
  3. open_content                 — уже отслеживаемый открытый content work_item
  4. draft_content                — черновик/review_required без открытого work_item
  5. signal_opportunity           — новый/high-score сигнал
  6. profile_incomplete           — профиль не дозаполнен
  7. cold_contact_fallback        — playbook «написать новому человеку»

fallback «написать новому человеку» — это НЕ производная от сигналов: это
общий playbook-кандидат (TA или обычный бизнес, в зависимости от
ta_affiliated), который существует независимо от того, есть ли вообще
сигналы. Смешивать эти два источника не нужно: сигнал — конкретная тема,
fallback — простое напоминание, что можно начать новый контакт вручную.
"""

from __future__ import annotations

from typing import Sequence

from app.domain.content import Artifact
from app.domain.work import (
    DailyActions,
    NextAction,
    SignalOpportunity,
    WaitingSubject,
    WorkItemView,
)


MAX_ACTIONS = 3

_TIER_ACTIVE_DIALOG = 1
_TIER_DUE_FOLLOW_UP = 2
_TIER_OPEN_CONTENT = 3
_TIER_DRAFT_CONTENT = 4
_TIER_SIGNAL_OPPORTUNITY = 5
_TIER_PROFILE_INCOMPLETE = 6
_TIER_COLD_CONTACT_FALLBACK = 7

# Ключ — ta_affiliated. Никакой TA-формулировки для False-ветки: это условие
# проверяется тестами и обязано соблюдаться при любых будущих правках текста.
_COLD_CONTACT_TEXT: dict[bool, tuple[str, str]] = {
    True: (
        "Найдите нового собеседника",
        "Начните разговор с новым контактом о Travel Advantage — по вашему "
        "обычному сценарию.",
    ),
    False: (
        "Найдите нового клиента",
        "Начните разговор с новым контактом — по вашему обычному сценарию "
        "работы с клиентами.",
    ),
}

_PROFILE_INCOMPLETE_TEXT = (
    "Дозаполните профиль",
    "Профиль ещё не заполнен полностью — это влияет на качество черновиков и ответов.",
)


class NextBestActionService:
    def build(
        self,
        *,
        workspace_id: int,
        ta_affiliated: bool,
        open_work_items: Sequence[WorkItemView] = (),
        waiting_not_due: Sequence[WorkItemView] = (),
        profile_incomplete: bool = False,
        draft_artifacts: Sequence[Artifact] = (),
        signal_candidates: Sequence[SignalOpportunity] = (),
        include_cold_contact_fallback: bool = True,
    ) -> DailyActions:
        for view in (*open_work_items, *waiting_not_due):
            if view.item.workspace_id != workspace_id:
                raise PermissionError("work_item не принадлежит workspace")
        for artifact in draft_artifacts:
            if artifact.workspace_id != workspace_id:
                raise PermissionError("Artifact не принадлежит workspace")

        # draft/review_required артефакты, уже отслеживаемые открытым
        # work_item, не должны задваиваться как отдельный derived-кандидат.
        covered_artifact_ids = {
            view.item.ref_id
            for view in open_work_items
            if view.item.ref_type == "artifact"
        }

        candidates: list[tuple[int, NextAction]] = []
        candidates.extend(_explicit_candidates(open_work_items))
        candidates.extend(_draft_candidates(draft_artifacts, covered_artifact_ids))
        candidates.extend(_signal_candidates(signal_candidates))
        if profile_incomplete:
            candidates.append((_TIER_PROFILE_INCOMPLETE, _profile_incomplete_action()))
        if include_cold_contact_fallback:
            candidates.append(
                (_TIER_COLD_CONTACT_FALLBACK, _cold_contact_fallback_action(ta_affiliated))
            )

        candidates.sort(key=lambda pair: pair[0])
        actions = tuple(action for _, action in candidates[:MAX_ACTIONS])
        waiting = tuple(_waiting_subject(view) for view in waiting_not_due)
        return DailyActions(actions=actions, waiting=waiting)


def _explicit_candidates(
    open_work_items: Sequence[WorkItemView],
) -> list[tuple[int, NextAction]]:
    result: list[tuple[int, NextAction]] = []
    for view in open_work_items:
        item = view.item
        if item.kind == "dialog" and item.loop_state == "active_dialog":
            tier, source = _TIER_ACTIVE_DIALOG, "active_dialog"
        elif item.kind == "dialog" and item.loop_state == "waiting_reply":
            tier, source = _TIER_DUE_FOLLOW_UP, "due_follow_up"
        elif item.kind == "content":
            tier, source = _TIER_OPEN_CONTENT, "open_content"
        else:
            continue
        result.append((tier, NextAction(
            source=source,
            headline=view.subject_name or item.next_step or "Незавершённое действие",
            detail=item.next_step,
            work_item_id=item.id,
            subject_name=view.subject_name,
            artifact_id=item.ref_id if item.ref_type == "artifact" else None,
            signal_interpretation_id=item.ref_id if item.ref_type == "signal" else None,
        )))
    return result


def _draft_candidates(
    draft_artifacts: Sequence[Artifact], covered_artifact_ids: set[int | None],
) -> list[tuple[int, NextAction]]:
    return [
        (_TIER_DRAFT_CONTENT, NextAction(
            source="draft_content",
            headline=f"Вернуться к черновику «{artifact.title}»",
            detail="Материал ждёт проверки перед публикацией.",
            artifact_id=artifact.id,
        ))
        for artifact in draft_artifacts
        if artifact.id not in covered_artifact_ids
    ]


def _signal_candidates(
    signal_candidates: Sequence[SignalOpportunity],
) -> list[tuple[int, NextAction]]:
    return [
        (_TIER_SIGNAL_OPPORTUNITY, NextAction(
            source="signal_opportunity",
            headline=signal.title,
            detail=signal.reason,
            signal_interpretation_id=signal.signal_interpretation_id,
        ))
        for signal in signal_candidates
    ]


def _profile_incomplete_action() -> NextAction:
    headline, detail = _PROFILE_INCOMPLETE_TEXT
    return NextAction(source="profile_incomplete", headline=headline, detail=detail)


def _cold_contact_fallback_action(ta_affiliated: bool) -> NextAction:
    headline, detail = _COLD_CONTACT_TEXT[bool(ta_affiliated)]
    return NextAction(source="cold_contact_fallback", headline=headline, detail=detail)


def _waiting_subject(view: WorkItemView) -> WaitingSubject:
    return WaitingSubject(
        work_item_id=view.item.id,
        subject_id=view.item.subject_id,
        subject_name=view.subject_name,
        next_step=view.item.next_step,
        due_at=view.item.due_at,
    )
