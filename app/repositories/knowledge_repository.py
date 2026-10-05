from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

import aiosqlite

from app.domain.knowledge import (
    KnowledgeExample,
    KnowledgeFact,
    KnowledgeItem,
    KnowledgeSource,
)


DEFAULT_KNOWLEDGE_DB_PATH = Path("data/knowledge.sqlite3")

_SEARCH_TOKEN_RE = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)
_SEARCH_STOP_WORDS = {
    "а", "без", "в", "для", "и", "из", "как", "какие", "какой", "ли", "на",
    "о", "об", "от", "по", "подробно", "про", "расскажи", "с", "со", "такое",
    "что", "чем", "это", "the", "a", "an", "about", "what", "is", "можно", "ли",
    "мне", "меня", "мой", "моя", "мы", "я", "куда", "если", "сколько", "сейчас",
    "есть", "будет", "буду", "точно", "весь", "все", "одно", "же", "делать",
}
_SEARCH_TOKEN_ALIASES = {
    "сильвер": "silver", "силвер": "silver", "silvr": "silver",
    "руби": "ruby", "rubi": "ruby", "элит": "elite", "турбо": "turbo",
    "тревел": "travel", "travle": "travel", "кредиты": "credits", "кредит": "credits",
    "поинт": "points", "поинты": "points", "поинтов": "points", "лоалти": "loyalty",
    "loyality": "loyalty", "credtis": "credits", "лайф": "life",
    "экспириенс": "experience", "экспириенсы": "experiences", "мвр": "mwr",
    "академия": "academy", "академии": "academy", "бинар": "binary",
    "бинара": "binary", "криптой": "crypto", "крипта": "crypto",
    "криптовалютой": "crypto", "гарантия": "guarantee", "garantee": "guarantee",
    "бронь": "booking", "брони": "booking", "бронирование": "booking",
    "advantge": "advantage", "advatage": "advantage", "guset": "guest", "lp": "points",
    # Live prod bug: "членство" had no alias at all, so it could never match
    # the English "membership" tag on ta.membership/ta.membership.cancellation
    # via the free-text search fallback (search_text) - only the hand-curated
    # _retrieval_policy() rules in app/services/knowledge_service.py ever
    # found these items. "клуб" maps to the same existing "membership" tag
    # (no separate "club" term exists in the catalogue); "подписка" is the
    # common everyday word for the same concept in this domain.
    "членство": "membership", "членский": "membership",
    "клуб": "membership", "подписка": "membership",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stable_key TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_reference TEXT NOT NULL,
    version TEXT,
    effective_date TEXT,
    verification_status TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stable_key TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    source_ref TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES knowledge_sources(id)
);
CREATE TABLE IF NOT EXISTS knowledge_item_tags (
    item_id INTEGER NOT NULL,
    tag TEXT NOT NULL,
    PRIMARY KEY (item_id, tag),
    FOREIGN KEY (item_id) REFERENCES knowledge_items(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS knowledge_item_relations (
    from_item_id INTEGER NOT NULL,
    relation_type TEXT NOT NULL,
    to_item_id INTEGER NOT NULL,
    PRIMARY KEY (from_item_id, relation_type, to_item_id),
    FOREIGN KEY (from_item_id) REFERENCES knowledge_items(id) ON DELETE CASCADE,
    FOREIGN KEY (to_item_id) REFERENCES knowledge_items(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS knowledge_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stable_key TEXT NOT NULL UNIQUE,
    item_id INTEGER NOT NULL,
    source_id INTEGER NOT NULL,
    fact_type TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    qualifier_key TEXT,
    qualifier_value TEXT,
    value_number TEXT,
    value_text TEXT,
    range_min TEXT,
    range_max TEXT,
    unit TEXT,
    currency TEXT,
    period TEXT,
    condition_text TEXT,
    source_ref TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (item_id) REFERENCES knowledge_items(id),
    FOREIGN KEY (source_id) REFERENCES knowledge_sources(id),
    CHECK ((value_number IS NOT NULL) + (value_text IS NOT NULL) +
           (range_min IS NOT NULL OR range_max IS NOT NULL) = 1),
    CHECK ((range_min IS NULL AND range_max IS NULL) OR
           (range_min IS NOT NULL AND range_max IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS knowledge_examples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stable_key TEXT NOT NULL UNIQUE,
    item_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    scenario TEXT NOT NULL,
    explanation TEXT NOT NULL,
    result_number TEXT,
    currency TEXT,
    source_id INTEGER NOT NULL,
    source_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (item_id) REFERENCES knowledge_items(id),
    FOREIGN KEY (source_id) REFERENCES knowledge_sources(id)
);
CREATE TABLE IF NOT EXISTS knowledge_example_facts (
    example_id INTEGER NOT NULL,
    fact_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (example_id, fact_id, role),
    FOREIGN KEY (example_id) REFERENCES knowledge_examples(id) ON DELETE CASCADE,
    FOREIGN KEY (fact_id) REFERENCES knowledge_facts(id)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_items_category
    ON knowledge_items(category, sort_order, id);
CREATE INDEX IF NOT EXISTS idx_knowledge_facts_lookup
    ON knowledge_facts(fact_type, subject_key, sort_order, id);
"""


class KnowledgeRepository:
    def __init__(self, db_path: Path = DEFAULT_KNOWLEDGE_DB_PATH) -> None:
        self.db_path = Path(db_path)

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.executescript(_SCHEMA)
            await db.commit()

    async def import_dataset(self, dataset: Mapping[str, Any]) -> None:
        """Atomically replace rows addressed by stable keys in one transaction."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                now = _now()
                source_ids: dict[str, int] = {}
                for source in dataset["sources"]:
                    await db.execute(
                        "INSERT INTO knowledge_sources (stable_key, title, source_type, "
                        "source_name, source_reference, version, effective_date, "
                        "verification_status, content_hash, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(stable_key) DO UPDATE SET title=excluded.title, "
                        "source_type=excluded.source_type, source_name=excluded.source_name, "
                        "source_reference=excluded.source_reference, version=excluded.version, "
                        "effective_date=excluded.effective_date, "
                        "verification_status=excluded.verification_status, "
                        "content_hash=excluded.content_hash, updated_at=excluded.updated_at",
                        (source["stable_key"], source["title"], source["source_type"],
                         source["source_name"], source["source_reference"], source.get("version"),
                         source.get("effective_date"), source["verification_status"],
                         source["content_hash"], now, now),
                    )
                    source_ids[source["stable_key"]] = await _id_for(db, "knowledge_sources", source["stable_key"])

                item_ids: dict[str, int] = {}
                for item in dataset["items"]:
                    source_id = source_ids[item["source_key"]]
                    await db.execute(
                        "INSERT INTO knowledge_items (stable_key, category, title, content, "
                        "source_id, source_ref, status, sort_order, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(stable_key) DO UPDATE SET category=excluded.category, "
                        "title=excluded.title, content=excluded.content, source_id=excluded.source_id, "
                        "source_ref=excluded.source_ref, status=excluded.status, "
                        "sort_order=excluded.sort_order, updated_at=excluded.updated_at",
                        (item["stable_key"], item["category"], item["title"], item["content"],
                         source_id, item["source_ref"], item.get("status", "active"),
                         item.get("sort_order", 0), now, now),
                    )
                    item_id = await _id_for(db, "knowledge_items", item["stable_key"])
                    item_ids[item["stable_key"]] = item_id
                    await db.execute("DELETE FROM knowledge_item_tags WHERE item_id = ?", (item_id,))
                    await db.executemany(
                        "INSERT INTO knowledge_item_tags (item_id, tag) VALUES (?, ?)",
                        [(item_id, tag) for tag in item.get("tags", [])],
                    )

                fact_ids: dict[str, int] = {}
                for fact in dataset["facts"]:
                    values = _fact_values(fact)
                    await db.execute(
                        "INSERT INTO knowledge_facts (stable_key, item_id, source_id, fact_type, "
                        "subject_type, subject_key, qualifier_key, qualifier_value, value_number, "
                        "value_text, range_min, range_max, unit, currency, period, condition_text, "
                        "source_ref, sort_order, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(stable_key) DO UPDATE SET item_id=excluded.item_id, "
                        "source_id=excluded.source_id, fact_type=excluded.fact_type, "
                        "subject_type=excluded.subject_type, subject_key=excluded.subject_key, "
                        "qualifier_key=excluded.qualifier_key, qualifier_value=excluded.qualifier_value, "
                        "value_number=excluded.value_number, value_text=excluded.value_text, "
                        "range_min=excluded.range_min, range_max=excluded.range_max, unit=excluded.unit, "
                        "currency=excluded.currency, period=excluded.period, "
                        "condition_text=excluded.condition_text, source_ref=excluded.source_ref, "
                        "sort_order=excluded.sort_order, updated_at=excluded.updated_at",
                        (fact["stable_key"], item_ids[fact["item_key"]], source_ids[fact["source_key"]],
                         fact["fact_type"], fact["subject_type"], fact["subject_key"],
                         fact.get("qualifier_key"), fact.get("qualifier_value"), *values,
                         fact.get("unit"), fact.get("currency"), fact.get("period"),
                         fact.get("condition_text"), fact["source_ref"], fact.get("sort_order", 0), now, now),
                    )
                    fact_ids[fact["stable_key"]] = await _id_for(db, "knowledge_facts", fact["stable_key"])

                for example in dataset.get("examples", []):
                    await db.execute(
                        "INSERT INTO knowledge_examples (stable_key, item_id, title, scenario, "
                        "explanation, result_number, currency, source_id, source_ref, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(stable_key) DO UPDATE SET item_id=excluded.item_id, "
                        "title=excluded.title, scenario=excluded.scenario, explanation=excluded.explanation, "
                        "result_number=excluded.result_number, currency=excluded.currency, "
                        "source_id=excluded.source_id, source_ref=excluded.source_ref, updated_at=excluded.updated_at",
                        (example["stable_key"], item_ids[example["item_key"]], example["title"],
                         example["scenario"], example["explanation"], _decimal_text(example.get("result_number")),
                         example.get("currency"), source_ids[example["source_key"]],
                         example["source_ref"], now, now),
                    )
                    example_id = await _id_for(db, "knowledge_examples", example["stable_key"])
                    await db.execute("DELETE FROM knowledge_example_facts WHERE example_id = ?", (example_id,))
                    await db.executemany(
                        "INSERT INTO knowledge_example_facts (example_id, fact_id, role, sort_order) "
                        "VALUES (?, ?, ?, ?)",
                        [(example_id, fact_ids[ref["fact_key"]], ref.get("role", "input"), i)
                         for i, ref in enumerate(example["fact_refs"])],
                    )

                for item in dataset["items"]:
                    from_id = item_ids[item["stable_key"]]
                    await db.execute("DELETE FROM knowledge_item_relations WHERE from_item_id = ?", (from_id,))
                    await db.executemany(
                        "INSERT INTO knowledge_item_relations (from_item_id, relation_type, to_item_id) "
                        "VALUES (?, ?, ?)",
                        [(from_id, rel["relation_type"], item_ids[rel["item_key"]])
                         for rel in item.get("related", [])],
                    )
                violations = await (await db.execute("PRAGMA foreign_key_check")).fetchall()
                if violations:
                    raise RuntimeError("Knowledge import violated foreign keys")
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def get_source(self, stable_key: str) -> KnowledgeSource | None:
        row = await self._one("SELECT * FROM knowledge_sources WHERE stable_key = ?", (stable_key,))
        return _source(row) if row else None

    async def get_sources(self, source_ids: Sequence[int] | None = None) -> list[KnowledgeSource]:
        if source_ids is not None:
            unique_ids = tuple(dict.fromkeys(int(value) for value in source_ids))
            if not unique_ids:
                return []
            placeholders = ",".join("?" for _ in unique_ids)
            rows = await self._all(
                f"SELECT * FROM knowledge_sources WHERE id IN ({placeholders}) ORDER BY stable_key",
                unique_ids,
            )
        else:
            rows = await self._all("SELECT * FROM knowledge_sources ORDER BY stable_key", ())
        return [_source(row) for row in rows]

    async def get_item(self, stable_key: str) -> KnowledgeItem | None:
        row = await self._one("SELECT * FROM knowledge_items WHERE stable_key = ?", (stable_key,))
        return await self._item(row) if row else None

    async def get_by_category(self, category: str) -> list[KnowledgeItem]:
        rows = await self._all(
            "SELECT * FROM knowledge_items WHERE category = ? AND status = 'active' "
            "ORDER BY sort_order, id", (category,)
        )
        return [await self._item(row) for row in rows]

    async def list_items(self, limit: int = 200) -> list[KnowledgeItem]:
        """All active items across every category, for a plain browse view
        (web «База знаний») - the existing surface only supports lookup by
        known category (get_by_category) or a ranked query (search_text),
        neither of which lists everything at once."""
        rows = await self._all(
            "SELECT * FROM knowledge_items WHERE status = 'active' "
            "ORDER BY category, sort_order, id LIMIT ?", (_limit(limit),)
        )
        return [await self._item(row) for row in rows]

    async def search_text(self, query: str, limit: int = 8) -> list[KnowledgeItem]:
        """Return active items ranked by deterministic lexical relevance.

        This intentionally scans the small item catalogue instead of using FTS or
        external search. Exact stable-key, tag, and title matches outrank phrase
        and token matches. Each item is scored once regardless of matching tags.
        """
        if limit <= 0:
            return []
        normalized = _normalize_search_text(query)
        tokens = _meaningful_search_tokens(normalized)
        if not normalized or not tokens:
            return []
        rows = await self._all(
            "SELECT i.* FROM knowledge_items i WHERE i.status='active' ORDER BY i.id", ()
        )
        ranked: list[tuple[int, int, int, aiosqlite.Row, tuple[str, ...]]] = []
        for row in rows:
            tag_rows = await self._all(
                "SELECT tag FROM knowledge_item_tags WHERE item_id=? ORDER BY tag", (row["id"],)
            )
            tags = tuple(tag["tag"] for tag in tag_rows)
            score, matched_tokens = _search_score(row, tags, normalized, tokens)
            if score >= 200:
                ranked.append((score, matched_tokens, -int(row["sort_order"]), row, tags))
        ranked.sort(
            key=lambda value: (
                -value[0], -value[1], -value[2],
                value[3]["stable_key"], value[3]["id"],
            )
        )
        return [_item(row, tags) for _, _, _, row, tags in ranked[:limit]]

    async def get_facts(self, *, item_key: str | None = None, fact_type: str | None = None,
                        subject_key: str | None = None) -> list[KnowledgeFact]:
        clauses, values = [], []
        if item_key is not None:
            clauses.append("i.stable_key = ?"); values.append(item_key)
        if fact_type is not None:
            clauses.append("f.fact_type = ?"); values.append(fact_type)
        if subject_key is not None:
            clauses.append("f.subject_key = ?"); values.append(subject_key)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = await self._all(
            "SELECT f.* FROM knowledge_facts f JOIN knowledge_items i ON i.id=f.item_id" +
            where + " ORDER BY f.sort_order, f.id", tuple(values))
        return [_fact(row) for row in rows]

    async def get_example(self, stable_key: str) -> KnowledgeExample | None:
        row = await self._one("SELECT * FROM knowledge_examples WHERE stable_key = ?", (stable_key,))
        if not row:
            return None
        refs = await self._all(
            "SELECT f.stable_key FROM knowledge_example_facts ef "
            "JOIN knowledge_facts f ON f.id=ef.fact_id WHERE ef.example_id=? "
            "ORDER BY ef.sort_order", (row["id"],))
        return _example(row, tuple(ref["stable_key"] for ref in refs))

    async def get_examples_for_item(self, item_key: str) -> list[KnowledgeExample]:
        rows = await self._all(
            "SELECT e.* FROM knowledge_examples e "
            "JOIN knowledge_items i ON i.id=e.item_id WHERE i.stable_key=? "
            "ORDER BY e.id", (item_key,)
        )
        result: list[KnowledgeExample] = []
        for row in rows:
            refs = await self._all(
                "SELECT f.stable_key FROM knowledge_example_facts ef "
                "JOIN knowledge_facts f ON f.id=ef.fact_id WHERE ef.example_id=? "
                "ORDER BY ef.sort_order", (row["id"],)
            )
            result.append(_example(row, tuple(ref["stable_key"] for ref in refs)))
        return result

    async def get_related(
        self, stable_key: str, relation_type: str | None = None
    ) -> list[KnowledgeItem]:
        sql = (
            "SELECT target.* FROM knowledge_items source "
            "JOIN knowledge_item_relations relation ON relation.from_item_id=source.id "
            "JOIN knowledge_items target ON target.id=relation.to_item_id "
            "WHERE source.stable_key=?"
        )
        params: list[Any] = [stable_key]
        if relation_type is not None:
            sql += " AND relation.relation_type=?"
            params.append(relation_type)
        sql += " ORDER BY target.sort_order, target.id"
        rows = await self._all(sql, tuple(params))
        return [await self._item(row) for row in rows]

    async def _item(self, row: aiosqlite.Row) -> KnowledgeItem:
        tags = await self._all("SELECT tag FROM knowledge_item_tags WHERE item_id=? ORDER BY tag", (row["id"],))
        return _item(row, tuple(tag["tag"] for tag in tags))

    async def _one(self, sql: str, params: Sequence[Any]) -> aiosqlite.Row | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            return await (await db.execute(sql, params)).fetchone()

    async def _all(self, sql: str, params: Sequence[Any]) -> list[aiosqlite.Row]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            return await (await db.execute(sql, params)).fetchall()


async def _id_for(db: aiosqlite.Connection, table: str, stable_key: str) -> int:
    row = await (await db.execute(f"SELECT id FROM {table} WHERE stable_key = ?", (stable_key,))).fetchone()
    if row is None:
        raise RuntimeError(f"Missing {table} row after upsert")
    return int(row[0])


def _fact_values(fact: Mapping[str, Any]) -> tuple[str | None, ...]:
    return (_decimal_text(fact.get("value_number")), fact.get("value_text"),
            _decimal_text(fact.get("range_min")), _decimal_text(fact.get("range_max")))


def _decimal_text(value: Any) -> str | None:
    return None if value is None else str(Decimal(str(value)))


def _decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _limit(value: int) -> int:
    if value <= 0:
        raise ValueError("limit должен быть положительным")
    return value


def _source(r: Mapping[str, Any]) -> KnowledgeSource:
    return KnowledgeSource(**dict(r))


def _item(r: Mapping[str, Any], tags: tuple[str, ...]) -> KnowledgeItem:
    return KnowledgeItem(**dict(r), tags=tags)


def _fact(r: Mapping[str, Any]) -> KnowledgeFact:
    data = dict(r)
    for key in ("value_number", "range_min", "range_max"):
        data[key] = _decimal(data[key])
    return KnowledgeFact(**data)


def _example(r: Mapping[str, Any], fact_keys: tuple[str, ...]) -> KnowledgeExample:
    data = dict(r); data["result_number"] = _decimal(data["result_number"])
    return KnowledgeExample(**data, fact_keys=fact_keys)


def _normalize_search_text(value: str) -> str:
    if not isinstance(value, str):
        return ""
    tokens = _SEARCH_TOKEN_RE.findall(value.casefold().replace("_", " "))
    return " ".join(_canonical_search_token(token) for token in tokens)


def _meaningful_search_tokens(normalized: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        token for token in normalized.split() if len(token) > 1 and token not in _SEARCH_STOP_WORDS
    ))


def _canonical_search_token(token: str) -> str:
    if token in _SEARCH_TOKEN_ALIASES:
        return _SEARCH_TOKEN_ALIASES[token]
    for prefix, replacement in (
        ("комисс", "commission"), ("бронир", "booking"), ("брон", "booking"), ("гостев", "guest"),
        ("регистрацион", "registration"), ("двойн", "dual"), ("команд", "team"),
        ("партнер", "partner"), ("партнёр", "partner"), ("крипт", "crypto"),
        ("процент", "percent"), ("гарант", "guarantee"),
    ):
        if token.startswith(prefix):
            return replacement
    return token


def _search_score(
    row: Mapping[str, Any], tags: tuple[str, ...], query: str, tokens: tuple[str, ...]
) -> tuple[int, int]:
    stable_key = _normalize_search_text(row["stable_key"])
    title = _normalize_search_text(row["title"])
    category = _normalize_search_text(row["category"])
    content = _normalize_search_text(row["content"])
    normalized_tags = tuple(_normalize_search_text(tag) for tag in tags)

    score = 0
    if query == stable_key:
        score += 1200
    if query in normalized_tags:
        score += 1100
    if query == title:
        score += 1000
    if query == category:
        score += 800
    if query and query in stable_key:
        score += 360
    if query and query in title:
        score += 320
    if query and any(query in tag for tag in normalized_tags):
        score += 280
    if query and any(tag and tag in query for tag in normalized_tags):
        score += 260
    if title and title in query:
        score += 300
    if query and query in category:
        score += 180
    if query and query in content:
        score += 120

    matched: set[str] = set()
    fields = ((stable_key, 80), (title, 70), (category, 35), (content, 15))
    for token in tokens:
        token_score = 0
        for field, weight in fields:
            if token in field:
                token_score = max(token_score, weight)
        if any(token in tag for tag in normalized_tags):
            token_score = max(token_score, 75)
        if token_score:
            matched.add(token)
            score += token_score
    if matched and len(matched) == len(tokens):
        score += 100
    return score, len(matched)
