# TA Control Center v2 — план миграции

## Статус и границы плана

План основан на локальном состоянии проекта на 05.08.2026 и продуктовой спецификации `docs/product_v2_spec.md`. Формулировки «есть» и «реализовано» ниже относятся только к подтверждённому коду. Формулировки «добавить», «целевой» и «планируется» описывают будущую работу.

MVP сохраняет Python, aiogram и SQLite. Проект не переписывается с нуля, не делится на микросервисы и не получает Redis, очереди, PostgreSQL или Kubernetes.

## 1. Исходное состояние проекта

| Компонент | Реальный файл и точка входа | Текущее назначение | Подтверждённые ограничения |
|---|---|---|---|
| Закрытый Telegram-бот | `app/main.py`: `_async_main()`, `_build_dispatcher()`; `app/__main__.py`: `run()` | Запускает aiogram polling, собирает зависимости и роутеры. | Только Telegram-интерфейс; `MemoryStorage`; продуктового workspace-контекста нет. |
| Allowlist | `app/access.py`: `AllowlistMiddleware`, `parse_allowed_user_ids()`; регистрация в `app/main.py` | Fail-closed доступ к message и callback_query по Telegram ID. | Общий набор ID; ID не сопоставлен отдельному рабочему пространству. `ADMIN_TELEGRAM_ID` не добавляется в allowlist автоматически. |
| Меню и FSM | `app/keyboards.py`: `main_menu()`; `app/handlers/menu.py`: `AwaitTask`, `on_category()`, `on_unsure()` | Показывает меню технических маршрутов и хранит выбранный forced module до следующего сообщения. | Меню ориентировано на модули; состояние находится в памяти и теряется при перезапуске. |
| Keyword routing | `app/routing/router.py`: `route_text()`, `route_for_button()`; `app/routing/keywords.py` | Выбирает `Module`, Safety-уровень и признаки смешанного/неуверенного запроса. | Основан на совпадениях фраз; не ведёт многошаговый продуктовый сценарий и не создаёт структурированный бриф. |
| Карточки маршрутов | `app/cards.py`: `build_card()` | Показывает технический модуль, Safety Layer, следующий шаг и ручное решение. | Описывает маршрут, а не сохранённый источник или материал. |
| Journal | `app/storage.py`: `_SCHEMA`, `Journal`, `JournalEntry`, `init()`, `add()`, `last()` | Хранит последнюю маршрутизированную задачу в SQLite. | Таблица содержит текст задачи, модули, Safety-уровень, статус и заметку; нет workspace, источника, материала или версий. |
| Content Factory | `app/services/content_factory.py`: `ContentFactoryConfig`, `generate_draft_sync()` | Вызывает внутренний HTTP endpoint генерации и возвращает `ContentDraft`. | Один общий контракт с `source_text`, `material_type`, `output_format`, `mode`; результат сам по себе не сохраняется как материал. |
| Safety Layer | `app/routing/safety.py`: `detect_safety_level()`; `app/services/content_factory.py`: `check_text_sync()`; `app/handlers/tasks.py`: `_send_text_check()` | Локально определяет обязательность проверки и вызывает проверку/переработку текста. | Проверка не привязана к ArtifactVersion, Source или PartnerProfile; часть правил задана ключевыми словами. |
| Travel Assistant | `app/routing/modules.py`: `Module.TRAVEL_ASSISTANT`; `app/handlers/tasks.py`: `_draft_request_for()`, `_maybe_send_draft()` | Маршрут клиентского вопроса формирует запрос к Content Factory и возвращает осторожный черновик. | Файла `app/services/travel_assistant.py` нет; отдельного сервисного контракта, профиля клиента и сохранения ответа нет. |
| Read-only Lead Radar | `app/services/lead_radar.py`: `LeadRadarConfig`, `fetch_signals_sync()`, `build_summary()`; `app/handlers/menu.py`: `on_find_signals()`, `on_radar_content_selected()` | Читает локальную базу `leads.db` через SQLite `mode=ro`, фильтрует сигналы и позволяет создать Telegram-пост из content-сигнала. | Не получает и не анализирует произвольные URL; зависит от внешнего `action_recommender.py`; сигнал не хранится в общей модели Source. |
| Partner Packaging | `app/routing/modules.py`: `Module.PARTNER_PACKAGING`; `app/handlers/tasks.py`: `_send_partner_package()` | Формирует текстовую структуру комплекта материалов. | Это ветка обработчика, а не библиотека сохраняемых FAQ/материалов. |
| Web Resources | `app/keyboards.py`: `WEB_RESOURCE_LINKS`, `web_resources_keyboard()`; `app/handlers/menu.py`: `on_web_resources()` | Показывает URL-кнопки Content Factory и AI Travel Assistant. | Ссылки глобальны и зашиты в код; профиль партнёра их не настраивает. |
| OpenClaw-автоматизация | `scripts/generate_lead_radar_report.py`: `main()`; описание в `docs/openclaw_automation.md` | Отдельно создаёт read-only Markdown-обзор сигналов `status=new`, `ai_score>=60`. | Код бота её не запускает. Расписание подтверждено документацией, а не кодом репозитория; автоматизация не является продуктовым хранилищем источников. |

Тестами подтверждены отдельные аспекты доступа, маршрутизации, Safety, Lead Radar и Web Resources в `tests/test_access.py`, `tests/test_access_integration.py`, `tests/test_router.py`, `tests/test_safety.py`, `tests/test_lead_radar.py` и `tests/test_web_resources.py`. Полного сквозного теста сохранения материалов v2 нет, потому что такой функции ещё нет.

## 2. Основные ограничения текущей архитектуры

1. **Интерфейс построен вокруг технических модулей.** `app/keyboards.py:main_menu()` предлагает «Создать контент», «Найти сигналы», «Проверить текст» и «Упаковать материалы», а `app/cards.py:build_card()` выводит `Module`, а не рабочий объект пользователя.
2. **Lead Radar занимает отдельный пункт, но не является анализатором ссылок.** `app/handlers/menu.py:on_find_signals()` вызывает только `app/services/lead_radar.py:fetch_signals_sync()` для готовой SQLite-базы. Получения произвольной страницы нет.
3. **Не все форматы из будущей спецификации реально генерируются отдельными контрактами.** `app/services/content_factory.py:generate_draft_sync()` возвращает единый `ContentDraft`; выбор параметров сосредоточен в `app/handlers/tasks.py:_draft_request_for()`. Отдельных моделей результата для Stories, видео, плана и FAQ нет.
4. **Результат преимущественно живёт в Telegram.** `app/handlers/tasks.py:_maybe_send_draft()` и `app/handlers/menu.py:on_radar_content_selected()` отправляют текст сообщением; в Journal записывается маршрут до/рядом с вызовом, но не содержимое самостоятельного Artifact.
5. **Journal не является библиотекой.** `_SCHEMA` в `app/storage.py` не содержит Source, ContentBrief, Artifact, ArtifactVersion или связей между ними.
6. **Нет полноценного профиля партнёра.** `app/config.py:Settings` содержит системные параметры бота и интеграций, но не бизнес-профиль, аудитории, тон и подтверждённые факты.
7. **Архитектура ориентирована на одного владельца.** Тексты в `app/cards.py`, `app/handlers/start.py` и документации адресованы владельцу; allowlist разрешает несколько ID, но данные не разделяет.
8. **Keyword routing ограничен для составных процессов.** `app/routing/router.py:route_text()` считает совпадения в наборах из `app/routing/keywords.py`; он может обозначить смешанную задачу, но не хранит прогресс «источник → анализ → бриф → версии».
9. **Интеграции и представление частично смешаны.** `app/handlers/menu.py:on_radar_content_selected()` одновременно читает FSM, строит продуктовый prompt, пишет Journal, вызывает Content Factory и форматирует Telegram-ответ. Аналогично `app/handlers/tasks.py` содержит ветвление, бриф, вызов сервисов и представление.

## 3. Принципы миграции

- Не переписывать проект целиком.
- После каждого этапа Telegram-бот остаётся запускаемым.
- Делать небольшие независимые итерации с минимальным diff.
- Старые сценарии временно сохранять как совместимый слой.
- Новые таблицы и репозитории добавлять рядом с Journal; Journal не заменять сразу.
- SQLite-схему расширять только идемпотентными, транзакционными и проверяемыми изменениями; существующие строки не преобразовывать без необходимости.
- Интеграции скрывать за едиными сервисными контрактами, не вызывать их напрямую из новых Telegram-обработчиков.
- Lead Radar не удалять до появления раздела источников и адаптера «сигнал → Source».
- Ручная проверка и ручное внешнее действие сохраняются на всех этапах.
- Каждый этап заканчивать тестами и отдельным коммитом.
- Push выполнять только после проверки и подтверждения владельца.

## 4. Что переиспользуется

| Текущий компонент | Файл | Что сохраняется | Что адаптируется |
|---|---|---|---|
| Сборка приложения | `app/main.py` | Один процесс, aiogram Dispatcher, dependency injection через `dp[...]`. | Добавить инициализацию репозиториев и resolver workspace без изменения polling. |
| Настройки | `app/config.py` | `Settings`, `.env`, безопасный разбор значений. | Добавлять только опциональные настройки с безопасными defaults; бизнес-профиль не хранить в env. |
| Клавиатуры | `app/keyboards.py` | Фабрики Reply/Inline keyboard. | Добавить меню v2 и callback-кнопки; старые константы оставить на период совместимости. |
| Карточки | `app/cards.py` | Подход с отдельными форматтерами. | Сохранить старую `build_card()`; новые карточки Source/Artifact вынести отдельно. |
| Хранилище | `app/storage.py` | Текущий `Journal` и путь общей SQLite. | Сначала добавить инициализацию схемы рядом; позднее разнести модели и репозитории, не ломая API Journal. |
| Меню | `app/handlers/menu.py` | Действующие хендлеры и Lead Radar callbacks. | Постепенно сделать legacy-слоем; новые сценарии направлять в use cases. |
| Обработка задач | `app/handlers/tasks.py` | Проверенные тексты fallback и текущие ветки v1. | Извлечь брифы, генерацию, Safety и сохранение в отдельные use cases. |
| Маршрутизатор | `app/routing/router.py` | `RouteDecision` и fallback свободного текста. | Не использовать как главный координатор v2; позднее обернуть в legacy adapter. |
| Ключевые слова | `app/routing/keywords.py` | Регрессионное поведение v1. | Расширять только при необходимости совместимости, не кодировать в них новые многошаговые процессы. |
| Content Factory | `app/services/content_factory.py` | HTTP-вызовы, timeout, скрытие токена, безопасный `None` при ошибке. | Ввести интерфейс генератора и структурированные запросы/ответы; старые функции оставить адаптером. |
| Lead Radar | `app/services/lead_radar.py` | Read-only доступ и фильтрацию. | Добавить поздний адаптер LeadSignal → Source; убрать кнопку из главного меню только на этапе 3. |
| Travel Assistant | Файл `app/services/travel_assistant.py` отсутствует | Сохранить пользовательскую логику осторожного ответа в `app/handlers/tasks.py`. | На этапе 6 выделить контракт генератора клиентских сообщений; не изображать существующий сервис. |
| Daily Review | `scripts/generate_lead_radar_report.py` | Независимый read-only отчёт. | Не менять в MVP без отдельной необходимости; не делать зависимостью приложения. |
| Тесты | `tests/` | Все существующие регрессионные тесты. | Добавлять отдельные тесты домена, репозиториев, workspace/FSM/handlers и контрактов. |

## 5. Целевая структура каталогов MVP

Структура вводится постепенно, без массового перемещения:

```text
app/
  domain/                 # модели и правила без aiogram/SQLite
  repositories/           # SQLite-доступ к продуктовым сущностям
  services/               # существующие и новые интеграционные адаптеры
  use_cases/              # последовательности продуктовых операций
  presentation/
    telegram/             # новые handlers/keyboards/formatters v2
  handlers/               # временно: обработчики v1
  routing/                # временно: маршрутизация v1
  storage.py              # временно: Journal и точка инициализации
```

- `app/domain/` появляется на этапе 1 для PartnerWorkspace и PartnerProfile. Позднее получает Source, Artifact, планы и value-правила. Текущие `app/routing/modules.py` и `app/storage.py` на первом этапе не перемещаются.
- `app/repositories/` появляется на этапе 1 с компактным SQLite-репозиторием workspace/profile. На этапе 2 добавляются источники и материалы. `Journal` остаётся в `app/storage.py`, пока его API используют старые handlers.
- `app/services/` уже существует. `content_factory.py` и `lead_radar.py` остаются на месте; новые протоколы и URL-fetcher добавляются здесь только на соответствующих этапах.
- `app/use_cases/` появляется на этапе 4, когда возникает первая сквозная операция «сохранить текст → проанализировать → предложить действия». Не нужен в этапе 1 для простого bootstrap.
- `app/presentation/telegram/` появляется на этапе 3 для меню v2 и далее растёт по сценариям. `app/handlers/`, `app/keyboards.py` и `app/cards.py` не перемещаются массово; они остаются legacy-слоем до стабилизации MVP.

## 6. Целевая модель данных

| Сущность | Этап | Минимальные поля | Связь с Journal | Таблица в MVP / что отложить |
|---|---:|---|---|---|
| PartnerWorkspace | 1 | id, name, owner_telegram_id, status, created_at | Новые записи Journal пока не обязаны иметь FK; связь можно добавить позже отдельным nullable полем. | Отдельная таблица нужна. Роли и команды отложить. |
| PartnerProfile | 1 | id, workspace_id, partner_name, project_name, description, style, verified_facts, forbidden_claims, updated_at | Journal продолжает работать независимо. | Отдельная таблица нужна; сложные каталоги настроек можно хранить компактно и нормализовать позднее. |
| Audience | 4 или 6 | id, workspace_id, name, description, needs, objections, tone | Прямой связи нет. | Таблица нужна до полноценной генерации по аудиториям; множественные персоны и аналитику отложить. |
| Source | 2 | id, workspace_id, type, url, original_text, title, publisher, published_at, created_at | При желании Journal note хранит ссылку на операцию, но не является источником. | Отдельная таблица нужна. Метаданные scraping отложить до этапа 5. |
| SourceAnalysis | 4 | id, source_id, summary, key_facts, disputed_claims, relevance, angles, formats, warnings, created_at | Journal может фиксировать факт запуска legacy-способом. | Отдельная таблица нужна; сложную историю повторных анализов можно отложить, если сохраняется последний и GenerationRun. |
| ContentBrief | 6 | id, workspace_id, source_id, audience_id, topic, format, platform, goal, tone, length, cta, constraints | Не заменяет task_text в Journal. | Таблица нужна для воспроизводимой генерации; шаблоны брифов отложить. |
| Artifact | 2 | id, workspace_id, source_id, type, title, status, created_at, updated_at | Journal остаётся журналом маршрута; Artifact — продуктовый объект. | Отдельная таблица нужна. Теги можно отложить. |
| ArtifactVersion | 2 | id, artifact_id, version_number, content, created_by, note, created_at | Связи с Journal не требуется. | Отдельная таблица нужна с уникальностью artifact/version; diff-формат отложить. |
| SafetyCheck | 6 | id, artifact_version_id, risk_level, findings, recommendations, review_status, created_at | Текущий safety_level Journal сохраняется как legacy-сигнал. | Отдельная таблица нужна до статуса «готов»; сложное согласование отложить. |
| ContentPlan | 7 | id, workspace_id, name, period_start, period_end, platforms, goal, status | Не связан напрямую. | Отдельная таблица нужна. Автопубликация отложена за MVP. |
| ContentPlanItem | 7 | id, plan_id, source_id, audience_id, artifact_id, position_or_date, topic, format, thesis, goal, cta, status | Не связан напрямую. | Отдельная таблица нужна; календарные интеграции отложить. |
| GenerationRun | 4 минимально, полноценно 6 | id, workspace_id, operation, input_summary, status, error_code, started_at, finished_at | Дополняет Journal, не заменяет его. | Таблица нужна для диагностики; сырые prompts, секреты и полный provider telemetry не хранить. |

## 7. Этапы реализации

### Этап 0. Спецификация и инвентаризация

- **Цель:** зафиксировать продукт и безопасную последовательность миграции.
- **Пользовательская ценность:** согласованные границы MVP до изменения работающего бота.
- **Изменяемые области:** только `docs/product_v2_spec.md` и `docs/product_v2_migration_plan.md`.
- **Новые файлы:** эти два документа.
- **Не меняется:** весь код, тесты, конфигурация, БД и runtime.
- **Тесты:** не запускаются, так как код не меняется; выполняются Git-проверки документации.
- **Критерий завершения:** оба документа существуют, утверждения сверены с кодом, других изменений нет.
- **Риски:** принять целевую функцию за существующую; снижается явной маркировкой состояния.
- **Коммит:** `docs: define TA Control Center v2 migration plan`.

### Этап 1. Рабочее пространство и профиль партнёра

- **Цель:** заложить multi-tenant фундамент без изменения поведения Telegram.
- **Пользовательская ценность:** безопасная основа для персонального контекста и будущего подключения партнёров.
- **Изменяемые области:** `app/main.py`, `app/config.py`, `app/storage.py` только при необходимости общей инициализации; новый домен и репозиторий.
- **Новые файлы:** ориентировочно `app/domain/workspace.py`, `app/repositories/workspaces.py`, `tests/test_workspace_repository.py`, `tests/test_workspace_isolation.py`.
- **Не меняется:** меню, ответы, routing, Lead Radar, Content Factory и Safety-интеграции.
- **Тесты:** модели, идемпотентная инициализация, существующая база только с Journal, привязка admin, allowlist compatibility, запрет cross-workspace чтения.
- **Критерий завершения:** текущий `ADMIN_TELEGRAM_ID` связан с первым workspace; повторный запуск не дублирует данные; старые тесты проходят.
- **Риски:** путаница allowlist и membership, повреждение БД; снижаются транзакцией, уникальными ограничениями и тестом на копии legacy-базы.
- **Коммит:** `feat: add partner workspace and profile foundation`.

### Этап 2. Хранилище источников и материалов

- **Цель:** добавить Source, Artifact и ArtifactVersion рядом с Journal.
- **Пользовательская ценность:** будущие результаты перестают быть одноразовыми Telegram-сообщениями.
- **Изменяемые области:** `app/domain/`, `app/repositories/`, инициализация схемы; `app/storage.py` только как совместимая точка старта, если нужно.
- **Новые файлы:** `app/domain/content.py`, `app/repositories/sources.py`, `app/repositories/artifacts.py`, профильные тесты.
- **Не меняется:** главное меню и старые handlers; существующий Journal API.
- **Тесты:** сохранение текста и типа источника, материал с первой версией, последовательность версий, статусы, workspace isolation, открытие legacy-базы.
- **Критерий завершения:** репозитории сохраняют и читают Source/Artifact только в заданном workspace без влияния на Journal.
- **Риски:** FK и транзакционные разрывы; создание Artifact и первой версии выполнять одной транзакцией.
- **Коммит:** `feat: add source and artifact storage`.

### Этап 3. Новое меню v2

- **Цель:** перейти к меню задач партнёра.
- **Пользовательская ценность:** понятный вход без названий внутренних модулей.
- **Изменяемые области:** новые Telegram presentation-файлы, `app/handlers/__init__.py`, `app/keyboards.py` или совместимый новый keyboard module.
- **Новые файлы:** ориентировочно `app/presentation/telegram/menu.py`, `app/presentation/telegram/keyboards.py`, тесты меню и callbacks.
- **Не меняется:** старые handlers и сервисы; Journal; Lead Radar логика.
- **Тесты:** наличие восьми пунктов v2, доступность legacy-команд, callback ownership, FSM entry states, регрессия allowlist.
- **Критерий завершения:** v2-меню показывается целевой группе; старые маршруты доступны через совместимый раздел; Lead Radar находится в «Источниках», а не в главном меню.
- **Риски:** конфликт фильтров и callback_data; использовать отдельные префиксы и явный порядок routers.
- **Коммит:** `feat: introduce task-oriented v2 menu`.

### Этап 4. Разбор вставленного текста

- **Цель:** принять текст/заметку, сохранить Source, получить SourceAnalysis и предложить действия.
- **Пользовательская ценность:** первая полноценная функция v2 без сетевых рисков URL.
- **Изменяемые области:** `app/domain/`, `app/repositories/`, `app/use_cases/`, новый Telegram flow; Content Factory только через адаптер анализа.
- **Новые файлы:** модели анализа, `app/use_cases/analyze_text_source.py`, сервисный контракт анализатора, handlers/formatters и тесты.
- **Не меняется:** Lead Radar, URL-fetching, старое меню совместимости.
- **Тесты:** пустой/длинный текст, структурированный анализ, сохранение Source до вызова, failed run, workspace isolation, FSM cancel/retry, mock интеграции.
- **Критерий завершения:** вставленный текст сохраняется и даёт структурированный анализ с предупреждениями и следующими действиями.
- **Риски:** неструктурированный ответ AI и лишние расходы; строгая схема результата, лимит входа и mock-контракт.
- **Коммит:** `feat: analyze and save text sources`.

### Этап 5. Разбор публичного URL

- **Цель:** безопасно получить публичную страницу и передать очищенный текст в сценарий этапа 4.
- **Пользовательская ценность:** ссылка превращается в сохраняемый и пригодный для контента источник.
- **Изменяемые области:** `app/services/`, URL use case, Telegram flow, настройки timeout/limit с defaults.
- **Новые файлы:** `app/services/web_source_fetcher.py`, URL policy, тесты безопасности и извлечения.
- **Не меняется:** браузерная автоматизация не добавляется; Lead Radar остаётся отдельным источником.
- **Тесты:** схема http/https, DNS/IP policy, localhost/private/link-local/metadata endpoints, redirects, timeout, размер, content type, malformed HTML, безопасные ошибки.
- **Критерий завершения:** разрешённый URL сохраняется с извлечённым текстом; запрещённый/слишком большой/недоступный URL отклоняется без сетевого доступа к приватным адресам.
- **Риски:** SSRF, DNS rebinding, redirect в private network, большие ответы; проверять каждый resolved адрес и redirect, читать поток с жёстким лимитом.
- **Коммит:** `feat: add safe public URL source analysis`.

### Этап 6. Генераторы материалов

- **Цель:** отделить бриф, генерацию, Safety, сохранение и Telegram-показ.
- **Пользовательская ценность:** из одного источника создаются пост, видео-сценарий, Stories, клиентское сообщение или ответ на возражение.
- **Изменяемые области:** `app/services/content_factory.py` через совместимый адаптер, `app/use_cases/`, доменные модели, новые handlers.
- **Новые файлы:** `app/domain/briefs.py`, генераторный контракт, use cases по форматам, Safety orchestration и contract tests.
- **Не меняется:** старые `generate_draft_sync()`/`check_text_sync()` до перевода legacy handlers; ручное решение пользователя.
- **Тесты:** форматные запросы/ответы, mock timeout/error, обязательный Safety, ArtifactVersion, атомарное сохранение, профиль и аудитория, регрессия v1.
- **Критерий завершения:** минимум пять форматов имеют явные контракты и сохраняются как Artifact с версией и нужной SafetyCheck.
- **Риски:** расхождение prompt-логики и provider dependency; один интерфейс, schema validation и fallback без потери данных.
- **Коммит:** `feat: generate and save v2 content artifacts`.

### Этап 7. Контент-план

- **Цель:** добавить ContentPlan и ContentPlanItem с созданием Artifact из пункта.
- **Пользовательская ценность:** темы организованы в последовательную работу, а не остаются списком идей.
- **Изменяемые области:** domain/repositories/use_cases/Telegram presentation.
- **Новые файлы:** модели и репозиторий плана, use cases создания/просмотра/генерации, тесты.
- **Не меняется:** автопубликация и внешние календари отсутствуют.
- **Тесты:** период, порядок, статусы, source/audience links, создание Artifact, workspace isolation.
- **Критерий завершения:** план сохраняется и показывается; из каждого пункта можно вручную запустить создание материала.
- **Риски:** сложная календарная логика; в MVP поддержать дату или простой порядок без scheduler.
- **Коммит:** `feat: add content plans and plan items`.

### Этап 8. Библиотека и версии

- **Цель:** дать список, фильтры, просмотр источника, новую версию, статусы и архив.
- **Пользовательская ценность:** материалы можно найти, повторно использовать и безопасно дорабатывать.
- **Изменяемые области:** artifact repository/use cases/Telegram presentation.
- **Новые файлы:** library queries, pagination/filters, version/status handlers и тесты.
- **Не меняется:** физическое удаление и автоматическая публикация не нужны.
- **Тесты:** workspace-scoped list, type/topic/date filters, pagination, immutable old version, status transitions, archive.
- **Критерий завершения:** пользователь видит только свою библиотеку и создаёт новую версию без перезаписи старой.
- **Риски:** длинная Telegram-навигация и утечка через callback ID; пагинация и workspace-scoped lookup на каждом callback.
- **Коммит:** `feat: add artifact library and version workflow`.

### Этап 9. Первый внешний партнёр

- **Цель:** безопасно подключить первый отдельный workspace.
- **Пользовательская ценность:** продукт подтверждает пригодность не только для текущего владельца.
- **Изменяемые области:** onboarding, профиль, membership/bootstrap, минимальные brand/audience настройки, инструкции.
- **Новые файлы:** onboarding use case/handlers, сквозные isolation tests, операторская инструкция.
- **Не меняется:** оплата, CRM, бронирование, автопубликация и массовый onboarding.
- **Тесты:** два Telegram ID/два workspace, попытки подмены IDs, профили, источники, библиотека, планы, генерации, callback isolation.
- **Критерий завершения:** партнёр проходит onboarding и не может получить данные владельца ни одним поддерживаемым путём.
- **Риски:** ошибочная привязка ID и неполный профиль; явная ручная активация, уникальные membership и audit checks.
- **Коммит:** `feat: onboard first isolated partner workspace`.

## 8. Первая техническая итерация после документации

Самый маленький безопасный шаг — добавить только фундамент PartnerWorkspace и PartnerProfile, не подключая их к текстам Telegram.

Предполагаемый минимальный набор файлов:

- новый `app/domain/workspace.py` — неизменяемые модели PartnerWorkspace и PartnerProfile;
- новый `app/repositories/workspaces.py` — идемпотентная схема, bootstrap и workspace-scoped чтение;
- `app/storage.py` — только если нужен единый вызов инициализации рядом с `Journal.init()`, без изменения таблицы `journal`;
- `app/main.py` — bootstrap первого workspace после `Journal.init()`, до polling;
- `app/config.py` — желательно без новых обязательных env; использовать уже проверенный `admin_telegram_id`;
- новые `tests/test_workspace_repository.py` и `tests/test_workspace_isolation.py`;
- при необходимости минимальная регрессия в `tests/test_access_integration.py`.

Правила итерации:

1. `ADMIN_TELEGRAM_ID` автоматически и идемпотентно становится владельцем первого workspace.
2. Allowlist остаётся отдельным входным guard: bootstrap admin не должен автоматически ослаблять fail-closed доступ.
3. Меню, ответы, FSM, Lead Radar и интеграции не меняются.
4. Новые таблицы создаются через `CREATE TABLE IF NOT EXISTS` внутри транзакции; таблица Journal и существующие строки не изменяются.
5. Сбои bootstrap должны быть видны при старте и не оставлять частично созданные данные.
6. Итерация завершается всеми тестами и одним коммитом `feat: add partner workspace and profile foundation`.

## 9. Матрица миграции компонентов

| Текущий компонент | Текущее назначение | Решение v2 | Этап | Риск |
|---|---|---|---:|---|
| Allowlist | Fail-closed вход по ID | Сохранить; дополнить membership/workspace resolution | 1 | Разрешённый ID может получить чужой workspace |
| FSM | Краткое состояние меню в памяти | Сохранить для диалогов, не хранить продуктовые данные | 3–8 | Разрастание состояний и потеря при restart |
| Journal | Маршруты задач | Сохранить рядом с новыми сущностями, заменить позднее только по решению | 1–8 | Попытка использовать Journal как библиотеку |
| Keyword routing | Выбор Module по словам | Оставить legacy/fallback | 3–6 | Ошибочная классификация составного запроса |
| Карточки маршрутов | Техническое объяснение маршрута | Legacy; добавить карточки Source/Artifact | 3–4 | Смешение технического и продуктового языка |
| Content Factory | Генерация и text check по HTTP | Обернуть единым контрактом | 4–6 | Недоступность/изменение API |
| Safety Layer | Keyword risk + remote check/rewrite | Сохранить, привязать к версии и профилю | 6 | Рискованный текст получит статус «готов» без check |
| Travel Assistant | Маршрут в Content Factory | Выделить use case клиентского сообщения | 6 | Ошибочно считать отдельным существующим сервисом |
| Lead Radar | Read-only список сигналов | Дополнительный адаптер Source, не главное меню | 3–6 | Сломать v1 до готовности Sources |
| Partner Packaging | Текстовая структура | Разложить на Artifact formats/FAQ | 6 | Дублирование генераторов |
| OpenClaw | Отдельный Markdown Daily Review | Оставить необязательным вне ядра | 0+ | Документацию принять за runtime-интеграцию |
| Web Resources | Глобальные URL-кнопки | Перенести в помощь, позже настраивать профилем | 3/9 | Жёстко заданные чужие ссылки |
| Главное меню | Кнопки модулей v1 | Ввести меню задач v2 с legacy-разделом | 3 | Конфликт handlers/filters |
| SQLite | Journal и read-only внешняя Radar DB | Основная БД MVP с новыми таблицами | 1–8 | Повреждение и смешивание tenant data |
| Тесты | Частичные регрессии v1 | Сохранить и расширять по этапам | Все | Ложная уверенность без isolation/e2e tests |

## 10. Стратегия совместимости

- **Меню:** новое меню вводится отдельной фабрикой и router. Старые кнопки временно доступны через «Помощь/Старые инструменты» или команды; их константы и handlers не удаляются на этапе 3.
- **Journal:** `Journal.init()`, `add()` и `last()` сохраняют сигнатуры. Новые таблицы не требуют немедленного `workspace_id` в `journal`; связь добавляется позднее только nullable и после теста миграции.
- **Существующая база:** инициализация открывает её обычным способом и создаёт только отсутствующие таблицы. Перед реальной миграцией проверяется копия существующей БД; транзакция откатывается целиком при ошибке.
- **Handlers:** новые routers используют уникальные state/callback prefixes. Порядок включения и фильтры исключают перехват старых команд; старые зависимости `journal`, `content_factory_config`, `lead_radar_config` продолжают регистрироваться в `app/main.py`.
- **Переменные окружения:** `BOT_TOKEN`, `ADMIN_TELEGRAM_ID`, `TELEGRAM_ALLOWED_USER_IDS`, `JOURNAL_DB_PATH`, Content Factory и Lead Radar settings сохраняются. Новые настройки опциональны и имеют безопасные ограничения по умолчанию.
- **Отключение функции:** новый handler сначала проверяет доступность репозитория/сервиса; при ошибке возвращает нейтральное сообщение и предлагает старый безопасный путь. Ошибка анализа URL или генерации не останавливает Dispatcher и не отключает старые функции.
- **Feature rollout:** до стабильности меню v2 может включаться для первого workspace через запись в БД или безопасный необязательный flag; отсутствие настройки означает старое поведение.

## 11. Стратегия тестирования

- **Этап 1:** unit-тесты доменных моделей; SQLite repository; bootstrap admin; migration existing Journal-only DB; workspace isolation; allowlist regressions.
- **Этап 2:** Source/Artifact repositories; транзакция первой версии; статусы; version uniqueness; isolation.
- **Этап 3:** keyboard/menu, FSM entry/cancel, router precedence, callbacks и старые handlers.
- **Этап 4:** анализ текста через mock contract, валидация структурированного ответа, GenerationRun, FSM retry и сохранение SourceAnalysis.
- **Этап 5:** URL policy и SSRF: localhost, loopback, private/link-local, IPv6, redirects, DNS results; timeouts, size/content-type limits и parser errors.
- **Этап 6:** интеграционные контракты Content Factory с mock; форматы; ContentBrief; SafetyCheck; сохранение ArtifactVersion; регрессия `test_router.py` и `test_safety.py`.
- **Этап 7:** ContentPlan/Item repositories и use cases, создание материала из пункта, isolation.
- **Этап 8:** версии, фильтры, pagination, архивирование, callback ownership.
- **Этап 9:** сквозные сценарии двух workspace и отрицательные попытки доступа ко всем сущностям.

На каждом этапе запускается весь текущий набор `pytest`, а не только новые тесты. Живые вызовы Content Factory, Lead Radar и публичных URL не должны быть обязательной частью автоматических тестов: их контракты проверяются mock/fake, а один ограниченный живой тест выполняется лишь при необходимости и с ручным разрешением.

## 12. Порядок проверки каждой итерации

1. `git status --short`.
2. Анализ текущих файлов и пересекающихся пользовательских изменений.
3. Реализация минимального diff.
4. Полный `pytest`.
5. `git diff --check`.
6. Ручной просмотр `git diff`.
7. Один живой тест только при необходимости и в безопасном контуре.
8. Один отдельный commit для завершённой итерации.
9. Push только после подтверждения владельца.

## 13. Риски миграции

| Риск | Снижение риска |
|---|---|
| Повреждение существующей SQLite-базы | Backup перед production rollout, тест на копии, идемпотентная схема, транзакции, отсутствие разрушительных ALTER на ранних этапах. |
| Смешивание данных партнёров | Обязательный `workspace_id`, repository methods всегда принимают workspace, составные ограничения, отрицательные тесты подмены ID. |
| Слишком большая FSM | В FSM хранить только шаг и временные идентификаторы; Source, brief и Artifact сохранять в SQLite; короткие сценарии и cancel. |
| Усложнение Telegram-навигации | Плоское главное меню, ограниченная глубина, кнопка назад, пагинация, пользовательские названия вместо модулей. |
| Дублирование логики между handlers | Use cases для операций; handlers отвечают только за ввод, вызов и форматирование. |
| Зависимость от Content Factory | Стабильный интерфейс, адаптер существующих функций, timeout, mock tests, безопасный fallback и сохранённый input. |
| Недоступность внешнего URL | Ограниченный timeout, понятная ошибка, возможность вставить текст вручную, отсутствие потери Source draft. |
| SSRF | Только http/https, проверка hostname и всех resolved IP, запрет private/loopback/link-local/reserved, повторная проверка redirect, без browser automation. |
| Слишком длинные страницы | Проверять Content-Length, потоковый жёсткий byte limit, допустимые content types, лимит очищенного текста. |
| Неподтверждённые факты | Сохранять источник, выделять disputed claims, обязательная SafetyCheck, нейтральные формулировки и ручной статус. |
| Рост API-расходов | Лимиты входа/форматов, явное подтверждение генерации, повторное использование SourceAnalysis, GenerationRun без автоматических массовых запусков. |
| Преждевременная сложность | Один процесс, одна SQLite, минимум каталогов, без очередей/микросервисов, добавление сущностей только на нужном этапе. |

## 14. Критерии завершения миграции MVP

1. Старый бот продолжает запускаться, а его регрессионные тесты проходят.
2. Для каждого разрешённого пользователя однозначно определяется workspace.
3. Репозитории и handlers не позволяют читать данные другого workspace.
4. Вставленный текст и разрешённый публичный URL разбираются с безопасной обработкой ошибок.
5. Source и SourceAnalysis сохраняются в текущем workspace.
6. Из одного Source можно создать разные типы материалов.
7. Artifact и первая/последующие ArtifactVersion сохраняются без перезаписи истории.
8. ContentPlan сохраняется, показывается и создаёт Artifact из ContentPlanItem.
9. Библиотека показывает только материалы текущего workspace и поддерживает согласованные фильтры и статусы.
10. Рискованные версии проходят обязательный Safety Layer до статуса «готов».
11. Lead Radar доступен только как дополнительный источник и не занимает главное меню.
12. Автоматические публикации, рассылки, бронирование и платежи отсутствуют.
13. Все внешние действия остаются под ручным контролем человека.
14. Все старые и новые автоматические тесты проходят.
