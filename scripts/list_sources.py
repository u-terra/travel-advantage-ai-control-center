"""Read-only просмотр реестра источников.

Ничего не собирает, никуда не ходит по сети и ничего не меняет: просто
печатает, какие источники активны сейчас и какие отключены.

Читается активный реестр: рабочий файл, если он уже развёрнут, иначе
стартовый набор из `config/sources.json`. Скрипт рабочий файл не создаёт —
это делает приложение при запуске. Управлять источниками можно правкой
рабочего файла или через раздел «Источники» в боте.

Запуск:
    python -m scripts.list_sources
    python scripts/list_sources.py --platform telegram
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

# Тот же .env, что читает приложение: иначе скрипт показывал бы реестр
# по пути по умолчанию, а бот работал бы с другим файлом.
load_dotenv()

from app.services.source_registry import (  # noqa: E402
    SourceRegistryError,
    active_registry_path,
    collection_targets,
    load_registry,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Реестр наблюдаемых источников")
    parser.add_argument("--platform", default=None, help="telegram / vk / rss / web")
    args = parser.parse_args()

    try:
        registry = load_registry()
    except SourceRegistryError as exc:
        print(f"ОШИБКА: {exc}")
        return 1

    active = collection_targets(platform=args.platform, registry=registry)

    print(f"Файл реестра: {active_registry_path()}")
    print(f"Всего источников в реестре: {len(registry)}")
    print(f"Активных для сбора: {len(active)}")
    print("")
    for source in active:
        print(
            f"[{source.platform}] {source.name} — {source.target}\n"
            f"    id={source.id} type={source.source_type} "
            f"purpose={source.purpose} priority={source.priority}"
        )

    disabled = registry.disabled()
    if disabled:
        print("")
        print("Отключены (в сборе не участвуют):")
        for source in disabled:
            print(f"    {source.id} — {source.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
