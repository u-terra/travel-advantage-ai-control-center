"""Offline-статистика по логам orchestration_shadow (Phase 2 shadow mode).

Полностью независим от Telegram, OpenAI и production: не импортирует ничего
из ``app.*``, не открывает сеть, не читает и не пишет БД. Единственный вход —
уже существующий текст лога (файл или stdin); единственный выход — отчёт в
stdout. Никак не влияет на работающего бота и не меняет формат логирования —
только читает то, что ``app.orchestration.shadow.ShadowComparisonLogger``
уже пишет через ``logging`` (см. этот модуль — формат строки скопирован
оттуда как контракт, который парсер обязан соблюдать построчно).

Строка лога (упрощённо, без timestamp/logger-префикса из ``basicConfig``):

    orchestration_shadow workspace_id=1 status=ok provider=openai
    latency_ms=1500 old_primary=Travel Content Factory old_secondary=
    old_safety=не требуется llm_intent=create_content
    llm_primary=Travel Content Factory llm_secondary= llm_safety=False
    llm_confidence=1.0 llm_reason_code=leading_rewrite_verb agreement=True
    task_text_preview='...'

``old_primary``/``llm_primary``/``old_safety`` могут содержать пробелы
("Travel Content Factory", "AI Lead Radar", "не требуется") — поэтому парсер
не делает наивный split по пробелам, а использует один regex с именованными
группами, заякоренный на литеральные "ключ=" между полями (нежадный ``.*?``
до следующего известного ключа). Строки, не содержащие "orchestration_shadow"
(обычные логи, трейсбеки, посторонний текст), молча пропускаются.

Запуск:
    python -m scripts.shadow_stats <log_file> [<log_file> ...]
    journalctl -u <service> | python -m scripts.shadow_stats -
"""

from __future__ import annotations

import argparse
import ast
import re
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Iterator

# Сколько примеров task_text_preview показывать на категорию в разделе
# "Potential improvements" - достаточно, чтобы понять паттерн, но не вывалить
# весь лог на экран.
_MAX_EXAMPLES_PER_CATEGORY = 5

# Значение SafetyLevel.NOT_REQUIRED.value из app/routing/safety.py.
# Продублировано намеренно (строкой, не импортом) — скрипт не должен зависеть
# от app.*: это чисто офлайн-парсер текста, независимый от production.
_SAFETY_NOT_REQUIRED = "не требуется"

# Единый regex на все поля строки: нежадный ``.*?`` между полями, где значение
# может содержать пробелы (module-имена, "не требуется"), и ``\S+`` там, где
# значение гарантированно однословное (числа, snake_case, True/False/None).
# ``re.search`` (не ``match``) — не важно, что стоит перед "orchestration_shadow"
# (timestamp, logger name, journalctl-префикс).
_LINE_PATTERN = re.compile(
    r"orchestration_shadow\s+"
    r"workspace_id=(?P<workspace_id>\S+)\s+"
    r"status=(?P<status>\S+)\s+"
    r"provider=(?P<provider>\S+)\s+"
    r"latency_ms=(?P<latency_ms>\S+)\s+"
    r"old_primary=(?P<old_primary>.*?)\s+"
    r"old_secondary=(?P<old_secondary>.*?)\s+"
    r"old_safety=(?P<old_safety>.*?)\s+"
    r"llm_intent=(?P<llm_intent>\S+)\s+"
    r"llm_primary=(?P<llm_primary>.*?)\s+"
    r"llm_secondary=(?P<llm_secondary>.*?)\s+"
    r"llm_safety=(?P<llm_safety>\S+)\s+"
    r"llm_confidence=(?P<llm_confidence>\S+)\s+"
    r"llm_reason_code=(?P<llm_reason_code>.*?)\s+"
    r"agreement=(?P<agreement>\S+)\s+"
    r"task_text_preview=(?P<task_text_preview>.*)$"
)


@dataclass(frozen=True)
class ShadowLogEntry:
    workspace_id: int
    status: str
    provider: str
    latency_ms: int
    old_primary: str
    old_secondary: tuple[str, ...]
    old_safety: str
    llm_intent: str | None
    llm_primary: str | None
    llm_secondary: tuple[str, ...]
    llm_safety: bool | None
    llm_confidence: float | None
    llm_reason_code: str | None
    agreement: bool | None
    task_text_preview: str = ""

    @property
    def old_safety_required(self) -> bool:
        """Тот же признак, что compute_agreement() в app/orchestration/shadow.py:
        любой уровень кроме NOT_REQUIRED считается требующим Safety."""
        return self.old_safety != _SAFETY_NOT_REQUIRED


def _opt_str(raw: str) -> str | None:
    return None if raw == "None" else raw


def _opt_bool(raw: str) -> bool | None:
    if raw == "True":
        return True
    if raw == "False":
        return False
    return None


def _opt_float(raw: str) -> float | None:
    if raw == "None":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _split_modules(raw: str) -> tuple[str, ...]:
    return tuple(part for part in raw.split(",") if part)


def _parse_preview(raw: str) -> str:
    """``task_text_preview`` is logged via ``%r`` (a Python repr), so the
    captured text is a valid Python string literal - ``ast.literal_eval``
    safely reverses the quoting/escaping. Falls back to the raw captured
    text on any parse failure (truncated line, unexpected shape) rather
    than raising."""
    try:
        value = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return raw
    return value if isinstance(value, str) else raw


def parse_shadow_line(line: str) -> ShadowLogEntry | None:
    """Возвращает разобранную запись или ``None``, если строка — не
    orchestration_shadow (обычный лог, ошибка, посторонний текст) либо не
    соответствует ожидаемому формату полей. Никогда не бросает исключение."""
    if "orchestration_shadow" not in line:
        return None
    match = _LINE_PATTERN.search(line)
    if match is None:
        return None
    g = match.groupdict()
    try:
        workspace_id = int(g["workspace_id"])
        latency_ms = int(g["latency_ms"])
    except ValueError:
        return None
    return ShadowLogEntry(
        workspace_id=workspace_id,
        status=g["status"],
        provider=g["provider"],
        latency_ms=latency_ms,
        old_primary=g["old_primary"],
        old_secondary=_split_modules(g["old_secondary"]),
        old_safety=g["old_safety"],
        llm_intent=_opt_str(g["llm_intent"]),
        llm_primary=_opt_str(g["llm_primary"]),
        llm_secondary=_split_modules(g["llm_secondary"]),
        llm_safety=_opt_bool(g["llm_safety"]),
        llm_confidence=_opt_float(g["llm_confidence"]),
        llm_reason_code=_opt_str(g["llm_reason_code"]),
        agreement=_opt_bool(g["agreement"]),
        task_text_preview=_parse_preview(g["task_text_preview"]),
    )


def parse_shadow_lines(lines: Iterable[str]) -> list[ShadowLogEntry]:
    entries = []
    for line in lines:
        entry = parse_shadow_line(line)
        if entry is not None:
            entries.append(entry)
    return entries


@dataclass(frozen=True)
class ImprovementCategory:
    """Одна категория "полезных расхождений" — не просто "решения разошлись",
    а конкретный, именованный класс случаев, где видно, что именно старый
    router делает хуже (или иначе), чем LLM."""

    name: str
    count: int
    examples: tuple[str, ...]


def compute_potential_improvements(
    ok_entries: list[ShadowLogEntry],
) -> tuple[ImprovementCategory, ...]:
    """Категоризация agreement=False записей (только status=ok - только там
    agreement вообще осмыслен) по классам "потенциальных улучшений". Категории
    не взаимоисключающие: одна запись может попасть в несколько (например,
    и "старый не определил задачу", и "LLM разошёлся по safety")."""
    disagreements = [entry for entry in ok_entries if entry.agreement is False]

    old_undetermined = [
        entry for entry in disagreements
        if entry.old_primary == "Orchestrator" and entry.llm_primary != "Orchestrator"
    ]
    llm_detected_feedback = [
        entry for entry in disagreements
        if entry.llm_intent == "feedback_on_previous_result"
    ]
    safety_diverged = [
        entry for entry in disagreements
        if entry.llm_safety is not None and entry.old_safety_required != entry.llm_safety
    ]
    safety_dangerous = [
        entry for entry in disagreements
        if entry.old_safety_required and entry.llm_safety is False
    ]

    def _examples(subset: list[ShadowLogEntry]) -> tuple[str, ...]:
        return tuple(entry.task_text_preview for entry in subset[:_MAX_EXAMPLES_PER_CATEGORY])

    return (
        ImprovementCategory(
            "Старый роутер не определил задачу (Orchestrator -> конкретный модуль у LLM)",
            len(old_undetermined), _examples(old_undetermined),
        ),
        ImprovementCategory(
            "LLM распознал feedback_on_previous_result",
            len(llm_detected_feedback), _examples(llm_detected_feedback),
        ),
        ImprovementCategory(
            "LLM разошёлся со старым роутером по safety_required",
            len(safety_diverged), _examples(safety_diverged),
        ),
        ImprovementCategory(
            "Потенциально опасные расхождения (старый требовал Safety, LLM - нет)",
            len(safety_dangerous), _examples(safety_dangerous),
        ),
    )


@dataclass(frozen=True)
class ShadowStats:
    total: int
    status_counts: Counter
    agreement_true: int
    agreement_false: int
    agreement_percent: float | None
    top_disagreements: list[tuple[tuple[str, str], int]]
    safety_old_required_llm_not: int
    safety_old_not_required_llm_required: int
    old_primary_distribution: Counter
    llm_primary_distribution: Counter
    llm_intent_distribution: Counter
    latency_mean: float | None
    latency_median: float | None
    potential_improvements: tuple[ImprovementCategory, ...]


def compute_stats(entries: list[ShadowLogEntry]) -> ShadowStats:
    status_counts = Counter(entry.status for entry in entries)
    ok_entries = [entry for entry in entries if entry.status == "ok"]

    agreement_true = sum(1 for entry in ok_entries if entry.agreement is True)
    agreement_false = sum(1 for entry in ok_entries if entry.agreement is False)
    compared = agreement_true + agreement_false
    agreement_percent = (agreement_true / compared * 100) if compared else None

    disagreement_pairs = Counter(
        (entry.old_primary, entry.llm_primary)
        for entry in ok_entries
        if entry.agreement is False
    )
    top_disagreements = disagreement_pairs.most_common(10)

    safety_old_required_llm_not = sum(
        1 for entry in ok_entries
        if entry.old_safety_required and entry.llm_safety is False
    )
    safety_old_not_required_llm_required = sum(
        1 for entry in ok_entries
        if not entry.old_safety_required and entry.llm_safety is True
    )

    old_primary_distribution = Counter(entry.old_primary for entry in entries)
    llm_primary_distribution = Counter(
        entry.llm_primary for entry in ok_entries if entry.llm_primary
    )
    llm_intent_distribution = Counter(
        entry.llm_intent for entry in ok_entries if entry.llm_intent
    )

    latencies = [entry.latency_ms for entry in ok_entries]
    latency_mean = statistics.mean(latencies) if latencies else None
    latency_median = statistics.median(latencies) if latencies else None

    potential_improvements = compute_potential_improvements(ok_entries)

    return ShadowStats(
        total=len(entries),
        status_counts=status_counts,
        agreement_true=agreement_true,
        agreement_false=agreement_false,
        agreement_percent=agreement_percent,
        top_disagreements=top_disagreements,
        safety_old_required_llm_not=safety_old_required_llm_not,
        safety_old_not_required_llm_required=safety_old_not_required_llm_required,
        old_primary_distribution=old_primary_distribution,
        llm_primary_distribution=llm_primary_distribution,
        llm_intent_distribution=llm_intent_distribution,
        latency_mean=latency_mean,
        latency_median=latency_median,
        potential_improvements=potential_improvements,
    )


def format_report(stats: ShadowStats) -> str:
    lines: list[str] = []

    lines.append("=== orchestration_shadow: общая статистика ===")
    lines.append(f"Всего shadow-записей: {stats.total}")
    lines.append(f"  status=ok:             {stats.status_counts.get('ok', 0)}")
    lines.append(f"  status=error:          {stats.status_counts.get('error', 0)}")
    lines.append(f"  status=invalid_output: {stats.status_counts.get('invalid_output', 0)}")
    known = {"ok", "error", "invalid_output"}
    other = stats.total - sum(stats.status_counts.get(s, 0) for s in known)
    if other:
        lines.append(f"  прочие статусы:        {other}")
    lines.append("")

    lines.append("=== Совпадение старого роутера и LLM (только status=ok) ===")
    compared = stats.agreement_true + stats.agreement_false
    if compared:
        lines.append(f"  agreement=True:  {stats.agreement_true}")
        lines.append(f"  agreement=False: {stats.agreement_false}")
        lines.append(f"  процент совпадений: {stats.agreement_percent:.1f}%")
    else:
        lines.append("  нет данных (нет записей status=ok)")
    lines.append("")

    lines.append("=== Top-10 расхождений: old_primary -> llm_primary (agreement=False) ===")
    if stats.top_disagreements:
        for (old_primary, llm_primary), count in stats.top_disagreements:
            lines.append(f"  {old_primary} -> {llm_primary} : {count}")
    else:
        lines.append("  расхождений не найдено")
    lines.append("")

    lines.append("=== Safety mismatch (только status=ok) ===")
    lines.append(
        f"  старый требовал Safety, LLM — нет:   {stats.safety_old_required_llm_not}"
    )
    lines.append(
        f"  старый НЕ требовал Safety, LLM — да: {stats.safety_old_not_required_llm_required}"
    )
    lines.append("")

    lines.append("=== Распределение: старый роутер (old_primary, все записи) ===")
    for module, count in stats.old_primary_distribution.most_common():
        lines.append(f"  {module}: {count}")
    lines.append("")

    lines.append("=== Распределение: LLM primary_module (только status=ok) ===")
    for module, count in stats.llm_primary_distribution.most_common():
        lines.append(f"  {module}: {count}")
    lines.append("")

    lines.append("=== Распределение: LLM intent (только status=ok) ===")
    for intent, count in stats.llm_intent_distribution.most_common():
        lines.append(f"  {intent}: {count}")
    lines.append("")

    lines.append("=== Latency, мс (только status=ok) ===")
    if stats.latency_mean is not None and stats.latency_median is not None:
        lines.append(f"  средняя:   {stats.latency_mean:.1f}")
        lines.append(f"  медианная: {stats.latency_median:.1f}")
    else:
        lines.append("  нет данных")
    lines.append("")

    lines.append("=== Potential improvements ===")
    lines.append(
        "Категории agreement=False, где видно не просто расхождение, а то, "
        "что LLM реально закрывает слабость старого router (не взаимоисключающие):"
    )
    lines.append("")
    for category in stats.potential_improvements:
        lines.append(f"[{category.name}] count={category.count}")
        if category.examples:
            for preview in category.examples:
                lines.append(f'    - "{preview}"')
        lines.append("")

    return "\n".join(lines).rstrip("\n")


def _read_lines(path: str) -> Iterator[str]:
    if path == "-":
        yield from sys.stdin
        return
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        yield from handle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Offline-статистика по orchestration_shadow логам. "
            "Read-only: без сети, без БД, не трогает production."
        )
    )
    parser.add_argument(
        "log_files",
        nargs="+",
        help="Путь(и) к лог-файлу; '-' читает stdin (например, из journalctl).",
    )
    args = parser.parse_args(argv)

    entries: list[ShadowLogEntry] = []
    for path in args.log_files:
        entries.extend(parse_shadow_lines(_read_lines(path)))

    if not entries:
        print("Не найдено ни одной строки orchestration_shadow во входных данных.")
        return 0

    print(format_report(compute_stats(entries)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
