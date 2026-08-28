"""Тесты офлайн-парсера orchestration_shadow логов (scripts/shadow_stats.py).

Ничего не трогает в app/**: чисто текстовый парсер + агрегация, без сети,
без БД, без Telegram/OpenAI.
"""

from __future__ import annotations

import pytest

from scripts.shadow_stats import (
    ShadowLogEntry,
    compute_potential_improvements,
    compute_stats,
    format_report,
    parse_shadow_line,
    parse_shadow_lines,
)


def _ok_line(
    *,
    workspace_id: int = 1,
    old_primary: str = "Travel Content Factory",
    old_secondary: str = "",
    old_safety: str = "не требуется",
    llm_intent: str = "create_content",
    llm_primary: str = "Travel Content Factory",
    llm_secondary: str = "",
    llm_safety: str = "False",
    llm_confidence: str = "1.0",
    llm_reason_code: str = "leading_rewrite_verb",
    agreement: str = "True",
    latency_ms: int = 1500,
    provider: str = "openai",
    task_text_preview: str = "'Напиши пост про Travel Advantage'",
) -> str:
    return (
        "2026-08-25 12:07:41,922 INFO app.orchestration.shadow: "
        "orchestration_shadow "
        f"workspace_id={workspace_id} status=ok provider={provider} "
        f"latency_ms={latency_ms} old_primary={old_primary} "
        f"old_secondary={old_secondary} old_safety={old_safety} "
        f"llm_intent={llm_intent} llm_primary={llm_primary} "
        f"llm_secondary={llm_secondary} llm_safety={llm_safety} "
        f"llm_confidence={llm_confidence} llm_reason_code={llm_reason_code} "
        f"agreement={agreement} task_text_preview={task_text_preview}"
    )


def _error_line(*, workspace_id: int = 2, latency_ms: int = 300) -> str:
    return (
        "orchestration_shadow "
        f"workspace_id={workspace_id} status=error provider=openai "
        f"latency_ms={latency_ms} old_primary=Safety Layer old_secondary= "
        "old_safety=обязателен llm_intent=None llm_primary=None "
        "llm_secondary= llm_safety=None llm_confidence=None "
        "llm_reason_code=None agreement=None task_text_preview='x'"
    )


def _invalid_output_line(*, workspace_id: int = 3, latency_ms: int = 400) -> str:
    return (
        "orchestration_shadow "
        f"workspace_id={workspace_id} status=invalid_output provider=openai "
        f"latency_ms={latency_ms} old_primary=AI Travel Assistant old_secondary= "
        "old_safety=не требуется llm_intent=None llm_primary=None "
        "llm_secondary= llm_safety=None llm_confidence=None "
        "llm_reason_code=None agreement=None task_text_preview='y'"
    )


# --- Парсинг: пробелы в названиях модулей ------------------------------------


def test_parses_module_names_with_spaces_not_split_on_whitespace():
    line = _ok_line(
        old_primary="Travel Content Factory",
        llm_primary="AI Lead Radar",
        old_safety="не требуется",
    )
    entry = parse_shadow_line(line)
    assert entry is not None
    assert entry.old_primary == "Travel Content Factory"
    assert entry.llm_primary == "AI Lead Radar"
    assert entry.old_safety == "не требуется"


def test_parses_secondary_modules_list():
    line = _ok_line(old_secondary="Safety Layer,AI Lead Radar")
    entry = parse_shadow_line(line)
    assert entry is not None
    assert entry.old_secondary == ("Safety Layer", "AI Lead Radar")


def test_parses_empty_secondary_as_empty_tuple():
    entry = parse_shadow_line(_ok_line(old_secondary=""))
    assert entry is not None
    assert entry.old_secondary == ()


def test_parses_task_text_preview_unescaping_the_repr():
    """task_text_preview is logged via %r (a Python repr) - the parser must
    reverse that (ast.literal_eval), not keep the surrounding quotes."""
    entry = parse_shadow_line(
        _ok_line(task_text_preview="'Напиши пост про Travel Advantage'")
    )
    assert entry is not None
    assert entry.task_text_preview == "Напиши пост про Travel Advantage"


def test_parses_task_text_preview_with_embedded_quote():
    """repr() switches to double quotes when the string contains a single
    quote - the parser must handle both quoting styles produced by %r."""
    entry = parse_shadow_line(
        _ok_line(task_text_preview='"Don\'t rewrite this"')
    )
    assert entry is not None
    assert entry.task_text_preview == "Don't rewrite this"


def test_parses_error_status_with_none_fields():
    entry = parse_shadow_line(_error_line())
    assert entry is not None
    assert entry.status == "error"
    assert entry.llm_primary is None
    assert entry.llm_intent is None
    assert entry.llm_safety is None
    assert entry.llm_confidence is None
    assert entry.agreement is None
    assert entry.old_primary == "Safety Layer"


def test_ignores_journalctl_style_prefix():
    line = (
        "Aug 25 12:07:41 vps-host bot[1234]: 2026-08-25 12:07:41,922 INFO "
        "app.orchestration.shadow: " + _ok_line().split("app.orchestration.shadow: ", 1)[1]
    )
    entry = parse_shadow_line(line)
    assert entry is not None
    assert entry.old_primary == "Travel Content Factory"


# --- Игнорирование мусорных строк --------------------------------------------


def test_ignores_unrelated_log_lines():
    assert parse_shadow_line("2026-08-25 12:00:00 INFO app.main: bot started") is None
    assert parse_shadow_line("Traceback (most recent call last):") is None
    assert parse_shadow_line("  File \"app/main.py\", line 42, in run") is None
    assert parse_shadow_line("") is None
    assert parse_shadow_line("random noise without the marker string") is None


def test_ignores_malformed_orchestration_shadow_line():
    """Contains the marker substring but not the expected field shape -
    must not raise, must return None."""
    assert parse_shadow_line("orchestration_shadow something went horribly wrong") is None


def test_parse_shadow_lines_filters_out_noise_from_a_mixed_log():
    lines = [
        "2026-08-25 12:00:00 INFO app.main: bot started",
        _ok_line(workspace_id=1),
        "Traceback (most recent call last):",
        _error_line(workspace_id=2),
        "",
        _invalid_output_line(workspace_id=3),
        "2026-08-25 12:00:05 INFO app.access: allowlist ok",
    ]
    entries = parse_shadow_lines(lines)
    assert len(entries) == 3
    assert [e.workspace_id for e in entries] == [1, 2, 3]


# --- Агрегация: несколько записей --------------------------------------------


def test_total_counts_all_parsed_entries_regardless_of_status():
    entries = parse_shadow_lines([
        _ok_line(), _ok_line(), _error_line(), _invalid_output_line(),
    ])
    stats = compute_stats(entries)
    assert stats.total == 4
    assert stats.status_counts["ok"] == 2
    assert stats.status_counts["error"] == 1
    assert stats.status_counts["invalid_output"] == 1


# --- Agreement-статистика -----------------------------------------------------


def test_agreement_percentage_computed_only_over_status_ok():
    entries = parse_shadow_lines([
        _ok_line(agreement="True"),
        _ok_line(agreement="True"),
        _ok_line(agreement="False"),
        _error_line(),  # agreement=None, must not count toward compared total
        _invalid_output_line(),
    ])
    stats = compute_stats(entries)
    assert stats.agreement_true == 2
    assert stats.agreement_false == 1
    assert stats.agreement_percent == pytest.approx(200 / 3)


def test_agreement_percent_is_none_when_no_ok_entries():
    entries = parse_shadow_lines([_error_line(), _invalid_output_line()])
    stats = compute_stats(entries)
    assert stats.agreement_percent is None
    assert stats.agreement_true == 0
    assert stats.agreement_false == 0


# --- Top disagreements --------------------------------------------------------


def test_top_disagreements_groups_old_to_llm_primary_pairs():
    entries = parse_shadow_lines([
        _ok_line(old_primary="Travel Content Factory", llm_primary="AI Lead Radar", agreement="False"),
        _ok_line(old_primary="Travel Content Factory", llm_primary="AI Lead Radar", agreement="False"),
        _ok_line(old_primary="Travel Content Factory", llm_primary="AI Lead Radar", agreement="False"),
        _ok_line(old_primary="Orchestrator", llm_primary="Safety Layer", agreement="False"),
        _ok_line(old_primary="Travel Content Factory", llm_primary="Travel Content Factory", agreement="True"),
    ])
    stats = compute_stats(entries)
    assert stats.top_disagreements[0] == (
        ("Travel Content Factory", "AI Lead Radar"), 3
    )
    assert (("Orchestrator", "Safety Layer"), 1) in stats.top_disagreements
    # Agreement=True rows must never show up as a disagreement pair.
    assert all(pair != ("Travel Content Factory", "Travel Content Factory")
               for pair, _ in stats.top_disagreements)


def test_top_disagreements_caps_at_ten():
    entries = parse_shadow_lines([
        _ok_line(old_primary=f"Module{i}", llm_primary="Safety Layer", agreement="False")
        for i in range(15)
    ])
    stats = compute_stats(entries)
    assert len(stats.top_disagreements) == 10


# --- Safety mismatch -----------------------------------------------------------


def test_safety_mismatch_old_required_llm_did_not():
    entries = parse_shadow_lines([
        _ok_line(old_safety="обязателен", llm_safety="False"),
        _ok_line(old_safety="рекомендуется", llm_safety="False"),
        _ok_line(old_safety="не требуется", llm_safety="False"),  # not a mismatch
    ])
    stats = compute_stats(entries)
    assert stats.safety_old_required_llm_not == 2
    assert stats.safety_old_not_required_llm_required == 0


def test_safety_mismatch_old_did_not_require_llm_did():
    entries = parse_shadow_lines([
        _ok_line(old_safety="не требуется", llm_safety="True"),
        _ok_line(old_safety="не требуется", llm_safety="True"),
        _ok_line(old_safety="обязателен", llm_safety="True"),  # both agree, not a mismatch
    ])
    stats = compute_stats(entries)
    assert stats.safety_old_not_required_llm_required == 2
    assert stats.safety_old_required_llm_not == 0


def test_safety_mismatch_excludes_status_not_ok():
    entries = parse_shadow_lines([_error_line(), _invalid_output_line()])
    stats = compute_stats(entries)
    assert stats.safety_old_required_llm_not == 0
    assert stats.safety_old_not_required_llm_required == 0


# --- Distribution ---------------------------------------------------------------


def test_old_primary_distribution_counts_all_entries_including_non_ok():
    entries = parse_shadow_lines([
        _ok_line(old_primary="Travel Content Factory"),
        _error_line(),  # old_primary=Safety Layer
        _invalid_output_line(),  # old_primary=AI Travel Assistant
    ])
    stats = compute_stats(entries)
    assert stats.old_primary_distribution["Travel Content Factory"] == 1
    assert stats.old_primary_distribution["Safety Layer"] == 1
    assert stats.old_primary_distribution["AI Travel Assistant"] == 1


def test_llm_primary_and_intent_distribution_only_from_status_ok():
    entries = parse_shadow_lines([
        _ok_line(llm_primary="Travel Content Factory", llm_intent="create_content"),
        _ok_line(llm_primary="AI Lead Radar", llm_intent="feedback_on_previous_result"),
        _error_line(),  # llm_primary/llm_intent are None - must not pollute distribution
    ])
    stats = compute_stats(entries)
    assert stats.llm_primary_distribution == {
        "Travel Content Factory": 1, "AI Lead Radar": 1,
    }
    assert stats.llm_intent_distribution == {
        "create_content": 1, "feedback_on_previous_result": 1,
    }
    assert None not in stats.llm_primary_distribution
    assert None not in stats.llm_intent_distribution


# --- Latency ----------------------------------------------------------------


def test_latency_mean_and_median_only_over_status_ok():
    entries = parse_shadow_lines([
        _ok_line(latency_ms=1000),
        _ok_line(latency_ms=2000),
        _ok_line(latency_ms=3000),
        _error_line(latency_ms=999999),  # must be excluded
    ])
    stats = compute_stats(entries)
    assert stats.latency_mean == 2000.0
    assert stats.latency_median == 2000.0


def test_latency_is_none_when_no_ok_entries():
    entries = parse_shadow_lines([_error_line()])
    stats = compute_stats(entries)
    assert stats.latency_mean is None
    assert stats.latency_median is None


# --- Sanity: ShadowLogEntry.old_safety_required ------------------------------


def test_old_safety_required_property():
    assert ShadowLogEntry(
        workspace_id=1, status="ok", provider="openai", latency_ms=1,
        old_primary="X", old_secondary=(), old_safety="не требуется",
        llm_intent=None, llm_primary=None, llm_secondary=(), llm_safety=None,
        llm_confidence=None, llm_reason_code=None, agreement=None,
    ).old_safety_required is False
    assert ShadowLogEntry(
        workspace_id=1, status="ok", provider="openai", latency_ms=1,
        old_primary="X", old_secondary=(), old_safety="рекомендуется",
        llm_intent=None, llm_primary=None, llm_secondary=(), llm_safety=None,
        llm_confidence=None, llm_reason_code=None, agreement=None,
    ).old_safety_required is True


# --- Potential improvements: категоризация agreement=False -------------------


def test_category_old_router_undetermined_task():
    entries = parse_shadow_lines([
        _ok_line(
            old_primary="Orchestrator", llm_primary="Safety Layer",
            agreement="False", task_text_preview="'Можно ли обещать доход?'",
        ),
        _ok_line(
            old_primary="Orchestrator", llm_primary="Orchestrator",
            agreement="False", task_text_preview="'модель тоже не уверена'",
        ),
        _ok_line(
            old_primary="Travel Content Factory", llm_primary="AI Lead Radar",
            agreement="False", task_text_preview="'старый роутер и так что-то выбрал'",
        ),
    ])
    categories = compute_potential_improvements(entries)
    category = categories[0]
    assert category.name.startswith("Старый роутер не определил задачу")
    assert category.count == 1
    assert category.examples == ("Можно ли обещать доход?",)


def test_category_llm_detected_feedback():
    entries = parse_shadow_lines([
        _ok_line(
            llm_intent="feedback_on_previous_result", agreement="False",
            task_text_preview="'Почему ты предлагаешь этот повод?'",
        ),
        _ok_line(llm_intent="create_content", agreement="False"),
        _ok_line(llm_intent="feedback_on_previous_result", agreement="True"),  # not a disagreement
    ])
    categories = compute_potential_improvements(entries)
    category = categories[1]
    assert category.name == "LLM распознал feedback_on_previous_result"
    assert category.count == 1
    assert category.examples == ("Почему ты предлагаешь этот повод?",)


def test_category_safety_diverged_counts_both_directions():
    entries = parse_shadow_lines([
        _ok_line(old_safety="обязателен", llm_safety="False", agreement="False"),
        _ok_line(old_safety="не требуется", llm_safety="True", agreement="False"),
        _ok_line(old_safety="обязателен", llm_safety="True", agreement="False"),  # agree on safety
    ])
    categories = compute_potential_improvements(entries)
    category = categories[2]
    assert category.name == "LLM разошёлся со старым роутером по safety_required"
    assert category.count == 2


def test_category_dangerous_disagreement_only_old_required_llm_did_not():
    entries = parse_shadow_lines([
        _ok_line(
            old_safety="обязателен", llm_safety="False", agreement="False",
            task_text_preview="'Возьмите предоплату и мы пропадём'",
        ),
        _ok_line(old_safety="не требуется", llm_safety="True", agreement="False"),  # opposite direction
        _ok_line(old_safety="обязателен", llm_safety="True", agreement="False"),  # agree
    ])
    categories = compute_potential_improvements(entries)
    category = categories[3]
    assert category.name.startswith("Потенциально опасные расхождения")
    assert category.count == 1
    assert category.examples == ("Возьмите предоплату и мы пропадём",)


def test_categories_exclude_agreement_true_and_non_ok_status():
    entries = parse_shadow_lines([
        _ok_line(
            old_primary="Orchestrator", llm_primary="Safety Layer", agreement="True",
        ),
        _error_line(),  # old_primary=Safety Layer, but status != ok
        _invalid_output_line(),  # old_primary=AI Travel Assistant, status != ok
    ])
    categories = compute_potential_improvements(entries)
    assert all(category.count == 0 for category in categories)
    assert all(category.examples == () for category in categories)


def test_categories_cap_examples_at_five():
    entries = parse_shadow_lines([
        _ok_line(
            old_primary="Orchestrator", llm_primary="Safety Layer", agreement="False",
            task_text_preview=f"'случай {i}'",
        )
        for i in range(8)
    ])
    categories = compute_potential_improvements(entries)
    category = categories[0]
    assert category.count == 8
    assert len(category.examples) == 5


def test_a_single_entry_can_belong_to_multiple_categories():
    """An Orchestrator-fallback case that ALSO has a dangerous safety
    divergence must be counted in both categories - they are not exclusive."""
    entries = parse_shadow_lines([
        _ok_line(
            old_primary="Orchestrator", llm_primary="Safety Layer",
            old_safety="обязателен", llm_safety="False", agreement="False",
        ),
    ])
    categories = compute_potential_improvements(entries)
    assert categories[0].count == 1  # old undetermined
    assert categories[2].count == 1  # safety diverged
    assert categories[3].count == 1  # dangerous divergence


# --- format_report includes the Potential improvements section --------------


def test_format_report_includes_potential_improvements_section():
    entries = parse_shadow_lines([
        _ok_line(
            old_primary="Orchestrator", llm_primary="Safety Layer", agreement="False",
            task_text_preview="'Можно ли обещать доход?'",
        ),
    ])
    report = format_report(compute_stats(entries))
    assert "=== Potential improvements ===" in report
    assert "Старый роутер не определил задачу" in report
    assert "Можно ли обещать доход?" in report
