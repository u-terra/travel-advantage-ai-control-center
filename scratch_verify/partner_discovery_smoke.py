"""Partner Discovery 2.0 smoke test - run on a server where real Yandex
Search + OpenAI credentials are already configured (production .env).

WHAT THIS DOES:
- Enables partner_discovery_v2 ONLY for this one-off process via
  os.environ, set INSIDE this script before any app.* import - never
  touches the real .env file, never committed, nothing persisted.
- Runs the pipeline directly (not via the Telegram handler/web_api wiring,
  which is NOT deployed yet): PartnerDiscoveryService.find_candidates(
  rotation_seed=...) -> enrich_candidates() -> rank_for_club_partner_fit()
  -> qualify_candidates(min_score, min_confidence, max_results). Uses the
  CURRENT production find_candidates(*, rotation_seed=None) signature -
  deliberately does NOT pass use_all_variants (that parameter only exists
  in the local, undeployed app/services/partner_discovery.py).
- Captures the pipeline's own structured logs (partner_discovery.*
  events already emitted by app/services/partner_discovery.py and
  app/services/lead_qualification.py) to report the funnel counts the
  task asked for (raw results / after dedup / qualified / rejected
  reasons) WITHOUT re-implementing any counting logic separately - the
  numbers come from the exact same log lines production would emit.
- Prints a human-readable card per final candidate (the same fields
  Telegram's _render_card shows) plus a summary table.

HOW TO RUN (on the server that already has YANDEX_SEARCH_API_KEY/
YANDEX_SEARCH_FOLDER_ID/WEB_SEARCH_ENABLED=true/WEB_SEARCH_PROVIDER=yandex
and PLANNER_OPENAI_API_KEY already set in its real .env):

    cd /path/to/travel-advantage-ai-control-center
    python partner_discovery_smoke_test.py

No flags to pass - PARTNER_DISCOVERY_V2 is forced on only inside this
process's own os.environ, before app.config is imported. Nothing in this
repo's committed .env or config.py is touched. Safe to run repeatedly;
PartnerDiscoveryService.find_candidates never writes anywhere.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

# Must happen BEFORE any `app.*` import - app.config reads os.environ at
# import/call time via load_settings(), so this has to come first.
os.environ.setdefault("PARTNER_DISCOVERY_V2", "true")
os.environ.setdefault("PARTNER_MIN_SCORE", "65")
os.environ.setdefault("PARTNER_MIN_CONFIDENCE", "0.60")
os.environ.setdefault("PARTNER_MAX_RESULTS", "5")

# Run this script from the repository root (where app/ lives) - e.g.:
#   cd /path/to/travel-advantage-ai-control-center
#   python partner_discovery_smoke_test.py


class _CapturingHandler(logging.Handler):
    """Collects every partner_discovery.* / qualification log line this
    run emits, so the funnel counts below are read from the SAME
    structured events production logging already produces - not a
    second, separately-computed count that could drift from reality."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def _extract_count(lines: list[str], pattern: str) -> int | None:
    rx = re.compile(pattern)
    for line in lines:
        m = rx.search(line)
        if m:
            return int(m.group(1))
    return None


def _count_rejections(lines: list[str], reason: str) -> int:
    rx = re.compile(rf"partner_discovery\.rejected reason={reason}\b")
    return sum(1 for line in lines if rx.search(line))


async def main() -> None:
    from app.config import load_settings
    from app.chat_provider import ChatConfig, OpenAIChatProvider
    from app.services.web_search.factory import create_web_search_service
    from app.services.partner_discovery import PartnerDiscoveryService
    from app.services.partner_outreach import (
        candidate_to_dict,
        enrich_candidates,
        rank_for_club_partner_fit,
    )
    from app.services.lead_qualification import qualify_candidates

    settings = load_settings()

    print("=" * 70)
    print("CONFIG CHECK (read-only, nothing modified)")
    print("=" * 70)
    print(f"web_search_enabled        = {settings.web_search_enabled}")
    print(f"web_search_provider       = {settings.web_search_provider!r}")
    print(f"yandex_api_key configured = {bool(settings.yandex_search_api_key)}")
    print(f"yandex_folder_id set      = {bool(settings.yandex_search_folder_id)}")
    print(f"partner_discovery_v2      = {settings.partner_discovery_v2_enabled}")
    print(f"partner_min_score         = {settings.partner_min_score}")
    print(f"partner_min_confidence    = {settings.partner_min_confidence}")
    print(f"partner_max_results       = {settings.partner_max_results}")
    print()

    web_search_service = create_web_search_service(settings)
    if web_search_service.provider is None:
        print("ABORTING: web_search_service.provider is None - this server's "
              "Yandex Search credentials are not actually configured/working. "
              "Fix WEB_SEARCH_ENABLED/WEB_SEARCH_PROVIDER/YANDEX_SEARCH_* in "
              "the real .env (not this script) and re-run.")
        return

    llm_provider = None
    if settings.planner_openai_api_key:
        llm_provider = OpenAIChatProvider(
            ChatConfig(
                api_key=settings.planner_openai_api_key,
                model="gpt-5.6-terra",
                timeout_seconds=60,
            )
        )
    else:
        print("WARNING: no PLANNER_OPENAI_API_KEY configured - qualification "
              "will run on the deterministic FALLBACK scorer, not the LLM "
              "path. Results will be cruder than production with a real key.")
    print()

    capture = _CapturingHandler()
    logging.getLogger("app.services.partner_discovery").addHandler(capture)
    logging.getLogger("app.services.lead_qualification").addHandler(capture)
    logging.getLogger("app.services.partner_discovery").setLevel(logging.INFO)
    logging.getLogger("app.services.lead_qualification").setLevel(logging.INFO)

    service = PartnerDiscoveryService(web_search_service.provider)
    rotation_seed = 0  # deterministic/no-rotation query set for reproducibility
    # NOTE (2026-10-11): kept compatible with the CURRENT production
    # find_candidates(*, rotation_seed=None) signature - this server's
    # deployed app/services/partner_discovery.py does not have the
    # use_all_variants parameter (that change was never deployed, out of
    # scope for this fix). Do not add it back without deploying that file.
    print("Running PartnerDiscoveryService.find_candidates(rotation_seed=...) ...")
    prospects = await service.find_candidates(rotation_seed=rotation_seed)
    candidates = [candidate_to_dict(p) for p in prospects]
    print(f"-> {len(candidates)} candidates survived dedupe/noise-filter/page-verify.\n")

    if not candidates:
        print("No candidates at all - nothing further to qualify. Report this "
              "as a real finding, not a bug to patch during the smoke test.")
        return

    candidates = await enrich_candidates(candidates, llm_provider, ta_affiliated=True)
    candidates = rank_for_club_partner_fit(candidates)

    pre_qualification_count = len(candidates)
    qualified = await qualify_candidates(
        candidates, llm_provider,
        min_score=settings.partner_min_score,
        min_confidence=settings.partner_min_confidence,
        max_results=settings.partner_max_results,
    )

    raw_results = _extract_count(capture.lines, r"search_started query_count=(\d+)")
    results_received = _extract_count(capture.lines, r"results_received raw_count=(\d+)")
    deduped = _extract_count(capture.lines, r"deduplicated verified_count=\d+ deduped_count=(\d+)")
    rejected_low_score = _count_rejections(capture.lines, "low_score")
    rejected_low_confidence = _count_rejections(capture.lines, "low_confidence")
    with_contact = sum(1 for lead in qualified if lead.get("contact_type"))

    print("=" * 70)
    print("FUNNEL")
    print("=" * 70)
    print(f"Search queries sent                 : {raw_results}")
    print(f"Raw search results (post-dedupe-key) : {results_received}")
    print(f"After identity dedupe                : {deduped}")
    print(f"Entered qualification                : {pre_qualification_count}")
    print(f"Rejected: low_score                  : {rejected_low_score}")
    print(f"Rejected: low_confidence              : {rejected_low_confidence}")
    print(f"Final qualified leads                : {len(qualified)}")
    print(f"  ...of which have a real contact     : {with_contact}")
    print()

    print("=" * 70)
    print(f"FINAL {len(qualified)} CANDIDATES")
    print("=" * 70)
    for i, lead in enumerate(qualified, 1):
        name = lead.get("display_name") or lead.get("name")
        contact_type = lead.get("contact_type")
        contact_line = (
            f"{contact_type} | value={lead.get('contact_value')} | url={lead.get('contact_url')}"
            if contact_type else "НЕ НАЙДЕН"
        )
        sources = [lead["url"]] + [e["source_url"] for e in (lead.get("evidence_claims") or [])]
        sources = list(dict.fromkeys(sources))  # dedupe, keep order
        print(f"\n--- Candidate {i}/{len(qualified)} ---")
        print(f"Name          : {name}")
        print(f"Category      : {lead.get('prospect_type')}")
        print(f"Score         : {lead.get('total_score')}/100")
        print(f"Confidence    : {lead.get('confidence')}")
        print("Why fit:")
        for item in lead.get("why_fit") or []:
            print(f"  - {item}")
        print(f"Hook          : {lead.get('hook') or '(none)'}")
        print(f"Contact       : {contact_line}")
        print(f"Contact source: {lead.get('contact_source_url') or '(none)'}")
        print(f"Sources       : {sources}")
        print(f"Risk flags    : {lead.get('risk_flags') or []}")
        print(f"Raw evidence excerpt (for manual verification): {lead.get('evidence')[:300]!r}")

    print("\n" + "=" * 70)
    print("RAW CAPTURED LOG LINES (for independent verification)")
    print("=" * 70)
    for line in capture.lines:
        print(line)


if __name__ == "__main__":
    asyncio.run(main())
