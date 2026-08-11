# Pilot source review

Партнёр открывает «Источники», выбирает «Предложить источник» и отправляет
публичный URL или Telegram username. До решения администратора источник не
подключён и не разрешён для сбора.

## Проверка администратором

```text
python -m app.admin list-source-requests
python -m app.admin show-source-request --request-id 42
python -m app.admin approve-source-request --request-id 42 --workspace-id 7
python -m app.admin reject-source-request --request-id 42 --workspace-id 7 --reason "Not supported"
```

Перед approve вручную проверить URL, workspace и отправителя. Одобрять только
источник, который фактически поддерживается текущей collector-инфраструктурой:
Control Center сам наличие сборщика не подтверждает. После approve выполнить
smoke check generated source projection и следующего штатного цикла Radar.

Для нестандартного пути SQLite используйте глобальный `--db-path`; для
generated projection — `--source-registry-path`. Команды не требуют и не
выводят токены, карточные данные или provider credentials.
