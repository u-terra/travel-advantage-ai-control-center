from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

from app.domain.competitor_discovery import canonical_domain
from app.domain.competitor_intelligence import (
    DATA_ORIGIN_DIRECT_FETCH,
    DATA_ORIGIN_RADAR_SIGNAL,
    CompetitorIntelligence,
    CompetitorSourceEvidence,
    ContentOpportunity,
)
from app.domain.competitors import Competitor
from app.domain.usage import UsageStatus
from app.planner.fetch import FetchedPublicSource, PublicSourceFetchError, fetch_public_source_sync
from app.repositories.usage_ledger_repository import UsageLedgerRepository
from app.repositories.workspace_signal_repository import (
    WorkspaceSignalRecord,
    WorkspaceSignalRepository,
)
from app.services.knowledge_service import KnowledgeService
from app.services.llm.base import LLMProvider
from app.services.llm.models import SourceAnalysisPayload
from app.services.usage_recorder import record_llm_call

_MAX_SOURCES = 5
_MAX_OPPORTUNITIES = 5
# Radar-signal fallback (used only when the competitor's own site can't be
# fetched at all - see CompetitorIntelligenceService.analyze()): deliberately
# smaller than _MAX_SOURCES and a stricter freshness window than the general
# Lead Radar UI (app/services/lead_radar.py's _FRESH_DAYS=30 is the same
# number, kept in sync intentionally - "fresh" means the same thing
# everywhere in this product) - a handful of genuinely matching, genuinely
# recent mentions, not a bulk dump of tangential travel news.
_MAX_SIGNAL_FALLBACK_SOURCES = 3
_SIGNAL_FRESH_DAYS = 30
# A label shorter than this is too generic to safely substring/word-match
# against arbitrary signal text (see _relevant_recent_signals) - domain
# matching still applies regardless of label length.
_MIN_LABEL_MATCH_LENGTH = 3
_OPPORTUNITY_CATEGORIES = (
    ("AI и технологии в travel", (" ai ", "chatgpt", "artificial intelligence", "technology", "digital", "biometric", "esim", "app", "интеллект", "нейросет")),
    ("travel trends", ("trend", "traveler", "traveller", "tourism", "booking data", "тренд")),
    ("направления", ("destination", "city", "country", "disneyland", "legoland", "resort", "направлен", "город", "стран")),
    ("практический travel guide", ("guide", "visa", "airport", "transit", "itinerary", "tips", "passport", "how to", "гид", "виз", "аэропорт", "транзит")),
    ("loyalty и promotions", ("loyal", "member", "reward", "coin", "promo", "discount", "deal", "coupon", "sale", "акци", "скидк", "промокод")),
    ("customer UX", ("support", "payment", "cancel", "refund", "search", "flexib", "pay", "alipay", "wechat", "оплат", "поддержк")),
    ("изменение спроса", ("demand", "surge", "growth", "increase", "decrease", "year-on-year", "спрос")),
    ("новый продукт или сервис", ("launch", "new product", "new service", "new feature", "introduc")),
)
_DEDUP_STOP_WORDS = frozenset({
    "the", "and", "for", "with", "from", "this", "that", "trip", "com",
    "как", "что", "для", "или", "это", "при", "про",
})
_NOISE_SUBSTRINGS = (
    "read more",
    "в источнике перечислены",
    "перечислены статьи",
    "на сайте перечислены",
    "перечислены разделы",
    "в нижней части списка",
    "в тексте присутствуют ссылки",
    "ссылки на",
)
_COMPOSITE_MARKERS = ("темы материалов включают", "материалы включают", "материалы про")
_CONNECTOR_WORDS = frozenset({"vs", "and", "or", "to", "in", "of", "for", "de", "the", "a", "an"})
_ANGLE_TEMPLATES = {
    "AI и технологии в travel": (
        "Разбираем своими словами, как инструменты вроде «{entity}» меняют "
        "планирование поездок — и что из этого стоит объяснить нашей аудитории"
    ),
    "travel trends": (
        "Интерпретируем тренд «{entity}» в поведении путешественников и что "
        "из него можно взять для собственного контента"
    ),
    "направления": (
        "Раскрываем направление «{entity}» самостоятельным материалом — что "
        "важно знать путешественнику перед поездкой"
    ),
    "практический travel guide": (
        "Готовим практический гид по теме «{entity}» с конкретными шагами "
        "для путешественника, не пересказывая источник"
    ),
    "loyalty и promotions": (
        "Объясняем аудитории, как находить и проверять предложения вроде "
        "«{entity}», не копируя рекламный текст конкурента"
    ),
    "customer UX": (
        "Показываем, как сервис или оплата «{entity}» влияют на удобство "
        "поездки для нашей аудитории"
    ),
    "изменение спроса": (
        "Интерпретируем сигнал спроса «{entity}» и что он означает для "
        "планирования поездок прямо сейчас"
    ),
    "новый продукт или сервис": (
        "Рассказываем своими словами о механике «{entity}», которую "
        "использует конкурент, и чем она полезна путешественнику"
    ),
}
_DATE_RE = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+20\d{2}\b",
    re.IGNORECASE,
)


class CompetitorIntelligenceUnavailable(RuntimeError):
    pass


class CompetitorIntelligenceService:
    """Bounded public-source analysis for any saved competitor.

    Conventional discovery paths are generic hints, never competitor identity:
    localized/final URLs remain provenance attached to the one persisted id.
    """

    def __init__(
        self,
        provider: LLMProvider,
        knowledge_service: KnowledgeService,
        *,
        fetcher: Callable[[str], FetchedPublicSource] = fetch_public_source_sync,
        usage_ledger_repository: UsageLedgerRepository | None = None,
        workspace_signal_repository: WorkspaceSignalRepository | None = None,
    ) -> None:
        self._provider = provider
        self._knowledge = knowledge_service
        self._fetcher = fetcher
        self._usage_ledger = usage_ledger_repository
        # Optional/default-None, same convention as usage_ledger_repository
        # above - existing callers/tests that don't pass this are entirely
        # unaffected (no signal fallback is attempted, same as before this
        # feature existed). See _relevant_recent_signals().
        self._workspace_signal_repository = workspace_signal_repository

    async def analyze(
        self, competitor: Competitor, *, ta_affiliated: bool,
    ) -> CompetitorIntelligence:
        """ta_affiliated is resolved by the caller from BusinessProfile.ta_affiliated
        (fail-closed - see app.web_api._is_ta_affiliated / app.handlers.competitors),
        never here - this service has no workspace/tenant lookup of its own by
        design, so both the bot and the web caller must pass it explicitly."""
        discovered_at = datetime.now(timezone.utc).isoformat()
        fetched: list[FetchedPublicSource] = []
        for url in _candidate_urls(competitor.url):
            try:
                source = await asyncio.to_thread(self._fetcher, url)
            except PublicSourceFetchError:
                continue
            if source.final_url not in {item.final_url for item in fetched}:
                fetched.append(source)
            if len(fetched) == _MAX_SOURCES:
                break

        data_origin = DATA_ORIGIN_DIRECT_FETCH
        # signal_dates carries each fallback source's REAL Radar date
        # (record.raw_created_at), keyed by the same final_url the source
        # loop below groups on - so evidence/fresh_signals can report the
        # actual signal date instead of _freshness()'s in-text date guess,
        # which rarely finds anything in a short Radar title/summary.
        signal_dates: dict[str, str] = {}
        if not fetched:
            # Step 2/3 of the fallback order: the competitor's own site
            # could not be read at all - before giving up, look for
            # genuinely matching, genuinely recent Radar signals already
            # visible to THIS workspace (see _relevant_recent_signals -
            # workspace isolation is inherited from
            # WorkspaceSignalRepository.list_for_workspace, not
            # reimplemented here). Never attempted when the direct fetch
            # already produced usable sources - existing behaviour for a
            # readable site is completely unchanged.
            matched_signals = await self._relevant_recent_signals(competitor)
            if matched_signals:
                # Keyed by the same final_url computation
                # _sources_from_signals() uses, NOT by zipping positionally
                # with `fetched` - _sources_from_signals() can drop/dedupe
                # records, which would silently misalign a positional zip.
                signal_dates = {
                    (signal.item_url or f"radar-signal:{signal.interpretation_id}"):
                        signal.raw_created_at
                    for signal in matched_signals
                }
                fetched = _sources_from_signals(matched_signals)
                data_origin = DATA_ORIGIN_RADAR_SIGNAL
            if not fetched:
                # Step 5: neither a readable direct source nor a relevant
                # recent signal exists - honest, precise refusal instead of
                # a vague "couldn't read sources" message. Callers (Telegram:
                # app/handlers/competitors.py, Web: app/web_api.py) surface
                # this text as-is; the Web chat path folds it into the LLM's
                # context together with an explicit instruction not to pass
                # off general model knowledge as fresh data (step 6).
                raise CompetitorIntelligenceUnavailable(
                    "Свежие источники по этому конкуренту найти не удалось, "
                    "поэтому анализ основан на доступных устойчивых данных."
                )

        evidence: list[CompetitorSourceEvidence] = []
        analyses = []
        for source in fetched:
            analysis = await asyncio.to_thread(
                self._provider.analyze_source, source_text=source.text[:6_000],
            )
            # Usage Cost & Subscription Foundation: analysis.usage is real
            # tokens if/when Content Factory's response includes them (see
            # app/services/content_factory.py); today it's None.
            await record_llm_call(
                self._usage_ledger, workspace_id=competitor.workspace_id,
                telegram_user_id=None, module="competitor_intelligence",
                provider=self._provider.name,
                usage=analysis.usage if analysis is not None else None,
                status=UsageStatus.SUCCESS if analysis is not None else UsageStatus.FAILURE,
            )
            if analysis is None:
                analysis = _fallback_analysis(source)
            analyses.append((source, analysis))
            evidence.append(CompetitorSourceEvidence(
                title=source.title or source.final_url,
                url=source.url,
                final_url=source.final_url,
                discovered_at=discovered_at,
                freshness=_source_freshness(source, signal_dates),
                summary=analysis.summary,
                key_facts=analysis.key_facts,
                # Step 4: every evidence entry is explicitly tagged with
                # where its text actually came from - never silently
                # presented as data from the competitor's own site when it
                # is not (see app.domain.competitor_intelligence's
                # DATA_ORIGIN_* docstring for who reads this).
                origin=data_origin,
            ))
        if not analyses:
            raise CompetitorIntelligenceUnavailable("Источники прочитаны, но анализ недоступен.")

        if ta_affiliated:
            bundle = await self._knowledge.retrieve(
                "Что такое Travel Advantage и какие услуги доступны"
            )
            ta_facts = tuple(
                f"Travel Advantage — {item.content} [источник: {item.source_ref}]"
                for item in bundle.primary_items[:3]
            )
        else:
            # Independent workspace: no Travel Advantage knowledge lookup
            # at all, not just an unpopulated comparison - see the
            # isolation audit this fixes.
            ta_facts = ()
        ta_link = ta_facts[0] if ta_facts else None
        opportunities = _opportunities(competitor.id, analyses, ta_link)
        all_facts = tuple(fact for _, a in analyses for fact in a.key_facts)
        summaries = tuple(a.summary for _, a in analyses)

        return CompetitorIntelligence(
            competitor_id=competitor.id,
            competitor_label=competitor.label,
            analyzed_at=discovered_at,
            positioning=summaries[:2],
            products=_matching(all_facts, "hotel", "flight", "train", "car", "cruise", "tour", "booking"),
            destinations_and_categories=_matching(all_facts, "destination", "city", "country", "travel", "hotel", "flight"),
            promotions=_matching(all_facts, "deal", "discount", "promo", "coupon", "sale", "offer"),
            loyalty_mechanics=_matching(all_facts, "member", "loyal", "coin", "reward", "tier", "perk"),
            service_and_ux=_matching(all_facts, "app", "service", "support", "search", "flex", "ai", "booking"),
            strengths=tuple(dict.fromkeys((*summaries, *all_facts)))[:5],
            travel_advantage_comparison=ta_facts,
            fresh_signals=tuple(
                f"{source.title}: {analysis.summary}"
                for source, analysis in analyses
                if _source_freshness(source, signal_dates)
            )[:5],
            sources=tuple(evidence),
            opportunities=opportunities,
            data_origin=data_origin,
        )

    async def _relevant_recent_signals(
        self, competitor: Competitor,
    ) -> list[WorkspaceSignalRecord]:
        """Genuinely matching, genuinely recent Radar signals already
        visible to this workspace - never a bulk dump of unrelated travel
        news (see _signal_matches_competitor). Never raises: a Radar/DB
        hiccup here must not break the whole competitor-analysis flow, it
        just means no fallback evidence is available (falls through to the
        precise refusal in analyze())."""
        if self._workspace_signal_repository is None:
            return []
        try:
            records = await self._workspace_signal_repository.list_for_workspace(
                competitor.workspace_id, limit=200,
            )
        except Exception:
            return []

        domain = canonical_domain(competitor.url)
        label = competitor.label.strip()
        label_pattern = (
            _label_regex(label) if len(label) >= _MIN_LABEL_MATCH_LENGTH else None
        )
        matched = [
            record for record in records
            if _signal_matches_competitor(record, domain=domain, label_pattern=label_pattern)
            and _signal_is_recent(record.raw_created_at)
        ]
        matched.sort(key=lambda record: record.raw_created_at, reverse=True)
        return matched[:_MAX_SIGNAL_FALLBACK_SOURCES]


def _candidate_urls(url: str) -> tuple[str, ...]:
    parts = urlsplit(url.strip())
    labels = (parts.hostname or "").split(".")
    if len(labels) >= 3 and (labels[0] == "www" or len(labels[0]) <= 3):
        root_host = ".".join(labels[1:])
    else:
        root_host = parts.hostname or ""
    origin = urlunsplit((parts.scheme or "https", f"www.{root_host}", "", "", ""))
    candidates = (
        url.strip(),
        origin + "/blog",
        origin + "/guide/all-content/",
        origin + "/newsroom/",
        origin + "/customer/loyalty",
    )
    return tuple(dict.fromkeys(candidates))


def _freshness(text: str) -> str | None:
    match = _DATE_RE.search(text)
    return match.group(0) if match else None


def _source_freshness(source: FetchedPublicSource, signal_dates: dict[str, str]) -> str | None:
    """Prefers a Radar signal's real recorded date (known, exact) over
    _freshness()'s in-text date guess, which rarely finds anything in a
    short Radar title/summary - used identically for a source's own
    evidence.freshness and for the fresh_signals summary list, so both stay
    consistent about what counts as fresh."""
    real_date = signal_dates.get(source.final_url)
    if real_date:
        return real_date[:10]
    return _freshness(source.text)


def _label_regex(label: str) -> re.Pattern[str] | None:
    escaped = re.escape(label)
    return re.compile(rf"\b{escaped}\b", re.IGNORECASE) if escaped else None


def _signal_matches_competitor(
    record: WorkspaceSignalRecord, *, domain: str, label_pattern: re.Pattern[str] | None,
) -> bool:
    """Genuinely matching, not "any travel news": either the signal's own
    URL resolves to the competitor's canonical domain, or the competitor's
    label appears as a whole word in the signal's title/summary/source
    name. A short/generic label (see _MIN_LABEL_MATCH_LENGTH) never matches
    by text alone - only by domain."""
    if domain and canonical_domain(record.item_url) == domain:
        return True
    if label_pattern is None:
        return False
    haystack = f"{record.item_title} {record.item_summary} {record.source_name}"
    return bool(label_pattern.search(haystack))


def _signal_is_recent(raw_created_at: str, *, days: int = _SIGNAL_FRESH_DAYS) -> bool:
    raw = (raw_created_at or "").strip()
    if not raw:
        return False
    try:
        created_date = date.fromisoformat(raw[:10])
    except ValueError:
        return False
    return created_date >= date.today() - timedelta(days=days)


def _sources_from_signals(records: list[WorkspaceSignalRecord]) -> list[FetchedPublicSource]:
    """Reshapes matched Radar signals into the same FetchedPublicSource
    shape a direct site fetch produces, so the rest of analyze() (LLM
    analysis, fact-matching, opportunity extraction) needs no separate code
    path for the fallback case - this IS the "reuse existing
    infrastructure" the fallback is built on."""
    sources: list[FetchedPublicSource] = []
    seen: set[str] = set()
    for record in records:
        final_url = record.item_url or f"radar-signal:{record.interpretation_id}"
        if final_url in seen:
            continue
        text = "\n".join(
            part for part in (record.item_title, record.item_summary) if part
        ).strip()
        if not text:
            continue
        seen.add(final_url)
        sources.append(FetchedPublicSource(
            url=record.item_url or final_url,
            final_url=final_url,
            title=record.item_title or record.source_name or "Radar-сигнал",
            text=text,
            content_type="text/plain",
        ))
    return sources


def _fallback_analysis(source: FetchedPublicSource) -> SourceAnalysisPayload:
    lines = tuple(dict.fromkeys(
        line.strip() for line in source.text.splitlines()
        if 25 <= len(line.strip()) <= 240
    ))
    facts = lines[:8] or (source.text[:240],)
    angles = _fallback_angles(source)
    return SourceAnalysisPayload(
        summary=(
            f"Публичная страница «{source.title or source.final_url}» содержит "
            "актуальные продукты, направления и темы конкурента."
        ),
        key_facts=facts,
        disputed_claims=(),
        audience_value=(
            "Источник показывает, какие travel-задачи и информационные поводы "
            "конкурент считает важными для путешественников."
        ),
        target_audiences=("путешественники",),
        content_angles=angles,
        recommended_formats=("post",),
        warnings=("Автоматический LLM-разбор недоступен; использована bounded текстовая проекция.",),
    )


def _fallback_angles(source: FetchedPublicSource) -> tuple[str, ...]:
    lower = f"{source.title}\n{source.text}".lower()
    rules = (
        (("promo", "discount", "deal", "coupon", "sale"), "Как находить и проверять актуальные travel-акции"),
        (("guide", "destination", "travel", "city"), "Сезонные направления и практические советы путешественникам"),
        (("visa", "airport", "transit", "entry"), "Что проверить до поездки: документы, аэропорты и транзит"),
        (("member", "loyal", "reward", "coin", "tier"), "Как loyalty-механики влияют на выбор travel-сервиса"),
        (("app", " ai ", "search", "booking"), "Какие UX-функции упрощают планирование и бронирование"),
        (("news", "data", "growth", "trend"), "Новые сигналы в поведении путешественников"),
    )
    selected = [title for words, title in rules if any(word in lower for word in words)]
    if not selected:
        selected.append("Что этот публичный материал говорит о запросах путешественников")
    return tuple(selected[:5])


def _matching(facts: tuple[str, ...], *keywords: str) -> tuple[str, ...]:
    selected = [fact for fact in facts if any(word in fact.lower() for word in keywords)]
    return tuple(dict.fromkeys(selected))[:6]


def _opportunities(competitor_id: int, analyses, ta_link: str | None) -> tuple[ContentOpportunity, ...]:
    candidates: list[tuple[int, int, FetchedPublicSource, SourceAnalysisPayload, str, str]] = []
    sequence = 0
    for source, analysis in analyses:
        theses = analysis.key_facts or (analysis.summary,)
        for raw_thesis in theses:
            for fragment in _expand_composite(raw_thesis):
                clean = " ".join(fragment.split())
                if not clean or _is_noise(clean) or _is_generic_fragment(clean):
                    continue
                category = _opportunity_category(clean)
                if category is None:
                    continue
                category_index, category_name = category
                candidates.append((category_index, sequence, source, analysis, clean, category_name))
                sequence += 1

    # One material/story keeps at most one opportunity: within a single
    # source, several raw facts often describe the same underlying article.
    # The strongest (most headline-like) phrasing wins the (source, category)
    # slot instead of every raw fact becoming its own weak opportunity.
    best_per_material: dict[tuple[str, int], tuple[int, int, FetchedPublicSource, SourceAnalysisPayload, str, str]] = {}
    for entry in candidates:
        category_index, _, source, _, thesis, _ = entry
        key = (source.final_url, category_index)
        current = best_per_material.get(key)
        if current is None or _title_strength(thesis) > _title_strength(current[4]):
            best_per_material[key] = entry

    grouped: dict[int, list[tuple[int, int, FetchedPublicSource, SourceAnalysisPayload, str, str]]] = {}
    for entry in sorted(best_per_material.values(), key=lambda item: item[1]):
        grouped.setdefault(entry[0], []).append(entry)
    category_order = sorted(grouped)

    result: list[ContentOpportunity] = []
    fingerprints: list[frozenset[str]] = []
    round_index = 0
    while len(result) < _MAX_OPPORTUNITIES and any(round_index < len(grouped[c]) for c in category_order):
        for category_index in category_order:
            if len(result) >= _MAX_OPPORTUNITIES:
                break
            items = grouped[category_index]
            if round_index >= len(items):
                continue
            _, _, source, analysis, thesis, category_name = items[round_index]
            fingerprint = _semantic_fingerprint(thesis)
            if not fingerprint or any(_semantic_duplicate(fingerprint, seen) for seen in fingerprints):
                continue
            fingerprints.append(fingerprint)
            entity = _core_entity(thesis)
            topic = f"{category_name}: {entity}"[:90]
            angle = _ANGLE_TEMPLATES[category_name].format(entity=entity)
            result.append(ContentOpportunity(
                id=f"opp-{len(result) + 1}", competitor_id=competitor_id,
                topic=topic, source_title=source.title or source.final_url,
                source_url=source.final_url, freshness=_freshness(source.text),
                key_thesis=thesis, audience_value=analysis.audience_value,
                own_post_angle=angle, travel_advantage_link=ta_link,
            ))
        round_index += 1
    return tuple(result)


def _opportunity_category(thesis: str) -> tuple[int, str] | None:
    normalized = f" {thesis.lower()} "
    for index, (category, markers) in enumerate(_OPPORTUNITY_CATEGORIES):
        if any(marker in normalized for marker in markers):
            return index, category
    return None


def _is_noise(thesis: str) -> bool:
    lower = thesis.lower().strip()
    if len(lower) < 12:
        return True
    if lower.startswith(("http://", "https://")):
        return True
    if lower.startswith("указано, что"):
        return True
    return any(pattern in lower for pattern in _NOISE_SUBSTRINGS)


def _is_generic_fragment(fragment: str) -> bool:
    if len(fragment) < 4:
        return True
    return not re.search(r"[A-ZА-ЯЁ][a-zа-яё]", fragment) and len(fragment) < 25


def _expand_composite(thesis: str) -> tuple[str, ...]:
    """A single raw fact sometimes lists an entire page's worth of stories
    ("темы материалов включают X, Y, Z..."). Splitting it lets each real
    story compete on its own merits instead of shipping as one unreadable
    opportunity or being dropped outright as noise."""
    lower = thesis.lower()
    marker = next((m for m in _COMPOSITE_MARKERS if m in lower), None)
    if marker is None:
        return (thesis,)
    tail = thesis[lower.index(marker) + len(marker):]
    segments = tuple(seg.strip(" .\n") for seg in tail.split(","))
    segments = tuple(seg for seg in segments if seg)
    return segments or (thesis,)


def _title_strength(thesis: str) -> float:
    """Prefers headline-like phrasing ("2026 Guide to Shanghai Pudong
    Airport") over narrative filler sentences describing the same story, so
    the one-opportunity-per-material rule keeps the more usable title."""
    words = [w for w in re.findall(r"[A-Za-zА-Яа-яЁё]+", thesis) if len(w) > 3]
    if not words:
        return 0.0
    title_case_ratio = sum(1 for w in words if w[0].isupper()) / len(words)
    digit_bonus = 0.1 if re.search(r"\d", thesis) else 0.0
    length_penalty = len(thesis) / 1000
    return title_case_ratio + digit_bonus - length_penalty


def _core_entity(thesis: str) -> str:
    """Extracts the proper-noun phrase (the longest run of capitalized
    words, tolerating short connectors like "vs"/"to") to use as a short,
    concrete anchor for the editorial topic/angle instead of the full raw
    sentence."""
    words = thesis.split()
    best: list[str] = []
    current: list[str] = []
    for word in words:
        core = word.strip(".,;:!?()&\"'")
        is_proper = bool(core) and (core[0].isupper() or core.isupper())
        is_connector = core.lower() in _CONNECTOR_WORDS
        if is_proper or (is_connector and current):
            current.append(core)
        else:
            if len(current) > len(best):
                best = current
            current = []
    if len(current) > len(best):
        best = current
    if len(best) >= 2:
        return " ".join(best[:6])
    return " ".join(words[:8]).strip(" .,;:")


def _semantic_fingerprint(value: str) -> frozenset[str]:
    tokens = re.findall(r"[0-9a-zа-яё]+", value.lower())
    return frozenset(token for token in tokens if len(token) > 2 and token not in _DEDUP_STOP_WORDS)


def _semantic_duplicate(first: frozenset[str], second: frozenset[str]) -> bool:
    if first == second:
        return True
    union = first | second
    return bool(union) and len(first & second) / len(union) >= 0.60
