"""Planner eligibility gate - deterministic, no LLM call.

Deliberately narrow: this is not a general "is this task complex" classifier.
It recognizes exactly the three Phase 1 MVP scenarios named in the Planner
architecture note:

1. Competitor analysis (a "конкурент" word plus an explicit analyze/compare
   verb - see ``app.repositories.competitor_repository``, which today only
   stores a URL and never analyzes it).
2. Content tasks explicitly composed of multiple, *different* actions
   (write + check + package, not just "one long post" or "N posts").
3. Multi-step research (an explicit multi-source/multi-step research
   request).

A request must NOT become eligible merely because it is long, or because it
asks for a quantity/duration of the same single action (e.g. "10 posts",
"a 2-week content plan") - those stay on the existing keyword router and
``MaterialOrchestrationService`` (see
``app.services.material_orchestration._wants_weekly_content_plan``, which
already handles that case as a single Content Factory call). Mixing that
signal into eligibility would silently reroute today's working, tested
content flow into disabled/incomplete Planner scaffolding once a live
provider is wired.
"""

from __future__ import annotations

import re

_COMPETITOR_WORD = re.compile(r"конкурент\w*", re.IGNORECASE)
_COMPETITOR_ACTION_VERB = re.compile(
    r"(проанализ\w*|анализ\w*|разбер\w*|разобра\w*|сравни\w*|изучи\w*|оцени\w*)",
    re.IGNORECASE,
)

_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
# Same analysis/differentiation intent as competitor analysis, but keyed on
# an actual URL in the message rather than the word "конкурент" - Stage 3
# scenario H: "Проанализируй https://example.com и предложи, как нам
# отстроиться" names no competitor by word, only by URL.
_URL_ANALYSIS_VERB = re.compile(
    r"(проанализ\w*|анализ\w*|сравни\w*|изучи\w*|оцени\w*|отстро\w*|выдели\w*|отлич\w*)",
    re.IGNORECASE,
)

_RESEARCH_PHRASE = re.compile(
    r"(многошагов\w*\s+исследован\w*|провед\w*\s+исследован\w*|"
    r"собери\s+информац\w*\s+из\s+нескольк\w*\s+источник\w*|"
    r"исследован\w*\s+рынк\w*)",
    re.IGNORECASE,
)

# Explicit sequencing language - "do A, then B" - not just a long request.
_SEQUENCE_CONNECTOR = re.compile(
    r"(\bзатем\b|\bпотом\b|после этого|а после|шаг\s*\d|этап\s*\d)",
    re.IGNORECASE,
)
_NUMBERED_STEP_LINE = re.compile(r"(?m)^\s*\d+[.)]\s+\S")

# Distinct action categories. A multi-action content task must reference at
# least two of these DIFFERENT categories - repeating the same category
# (e.g. "напиши, потом создай ещё один пост") is still a single kind of
# action, not a chain across modules/executors.
_ACTION_VERB_GROUPS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(напиши\w*|создай\w*|сгенерируй\w*|составь\w*|подготовь\w*)", re.IGNORECASE),
    re.compile(r"(провер\w*|оцени\w*\s+риск\w*)", re.IGNORECASE),
    re.compile(r"(оформ\w*|упакуй\w*|подгот\w*\s+комплект\w*)", re.IGNORECASE),
    re.compile(r"(опубликуй\w*|отправ\w*|раздай\w*)", re.IGNORECASE),
)


def _has_numbered_steps(text: str) -> bool:
    return len(_NUMBERED_STEP_LINE.findall(text)) >= 2


def _is_competitor_analysis(text_lower: str) -> bool:
    return bool(
        _COMPETITOR_WORD.search(text_lower) and _COMPETITOR_ACTION_VERB.search(text_lower)
    )


def _is_url_analysis_request(text: str) -> bool:
    return bool(_URL_PATTERN.search(text) and _URL_ANALYSIS_VERB.search(text.lower()))


def _is_multistep_research(text_lower: str) -> bool:
    return bool(_RESEARCH_PHRASE.search(text_lower))


def _is_multi_action_content_task(text: str, text_lower: str) -> bool:
    has_sequence_marker = bool(_SEQUENCE_CONNECTOR.search(text_lower)) or _has_numbered_steps(
        text
    )
    if not has_sequence_marker:
        return False
    matched_groups = sum(1 for pattern in _ACTION_VERB_GROUPS if pattern.search(text_lower))
    return matched_groups >= 2


def is_planner_eligible(task_text: str) -> bool:
    """True only for the three narrow Phase 1 Planner scenarios.

    Everything else - including long single-action requests such as "10
    posts" or "a 2-week content plan" - stays on the existing
    ``app.routing.router.route_text`` path unchanged.
    """
    if not isinstance(task_text, str):
        return False
    text = task_text.strip()
    if not text:
        return False
    text_lower = text.lower()
    return (
        _is_competitor_analysis(text_lower)
        or _is_url_analysis_request(text)
        or _is_multistep_research(text_lower)
        or _is_multi_action_content_task(text, text_lower)
    )


def is_planner_allowed_for_user(
    telegram_user_id: int | None, allowed_user_ids: frozenset[int],
) -> bool:
    """Staged-rollout gate - a SEPARATE axis from ``is_planner_eligible``
    (which is about task content, not user identity). Checked first, before
    task eligibility and before any LLM call: an out-of-allowlist user must
    never trigger a Planner LLM call regardless of what they typed.

    Fail-closed by design: an empty/unset allowlist denies everyone, it does
    NOT mean "no restriction" - staged rollout (e.g. "enable Planner only for
    my own Telegram id") must not accidentally become "enabled for
    everyone" just because the operator has not populated the list yet.
    """
    if telegram_user_id is None:
        return False
    return telegram_user_id in allowed_user_ids
