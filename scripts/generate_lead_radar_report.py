from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(r"C:\Desktop\travel_lead_radar\data\leads.db")
REPORT_PATH = Path(__file__).resolve().parents[1] / "reports" / "lead-radar-daily-review.md"
DISPLAY_LIMIT = 20

TECHNICAL_PATTERN = re.compile(
    r"(?i)(?:"
    r"\b(?:test|dummy|mock|localhost)\b|"
    r"127\.0\.0\.1|"
    r"example\.com|"
    r"тестов\w*|"
    r"техническ\w*"
    r")"
)


def clean(value: object | None) -> str:
    return str(value or "").strip()


def short(value: object | None, limit: int = 320) -> str:
    text = clean(value).replace("\r", " ").replace("\n", " ")
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def is_technical(row: sqlite3.Row) -> bool:
    fields = (
        row["source_name"], row["source_url"], row["item_url"],
        row["item_title"], row["item_summary"],
    )
    return bool(TECHNICAL_PATTERN.search(" ".join(clean(value) for value in fields)))


def format_score(value: object | None) -> str:
    try:
        score = float(value)
        return f"{score:.0f}" if score.is_integer() else f"{score:.1f}"
    except (TypeError, ValueError):
        return "—"


def main() -> None:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"База Lead Radar не найдена: {DB_PATH}")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)

    query = """
        SELECT id, created_at, source_type, source_name, source_url,
               item_url, item_title, item_summary, published_at, status,
               ai_score, ai_category, ai_reason
        FROM lead_signals
        WHERE LOWER(COALESCE(status, '')) = 'new'
          AND COALESCE(ai_score, 0) >= 60
        ORDER BY COALESCE(ai_score, 0) DESC, created_at DESC
        LIMIT 100
    """

    with sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query).fetchall()

    qualified = [row for row in rows if not is_technical(row)]
    excluded = len(rows) - len(qualified)
    shown = qualified[:DISPLAY_LIMIT]
    now = datetime.now().strftime("%d.%m.%Y %H:%M")

    lines = [
        "# Travel Lead Radar - Daily Review",
        "",
        f"Сформировано: {now} (локальное время компьютера)",
        "Режим: локальная read-only проверка базы.",
        "Автоматические сообщения, публикации, VK/RSS-сбор и LLM-анализ не выполнялись.",
        "",
        "## Критерии отбора",
        "- Статус: `new`.",
        "- AI Score: от `60`.",
        "- Технические и тестовые записи исключаются.",
        "",
        "## Итог",
        f"- Найдено записей со статусом `new` и AI Score от 60: **{len(rows)}**.",
        f"- Исключено технических/тестовых записей: **{excluded}**.",
        f"- Сигналов для ручного просмотра: **{len(qualified)}**.",
        f"- В отчёте показано: **{len(shown)}** наиболее сильных сигналов.",
        "- Источник: `leads.db`, таблица `lead_signals`.",
        "",
        "## Сигналы для ручного просмотра",
    ]

    if not shown:
        lines.extend([
            "Подходящих новых сигналов не найдено.",
            "",
            "Это нормальный результат: исходные данные не изменялись, автоматические действия не выполнялись.",
        ])
    else:
        for number, row in enumerate(shown, start=1):
            source_name = clean(row["source_name"]) or clean(row["source_type"]) or "Не указан"
            title = clean(row["item_title"]) or "Без заголовка"
            url = clean(row["item_url"]) or clean(row["source_url"]) or "не указан"
            published_at = clean(row["published_at"]) or clean(row["created_at"]) or "не указано"
            category = clean(row["ai_category"]) or "не указана"
            reason = short(row["ai_reason"]) or "не указана"
            summary = short(row["item_summary"]) or "нет краткого описания"
            lines.extend([
                f"### {number}. {title}",
                f"- ID: `{row['id']}`",
                f"- AI Score: **{format_score(row['ai_score'])}**",
                f"- Категория: {category}",
                f"- Источник: {source_name}",
                f"- Дата: {published_at}",
                f"- Ссылка: {url}",
                f"- Кратко: {summary}",
                f"- Причина скоринга: {reason}",
                "",
            ])

    lines.extend([
        "## Ручной следующий шаг",
        "Владелец вручную проверяет контекст каждого сигнала и принимает решение о дальнейших действиях.",
        "Система не пишет потенциальным клиентам, не меняет записи базы и не запускает новые источники данных.",
        "",
    ])

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"REPORT_CREATED={REPORT_PATH}")
    print(f"CANDIDATES_SCORE_60_PLUS={len(rows)}")
    print(f"QUALIFIED_SIGNALS={len(qualified)}")
    print(f"DISPLAYED_IN_REPORT={len(shown)}")
    print("NO_REPLY")


if __name__ == "__main__":
    main()

