# Provisioning партнёра для платного пилота

Команды работают с SQLite из `JOURNAL_DB_PATH`. При необходимости передайте
`--db-path`. Не редактируйте production SQLite вручную.

## Provision

1. Подготовьте UTF-8 JSON с полями существующего Business Profile:
   `business_name`, `business_type`, `short_description`, `context`.
2. Выполните:

   ```text
   python -m app.admin --db-path <db> provision-partner --telegram-user-id <id> --workspace-name "<name>" --workspace-slug <slug> --profile-file <profile.json>
   ```

3. Проверьте состояние:

   ```text
   python -m app.admin --db-path <db> show-partner --telegram-user-id <id>
   ```

4. Добавьте ID в `TELEGRAM_ALLOWED_USER_IDS` и перезапустите сервис.
5. Проверьте `/start` и создайте тестовый персонализированный пост.

Если CLI показывает `profile_status: incomplete`, дополните профиль до выдачи
платного доступа либо явно примите ограниченную персонализацию.

## Deactivate

1. Выполните `deactivate-partner` и затем `show-partner`.
2. Уберите ID из `TELEGRAM_ALLOWED_USER_IDS`.
3. Перезапустите сервис. Workspace и tenant data сохраняются.

## Reactivate

1. Выполните `reactivate-partner` и затем `show-partner`.
2. Верните ID в `TELEGRAM_ALLOWED_USER_IDS`.
3. Перезапустите сервис и выполните smoke test.

CLI не редактирует `.env`, не обращается к Telegram API и не загружает
BOT_TOKEN или credentials LLM provider.
