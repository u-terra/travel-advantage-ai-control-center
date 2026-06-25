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
   - `ADMIN_TELEGRAM_ID` — числовой Telegram ID Владимира.

## Запуск

```powershell
.\.venv\Scripts\Activate.ps1
python -m app
```

Доступ открыт только владельцу. Остальные получают единственный ответ:
«Этот бот предназначен для внутренней работы владельца системы.»

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
