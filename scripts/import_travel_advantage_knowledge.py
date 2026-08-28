from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.repositories.knowledge_repository import DEFAULT_KNOWLEDGE_DB_PATH, KnowledgeRepository
from app.services.knowledge_import import import_dataset, load_and_validate_dataset


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate or import a Travel Advantage knowledge dataset")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_KNOWLEDGE_DB_PATH)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    if args.validate_only:
        load_and_validate_dataset(args.dataset)
        print(f"Valid dataset: {args.dataset}")
        return 0
    asyncio.run(import_dataset(args.dataset, KnowledgeRepository(args.db_path)))
    print(f"Imported {args.dataset} into {args.db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
