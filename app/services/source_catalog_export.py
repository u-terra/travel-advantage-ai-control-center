from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

from app.services.source_registry import SCHEMA_VERSION, SourceRegistryError, parse_registry


def export_radar_projection(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    """Atomically replace the generated schema-v2 Lead Radar projection."""
    sources: list[dict[str, Any]] = []
    for row in rows:
        record: dict[str, Any] = {
            "id": row["id"],
            "name": row["name"],
            "platform": row["platform"],
            "source_type": row["source_type"],
            "purpose": row["purpose"],
            "enabled": bool(row["radar_enabled"]),
            "priority": row["priority"],
            "notes": row["notes"],
            "collector": json.loads(row["collector_json"]),
        }
        if row["url"]:
            record["url"] = row["url"]
        if row["username"]:
            record["username"] = row["username"]
        sources.append(record)

    payload = {"schema_version": SCHEMA_VERSION, "sources": sources}
    parse_registry(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise SourceRegistryError(
            f"не удалось записать проекцию источников: {path}"
        ) from exc
