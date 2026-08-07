# Travel Advantage AI Control Center

Закрытый Telegram-бот-диспетчер для владельца Travel Advantage AI Ecosystem.

Бот не выполняет работу за модули экосистемы. Он только определяет маршрут:
какой модуль использовать, нужен ли Safety Layer и какой следующий шаг.
Никаких автоматических сообщений людям, публикаций, бронирований и обещаний дохода.

## Требования

- Windows 11
- Python 3.12
- PowerShell
- Telegram-бот (токен от @BotFather)
- Telegram ID владельца

## Установка

```powershell
cd C:\Desktop\travel-advantage-ai-control-center

py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

## Настройка

1. Скопируйте `.env.example` в `.env`:

   ```powershell
   Copy-Item .env.example .env
   ```

2. Откройте `.env` и заполните:
   - `BOT_TOKEN` — токен бота;
   - `ADMIN_TELEGRAM_ID` — числовой Telegram ID Владимира;
   - `TELEGRAM_ALLOWED_USER_IDS` — список разрешённых Telegram ID через запятую
     (allowlist) — единственный источник доступа к панели. Политика fail-closed:
     пустое/отсутствующее/некорректное значение закрывает доступ всем.

## Запуск

```powershell
.\.venv\Scripts\Activate.ps1
python -m app
```

Доступ открыт только ID из allowlist `TELEGRAM_ALLOWED_USER_IDS` (fail-closed:
без корректного значения доступа нет ни у кого). Проверка централизована в
outer-middleware и охватывает команды, сообщения и callback-кнопки. Посторонние
получают единственный ответ: «Доступ к панели управления ограничен.»

## Минимальная проверка

```powershell
.\scripts\check.ps1
```

Что проверяется:
- байт-компиляция всех модулей через `py_compile`;
- тесты маршрутизации и Safety Layer через `pytest`.

## Главное меню бота

- 📝 Создать контент
- 💬 Вопрос клиента
- 📡 Найти сигналы интереса
- 🛡 Проверить текст
- 📦 Упаковать материалы
- 🧭 Не знаю, куда идти
- 📋 Последняя задача
- ℹ️ Как это работает

Можно также написать задачу обычным текстом. Бот разложит смешанные задачи на части,
отметит, нужен ли Safety Layer, и предложит ручной следующий шаг.

## Реестр источников

Наблюдаемые источники (Telegram-каналы, VK, RSS, веб-ресурсы) описаны данными,
а не кодом. Единственное место — `config/sources.json`.

- Добавить новый канал = добавить запись в этот файл. Python-код менять не нужно.
- Источник с `"enabled": false` в сборе не участвует.
- Код получает источники через `app.services.source_registry.collection_targets()`.
- Посмотреть текущий состав: `.\.venv\Scripts\python.exe scripts\list_sources.py`.

Важно: запись в реестре означает **намерение** наблюдать за источником, а не
факт мониторинга. Кода сбора в этом репозитории нет — Control Center читает
готовые сигналы Travel Lead Radar. Интеграция реестра со сбором — отдельный
следующий этап, поэтому добавленные Telegram-каналы пока физически не читаются.

Источник — это не база знаний и не источник гарантированной истины. Публикации
не копируются как готовый контент: источники используются для поиска инфоповодов,
тем, рыночных сигналов, аргументов, возражений, историй и идей, на основе которых
создаётся новый материал с ручной проверкой.

Подробности: [docs/sources_registry.md](docs/sources_registry.md).

## OpenClaw: агент и автоматический обзор Lead Radar

В проект добавлен отдельный агент OpenClaw `travel-advantage-orchestrator`.

- Роль и ограничения агента описаны в `AGENTS.md`.
- Собственный workspace-skill находится в `skills/travel-advantage-orchestrator/SKILL.md`.
- Источник данных — локальная SQLite-база Travel Lead Radar в read-only режиме.
- Настроен Cron Job `Travel Lead Radar Daily Review`: по будням в 09:15, часовой пояс `Europe/Samara`.
- Задача запускает локальный Python-скрипт `scripts/generate_lead_radar_report.py` и формирует внутренний отчёт `reports/lead-radar-daily-review.md`.
- Автоматизация не запускает новый сбор данных, не вызывает LLM-анализ, не меняет SQLite-базу и не отправляет сообщения потенциальным клиентам.

Подробности: [docs/openclaw_automation.md](docs/openclaw_automation.md).
