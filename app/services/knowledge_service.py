from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

from app.domain.knowledge import KnowledgeExample, KnowledgeFact, KnowledgeItem
from app.repositories.knowledge_repository import KnowledgeRepository


_TOKEN_RE = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)
_COMPLIANCE_TYPES = {"compliance_rule", "staleness_compliance_rule", "sponsor_compliance_rule"}


@dataclass(frozen=True)
class SourceReference:
    source_id: int
    stable_key: str
    title: str
    source_reference: str
    verification_status: str


@dataclass(frozen=True)
class KnowledgeBundle:
    question: str
    primary_items: tuple[KnowledgeItem, ...]
    related_items: tuple[KnowledgeItem, ...]
    facts: tuple[KnowledgeFact, ...]
    compliance_facts: tuple[KnowledgeFact, ...]
    examples: tuple[KnowledgeExample, ...]
    sources: tuple[SourceReference, ...]
    potentially_ambiguous: bool
    ambiguity_reasons: tuple[str, ...]
    missing_definitions: tuple[str, ...]


class KnowledgeService:
    """Build bounded, provenance-preserving knowledge bundles without an LLM."""

    def __init__(
        self,
        repository: KnowledgeRepository,
        *,
        max_primary_items: int = 5,
        max_related_items: int = 12,
        max_facts: int = 40,
        max_examples: int = 3,
    ) -> None:
        self.repository = repository
        self.max_primary_items = max(1, max_primary_items)
        self.max_related_items = max(0, max_related_items)
        self.max_facts = max(1, max_facts)
        self.max_examples = max(0, max_examples)

    async def retrieve(self, question: str) -> KnowledgeBundle:
        normalized = _normalize(question)
        tokens = _tokens(normalized)
        searched = await self.repository.search_text(question, limit=self.max_primary_items)
        searched = _filter_contextual_false_positives(normalized, searched)
        required, compliance_keys, ambiguous, reasons, missing = _retrieval_policy(normalized)
        primary = await self._merge_items(required, searched, self.max_primary_items)
        compliance_items = await self._items_for_keys(compliance_keys)

        related_candidates: list[KnowledgeItem] = []
        for item in primary:
            related_candidates.extend(await self.repository.get_related(item.stable_key))
        related = _dedupe_items(
            (item for item in (*compliance_items, *related_candidates)
             if item.stable_key not in {p.stable_key for p in primary}),
            self.max_related_items,
        )

        all_items = _unique_by_key((*primary, *related, *compliance_items))
        fact_candidates: list[KnowledgeFact] = []
        for item in all_items:
            fact_candidates.extend(await self.repository.get_facts(item_key=item.stable_key))

        compliance = tuple(sorted(
            (fact for fact in fact_candidates if fact.fact_type in _COMPLIANCE_TYPES),
            key=lambda fact: (fact.sort_order, fact.stable_key),
        ))
        regular = [fact for fact in fact_candidates if fact.fact_type not in _COMPLIANCE_TYPES]
        ranked_regular = sorted(
            regular,
            key=lambda fact: (-_fact_score(fact, tokens), fact.sort_order, fact.stable_key),
        )
        available = max(0, self.max_facts - len(compliance))
        facts = tuple(ranked_regular[:available])

        examples: list[KnowledgeExample] = []
        if self.max_examples:
            for item in all_items:
                examples.extend(await self.repository.get_examples_for_item(item.stable_key))
            examples.sort(key=lambda example: (-_example_score(example, tokens), example.stable_key))
            examples = examples[:self.max_examples]

        source_ids = [item.source_id for item in all_items]
        source_ids.extend(fact.source_id for fact in (*facts, *compliance))
        source_ids.extend(example.source_id for example in examples)
        sources = await self.repository.get_sources(source_ids)

        return KnowledgeBundle(
            question=question,
            primary_items=tuple(primary),
            related_items=tuple(related),
            facts=facts,
            compliance_facts=compliance,
            examples=tuple(examples),
            sources=tuple(SourceReference(
                source_id=source.id,
                stable_key=source.stable_key,
                title=source.title,
                source_reference=source.source_reference,
                verification_status=source.verification_status,
            ) for source in sources),
            potentially_ambiguous=ambiguous,
            ambiguity_reasons=tuple(reasons),
            missing_definitions=tuple(missing),
        )

    async def _merge_items(
        self, required_keys: Iterable[str], searched: Iterable[KnowledgeItem], limit: int
    ) -> list[KnowledgeItem]:
        required = await self._items_for_keys(required_keys)
        return _dedupe_items((*required, *searched), limit)

    async def _items_for_keys(self, keys: Iterable[str]) -> list[KnowledgeItem]:
        result: list[KnowledgeItem] = []
        for key in keys:
            item = await self.repository.get_item(key)
            if item is not None:
                result.append(item)
        return result


def _retrieval_policy(
    query: str,
) -> tuple[tuple[str, ...], tuple[str, ...], bool, tuple[str, ...], tuple[str, ...]]:
    required: list[str] = []
    compliance: list[str] = []
    reasons: list[str] = []
    missing: list[str] = []
    ambiguous = False

    def has(*parts: str) -> bool:
        return all(part in query for part in parts)

    if "travel advantage" in query:
        required.append("ta.platform")
    if "mwr life" in query:
        required.append("mwr.life")
    if "new partner" in query or "с чего начать" in query:
        required.extend(("mwr.getting_started", "mwr.partner_product_knowledge"))
    if "loyalty points" in query:
        required.append("ta.loyalty_points")
    if "travel credits" in query:
        required.append("ta.travel_credits")
    if "loyalty points" in query and "travel credits" in query:
        required.append("ta.points_transfer_and_use_delta")
    if "guest pass" in query:
        required.append("ta.guest_pass")
    if "best price" in query or "всегда дешевле" in query:
        required.append("ta.best_price_guarantee")
        compliance.append("mwr.claims_and_staleness_compliance")
    # Live prod bug: "Зачем платить за членство каждый месяц, если я и без
    # клуба могу сам бронировать отели и покупать билеты?" matched only
    # "booking" (from "бронировать") below - "членство"/"клуб" had no rule
    # at all, so booking-backend facts (pending/additional verification/
    # supplier update delay) became the entire required core instead of
    # membership value/cancellation facts, even though the question is
    # about membership, and "бронировать" is only mentioned inside a
    # comparison ("если я и без клуба могу сам..."), not the actual topic.
    # membership_intent is intentionally a minimal stem check (не словарь):
    # "член" covers "членство"/"членский", separate from "клуб"/"подписк".
    membership_intent = any(stem in query for stem in ("член", "клуб", "подписк"))
    if membership_intent:
        required.extend(("ta.membership", "ta.membership.cancellation", "mwr.member_vs_ambassador"))
    # Same live prod bug, other half: a genuine booking question ("Почему
    # бронирование после оплаты pending?") must keep matching this rule
    # unchanged - only suppressed when membership_intent is ALSO explicit,
    # so "booking" stays a required core for its own real topic.
    if "booking" in query and not membership_intent:
        required.extend(("ta.booking_status_inventory", "ta.support"))
    if "commission" in query:
        required.append("ta.payments_and_support_routing")
    if "registration team" in query or "dual team" in query or (
        "binary" in query and not any(word in query for word in ("дерево", "разработ", "код"))
    ):
        required.append("mwr.team_structures")
    if "builder bonus" in query:
        required.append("ta.builder_bonus")
    if "acceleration" in query:
        required.append("ta.acceleration_bonus")
    if "silver" in query and any(word in query for word in ("закрыв", "закрыва", "стать")):
        required.append("ta.rank_qualification")
    if "silver" in query and "день" in query:
        required.append("ta.dual_team_income")
    if "silver" in query and "elite turbo" in query:
        required.extend(("ta.member_bonus", "ta.builder_bonus", "ta.compensation_examples"))
    generic_ruby_pay = "ruby" in query and any(
        word in query for word in ("выплат", "доход", "плат", "получ")
    ) and not any(block in query for block in ("builder bonus", "dual team"))
    if generic_ruby_pay:
        required.extend(("ta.dual_team_income", "ta.builder_bonus", "ta.rank_qualification"))
        ambiguous = True
        reasons.append("Ruby может относиться к нескольким compensation blocks.")
    if "mwr academy" in query:
        required.append("mwr.getting_started")
        missing.append("Отдельное официальное определение MWR Academy не предоставлено.")
    if "l i f e cycle" in query or "life cycle" in query:
        required.append("mwr.getting_started")
        missing.append("Отдельное официальное определение L.I.F.E. Cycle не предоставлено.")
    if "crypto" in query:
        required.append("ta.payments_and_support_routing")
    if any(word in query for word in ("guarantee", "точно", "заработаю", "пассивн")) and any(
        word in query for word in ("заработ", "доход", "получ", "плат", "ruby")
    ):
        required.append("mwr.claims_and_staleness_compliance")
        compliance.append("mwr.claims_and_staleness_compliance")
    if has("актуальн", "life experience") or has("сейчас", "life experience"):
        required.append("ta.life_experiences")
        compliance.append("mwr.claims_and_staleness_compliance")
    if "тариф" in query or "elite turbo" in query or "turbo" in query:
        required.extend(("ta.membership", "ta.elite_turbo_features"))
    if ("elite" in query or "turbo" in query) and "pv" in query:
        required.extend(("ta.membership", "mwr.qualification_status"))
    points_context = "points" in query or "loyalty" in query or (
        "балл" in query and any(word in query for word in (
            "вывести", "деньг", "доллар", "аккаунт", "life experience", "оплат", "переда",
        ))
    )
    if points_context:
        required.extend(("ta.loyalty_points", "ta.points_transfer_and_use_delta"))
    if ("вывести" in query or "деньг" in query or "доллар" in query) and (
        points_context
    ):
        required.append("ta.loyalty_points")
    if "travel credits" in query and any(word in query for word in ("подар", "переда", "transfer")):
        required.extend(("ta.travel_credits", "ta.points_transfer_and_use_delta"))
    if "life experience" in query:
        required.append("ta.life_experiences")
        if points_context:
            required.extend(("ta.loyalty_points", "ta.points_transfer_and_use_delta"))
        if "100" in query and any(word in query for word in ("втор", "гост", "second guest")):
            required.append("ta.elite_turbo_features")
    if "guest" in query and ("доступ" in query or "pass" in query):
        required.append("ta.guest_pass")
    if "150" in query and ("guarantee" in query or "percent" in query):
        required.append("ta.best_price_guarantee")
        compliance.append("mwr.claims_and_staleness_compliance")
    if "сейчас" in query and any(word in query for word in ("стоить", "стоит", "цена", "price")):
        compliance.append("mwr.claims_and_staleness_compliance")
        if "elite" in query or "vip" in query or "turbo" in query:
            required.append("ta.membership")
    if "confirmation" in query or "pending" in query or "пендинг" in query:
        required.extend(("ta.booking_status_inventory", "ta.support"))
    if "support" in query and any(word in query for word in ("отел", "travel", "booking")):
        required.append("ta.support")
    if "vip" in query and "elite" in query:
        required.append("ta.membership")
    if "silver" in query and any(word in query for word in ("услов", "left", "right")):
        required.append("ta.rank_qualification")
    ranks = ("silver", "gold", "platinum", "titanium", "jade", "pearl", "emerald", "ruby",
             "sapphire", "diamond", "royal")
    if any(rank in query for rank in ranks) and "pv" in query:
        required.append("ta.rank_qualification")
    if any(rank in query for rank in ranks) and any(word in query for word in ("daily", "день")):
        required.append("ta.dual_team_income")
    if any(word in query for word in ("актуальн", "сегодня", "текущ")) and any(
        word in query for word in ("акци", "promotion", "цена", "отел", "availability", "места")
    ):
        compliance.append("mwr.claims_and_staleness_compliance")
    if "vip180" in query or "elite180" in query:
        compliance.append("mwr.claims_and_staleness_compliance")
    if any(word in query for word in ("mobile app", "biz center", "приложение")):
        required.append("mwr.getting_started")
    if "ambassador" in query:
        required.append("mwr.lifestyle_ambassador")
    if "sponsor" in query:
        required.append("mwr.sponsor_training_responsibility")
    if "dual team" in query and any(word in query for word in ("прогноз", "guarantee")):
        compliance.append("mwr.claims_and_staleness_compliance")

    return (
        tuple(dict.fromkeys(required)),
        tuple(dict.fromkeys(compliance)),
        ambiguous,
        tuple(reasons),
        tuple(missing),
    )


def _normalize(value: str) -> str:
    raw_tokens = _TOKEN_RE.findall((value or "").casefold().replace(".", " "))
    return " ".join(_canonical_token(token) for token in raw_tokens)


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(token for token in value.split() if len(token) > 1))


def _canonical_token(token: str) -> str:
    aliases = {
        "сильвер": "silver", "силвер": "silver", "silvr": "silver",
        "руби": "ruby", "rubi": "ruby", "элит": "elite", "турбо": "turbo",
        "тревел": "travel", "travle": "travel", "кредиты": "credits", "кредит": "credits",
        "поинт": "points", "поинты": "points", "поинтов": "points", "лоалти": "loyalty",
        "loyality": "loyalty", "credtis": "credits", "лайф": "life",
        "экспириенс": "experience", "экспириенсы": "experience", "мвр": "mwr",
        "академия": "academy", "академии": "academy", "бинар": "binary",
        "бинара": "binary", "криптой": "crypto", "крипта": "crypto",
        "криптовалютой": "crypto", "гарантия": "guarantee", "garantee": "guarantee",
        "бронь": "booking", "брони": "booking", "бронирование": "booking",
        "новому": "new", "новый": "new", "новичку": "new", "партнеру": "partner",
        "партнёру": "partner", "партнер": "partner", "партнёр": "partner",
        "advantge": "advantage", "advatage": "advantage", "guset": "guest", "lp": "points",
    }
    if token in aliases:
        return aliases[token]
    for prefix, replacement in (
        ("комисс", "commission"), ("бронир", "booking"), ("брон", "booking"), ("гостев", "guest"),
        ("регистрацион", "registration"), ("двойн", "dual"), ("команд", "team"),
        ("крипт", "crypto"), ("процент", "percent"), ("гарант", "guarantee"),
    ):
        if token.startswith(prefix):
            return replacement
    return token


def _dedupe_items(items: Iterable[KnowledgeItem], limit: int) -> list[KnowledgeItem]:
    result: list[KnowledgeItem] = []
    seen: set[str] = set()
    for item in items:
        if item.stable_key not in seen:
            result.append(item)
            seen.add(item.stable_key)
        if len(result) >= limit:
            break
    return result


def _unique_by_key(items: Iterable[KnowledgeItem]) -> tuple[KnowledgeItem, ...]:
    result: list[KnowledgeItem] = []
    seen: set[str] = set()
    for item in items:
        if item.stable_key not in seen:
            result.append(item)
            seen.add(item.stable_key)
    return tuple(result)


def _filter_contextual_false_positives(
    query: str, items: Iterable[KnowledgeItem]
) -> list[KnowledgeItem]:
    if "elite dangerous" in query or ("elite" in query and "игра" in query):
        return []
    excluded: set[str] = set()
    if "binary" in query and any(word in query for word in ("tree", "дерево", "разработ", "код")):
        excluded.update(("mwr.team_structures", "ta.dual_team_income"))
    if "ruby" in query and any(word in query for word in ("язык", "programming", "программир")):
        excluded.update(("ta.builder_bonus", "ta.dual_team_income", "ta.rank_qualification"))
    return [item for item in items if item.stable_key not in excluded]


def _fact_score(fact: KnowledgeFact, tokens: tuple[str, ...]) -> int:
    text = _normalize(" ".join(str(value or "") for value in (
        fact.stable_key, fact.fact_type, fact.subject_key, fact.qualifier_key,
        fact.qualifier_value, fact.value_text, fact.condition_text,
    )))
    return sum(1 for token in tokens if token in text)


def _example_score(example: KnowledgeExample, tokens: tuple[str, ...]) -> int:
    text = _normalize(f"{example.stable_key} {example.title} {example.scenario} {example.explanation}")
    return sum(1 for token in tokens if token in text)
