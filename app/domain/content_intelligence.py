"""Контракт разбора материала из источника (Content Intelligence v1).

Это ТОЛЬКО словарь понятий: что за материал попался и что с ним можно
сделать. Здесь нет pipeline, нет фонового мониторинга и нет обращений к LLM —
модуль ничего не запускает и ни от чего не зависит.

Смысл — зафиксировать значения заранее, чтобы позже реестр источников можно
было подключить к анализу, не переделывая ни хранение, ни существующий
provider layer. Классификация появляется рядом с уже существующим
``SourceAnalysisPayload``, а не вместо него: тот отвечает на вопрос «что в
материале», а этот — «какого он рода и что с ним делать».

Действие — это ПРЕДЛОЖЕНИЕ владельцу, а не команда. Ничего не публикуется
автоматически.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

# ── Род материала ────────────────────────────────────────────────────────────

KIND_NEWS = "news"
KIND_POST_IDEA = "post_idea"
KIND_SCRIPT_IDEA = "script_idea"
KIND_CASE_OR_REVIEW = "case_or_review"
KIND_COMPETITOR_SIGNAL = "competitor_signal"
KIND_NOISE = "noise"

MATERIAL_KINDS: tuple[str, ...] = (
    KIND_NEWS,
    KIND_POST_IDEA,
    KIND_SCRIPT_IDEA,
    KIND_CASE_OR_REVIEW,
    KIND_COMPETITOR_SIGNAL,
    KIND_NOISE,
)

# ── Возможное действие ───────────────────────────────────────────────────────

ACTION_MAKE_POST = "make_post"
ACTION_MAKE_SCRIPT = "make_script"
ACTION_GENERATE_TOPICS = "generate_topics"
ACTION_ADAPT = "adapt_for_travel_advantage"
ACTION_OBSERVE = "observe"
ACTION_SKIP = "skip"

MATERIAL_ACTIONS: tuple[str, ...] = (
    ACTION_MAKE_POST,
    ACTION_MAKE_SCRIPT,
    ACTION_GENERATE_TOPICS,
    ACTION_ADAPT,
    ACTION_OBSERVE,
    ACTION_SKIP,
)

# Действие по умолчанию для каждого рода материала. Это подсказка, а не
# правило: явно указанное действие всегда важнее.
DEFAULT_ACTION_BY_KIND: Mapping[str, str] = {
    KIND_NEWS: ACTION_GENERATE_TOPICS,
    KIND_POST_IDEA: ACTION_MAKE_POST,
    KIND_SCRIPT_IDEA: ACTION_MAKE_SCRIPT,
    KIND_CASE_OR_REVIEW: ACTION_ADAPT,
    KIND_COMPETITOR_SIGNAL: ACTION_OBSERVE,
    KIND_NOISE: ACTION_SKIP,
}


def normalize_material_kind(raw: str) -> str:
    """Известный род материала или пустая строка.

    Пустая строка вместо исключения нужна разбору ответа модели: неизвестное
    значение не должно ронять сценарий, но и молча превращаться в `news` —
    тоже. Решение принимает вызывающий код.
    """
    value = (raw or "").strip().lower()
    return value if value in MATERIAL_KINDS else ""


def normalize_material_action(raw: str) -> str:
    """Известное действие или пустая строка."""
    value = (raw or "").strip().lower()
    return value if value in MATERIAL_ACTIONS else ""


def default_action_for(kind: str) -> str:
    """Предлагаемое действие для рода материала.

    Незнакомый род даёт ``observe``: наблюдать безопаснее, чем что-то делать
    с материалом, который система не поняла.
    """
    return DEFAULT_ACTION_BY_KIND.get(normalize_material_kind(kind), ACTION_OBSERVE)


@dataclass(frozen=True)
class MaterialClassification:
    """Род материала и предлагаемое действие.

    ``source_id`` — id записи из реестра источников, если материал пришёл
    оттуда. Для вставленного вручную текста он пустой.
    """

    kind: str
    action: str
    rationale: str = ""
    source_id: str = ""

    def __post_init__(self) -> None:
        if self.kind not in MATERIAL_KINDS:
            raise ValueError(
                f"неизвестный род материала: {self.kind!r}. "
                f"Допустимо: {', '.join(MATERIAL_KINDS)}"
            )
        if self.action not in MATERIAL_ACTIONS:
            raise ValueError(
                f"неизвестное действие: {self.action!r}. "
                f"Допустимо: {', '.join(MATERIAL_ACTIONS)}"
            )

    @property
    def is_actionable(self) -> bool:
        """Требует ли материал работы. `observe`/`skip` — нет."""
        return self.action not in (ACTION_OBSERVE, ACTION_SKIP)

    @classmethod
    def from_raw(
        cls,
        *,
        kind: str,
        action: str = "",
        rationale: str = "",
        source_id: str = "",
    ) -> Optional["MaterialClassification"]:
        """Собирает классификацию из «сырых» значений (например, ответа LLM).

        ``None`` — если род материала распознать не удалось. Пропущенное
        действие подставляется по роду материала.
        """
        normalized_kind = normalize_material_kind(kind)
        if not normalized_kind:
            return None
        normalized_action = normalize_material_action(action) or default_action_for(
            normalized_kind
        )
        return cls(
            kind=normalized_kind,
            action=normalized_action,
            rationale=(rationale or "").strip(),
            source_id=(source_id or "").strip(),
        )
