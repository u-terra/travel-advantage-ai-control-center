# OpenClaw automation: Travel Lead Radar Daily Review

## Назначение
Внутренний автоматический обзор уже подготовленных сигналов Travel Lead Radar. Это не модуль рассылок и не сборщик новых данных.

## Агент и skill
- Агент OpenClaw: `travel-advantage-orchestrator`.
- Рабочее пространство: `C:\Desktop\travel_advantage_ai_ecosystem`.
- Роль и ограничения: [AGENTS.md](../AGENTS.md).
- Собственный skill: [skills/travel-advantage-orchestrator/SKILL.md](../skills/travel-advantage-orchestrator/SKILL.md).

## Cron Job
- Название: `Travel Lead Radar Daily Review`.
- Расписание: `15 9 * * 1-5`.
- Часовой пояс: `Europe/Samara`.
- Тип: локальная command-задача OpenClaw.
- Команда: `C:\Desktop\travel_lead_radar\.venv\Scripts\python.exe scripts\generate_lead_radar_report.py`.

## Источник и правила
Скрипт читает `C:\Desktop\travel_lead_radar\data\leads.db` в read-only режиме. Для отчёта отбираются записи с `status=new` и `ai_score>=60`. Технические и тестовые записи исключаются.

Скрипт не:
- запускает VK/RSS-сбор или LLM-анализ;
- меняет исходную SQLite-базу;
- пишет потенциальным клиентам;
- публикует контент или создаёт бронирования.

## Результат
После запуска создаётся файл `reports/lead-radar-daily-review.md`. В ручной проверке 01.07.2026 Cron Job завершился со статусом `ok`, код команды - `0`. Из 2 записей с AI Score от 60 обе были исключены как технические/тестовые; итог - 0 сигналов для ручного просмотра. Это корректный результат фильтра, а не ошибка автоматизации.
