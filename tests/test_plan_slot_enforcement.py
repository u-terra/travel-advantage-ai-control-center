"""Plan slot enforcement for competitors and monitoring sources: atomic
check+insert shared by every write path, deterministic default pack,
approval that never records an approval without a connection, fail-closed
plan resolution, workspace isolation, and untouched existing over-limit data.

Real sqlite files, real repositories - no mocking of the DB layer.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.competitor_discovery import (
    CandidateClassification,
    CandidateConfidence,
    CandidateStatus,
)
from app.domain.subscription import SubscriptionPlan
from app.keyboards import COMPETITOR_DISCOVERY_ADD_PREFIX
from app.repositories import slot_guard
from app.repositories.competitor_repository import CompetitorRepository
from app.repositories.partner_repository import PartnerRepository
from app.repositories.slot_guard import (
    SLOT_COMPETITORS,
    SLOT_SOURCES,
    SlotLimitReached,
)
from app.repositories.source_catalog_repository import (
    DEFAULT_SOURCE_PACK_IDS,
    SourceCatalogRepository,
)
from app.repositories.subscription_repository import SubscriptionRepository
from app.services.source_registry import SEED_REGISTRY_PATH, load_registry


def run(coro):
    return asyncio.run(coro)


def _legacy_file(path: Path, sources: list[dict] | None = None) -> Path:
    path.write_text(json.dumps({"schema_version": 2, "sources": sources or []}), encoding="utf-8")
    return path


def _web_source(source_id: str, priority: int = 40) -> dict:
    return {
        "id": source_id, "name": source_id, "platform": "web",
        "source_type": "monitored_source", "purpose": "content", "enabled": True,
        "priority": priority, "notes": "", "url": f"https://example.com/{source_id}",
    }


class Env:
    """One db with partner/subscription/competitor/source repos, all enforcing."""

    def __init__(self, tmp_path: Path, *, enforce: bool = True) -> None:
        self.tmp = tmp_path
        self.db = tmp_path / "app.sqlite3"
        self.partners = PartnerRepository(self.db)
        run(self.partners.init())
        self.owner = run(self.partners.bootstrap_owner_membership(100)).workspace_id
        self.subs = SubscriptionRepository(self.db)
        run(self.subs.init())  # backfill: every existing workspace is 'beta'
        self.sources = SourceCatalogRepository(
            self.db, tmp_path / "projection.json", enforce_plan_slots=enforce,
        )
        run(self.sources.init(self.owner, legacy_path=_legacy_file(tmp_path / "legacy.json")))
        self.competitors = CompetitorRepository(self.db, enforce_plan_slots=enforce)
        run(self.competitors.init())

    def workspace(self, plan: SubscriptionPlan | None, slug: str, user: int) -> int:
        """A new workspace on ``plan``; plan=None leaves it WITHOUT a subscription row."""
        with sqlite3.connect(self.db) as db:
            cursor = db.execute(
                "INSERT INTO partner_workspaces(name, slug, status, created_at, updated_at) "
                "VALUES (?, ?, 'active', 'now', 'now')", (slug, slug),
            )
            ws = int(cursor.lastrowid)
            db.execute(
                "INSERT INTO workspace_memberships "
                "(workspace_id, telegram_user_id, role, status, created_at, updated_at) "
                "VALUES (?, ?, 'owner', 'active', 'now', 'now')", (ws, user),
            )
        if plan is not None:
            paid = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
            run(self.subs.mark_paid(
                ws, external_payment_id=f"o{ws}", payment_provider="robokassa",
                paid_until=paid, plan=plan,
            ))
        return ws

    def count(self, table: str, ws: int) -> int:
        column = "workspace_id"
        with sqlite3.connect(self.db) as db:
            return db.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (ws,)).fetchone()[0]

    def fill_sources(self, ws: int, n: int) -> list[str]:
        ids = []
        for i in range(n):
            result = run(self.sources.add_source(ws, f"https://fill{ws}-{i}.example.org/"))
            ids.append(result.source.id)
        return ids

    def raw_competitors(self, ws: int, n: int) -> None:
        with sqlite3.connect(self.db) as db:
            for i in range(n):
                db.execute(
                    "INSERT INTO competitors (workspace_id, url, label, created_at) "
                    "VALUES (?, ?, ?, 'now')", (ws, f"https://c{ws}-{i}.example.com", f"c{i}"),
                )

    def raw_sources(self, ws: int, n: int) -> None:
        with sqlite3.connect(self.db) as db:
            for i in range(n):
                sid = f"raw_{ws}_{i}"
                db.execute(
                    "INSERT INTO source_catalog (id, identity_key, name, platform, url, "
                    "username, source_type, purpose, priority, notes, collector_json, "
                    "visibility, owner_workspace_id, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'web', ?, NULL, 'monitored_source', 'mixed', 50, '', "
                    "'{}', 'private', ?, 'active', 'now', 'now')",
                    (sid, f"web:{sid}", sid, f"https://{sid}.example.com", ws),
                )
                db.execute(
                    "INSERT INTO workspace_source_subscriptions "
                    "(workspace_id, source_id, enabled, usage_role, created_at, updated_at) "
                    "VALUES (?, ?, 1, 'monitoring', 'now', 'now')", (ws, sid),
                )


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


# ── 1. first activation: the default pack fits the tariff ───────────────────

_PACK_PRIORITIES = {"p1": 50, "p2": 10, "p3": 30, "p4": 20, "p5": 90,
                    "p6": 40, "p7": 60, "p8": 80, "p9": 70}


def _pack_seed(tmp: Path) -> Path:
    return _legacy_file(
        tmp / "pack.json", [_web_source(i, p) for i, p in _PACK_PRIORITIES.items()],
    )


def _connected(env: Env, ws: int) -> set[str]:
    return {s.id for s in run(env.sources.list_for_workspace(ws))}


@pytest.mark.parametrize(
    "plan,expected_count",
    [(SubscriptionPlan.START, 5), (SubscriptionPlan.STANDARD, 9), (SubscriptionPlan.FULL, 9)],
)
def test_default_pack_fits_the_tariff(env: Env, plan, expected_count) -> None:
    ws = env.workspace(plan, f"w-{plan.value}", 200)
    seed = _pack_seed(env.tmp)

    connected = run(env.sources.assign_default_sources(ws, tuple(_PACK_PRIORITIES), seed_path=seed))

    assert connected == expected_count
    assert env.count("workspace_source_subscriptions", ws) == expected_count


def test_start_activation_connects_the_five_highest_priority_sources_deterministically(
    env: Env,
) -> None:
    seed = _pack_seed(env.tmp)
    expected = {"p2", "p4", "p3", "p6", "p1"}  # priorities 10, 20, 30, 40, 50
    first = env.workspace(SubscriptionPlan.START, "a", 201)
    second = env.workspace(SubscriptionPlan.START, "b", 202)

    # caller order must not matter - selection is by the catalog priority
    run(env.sources.assign_default_sources(first, tuple(_PACK_PRIORITIES), seed_path=seed))
    run(env.sources.assign_default_sources(
        second, tuple(reversed(tuple(_PACK_PRIORITIES))), seed_path=seed,
    ))

    assert _connected(env, first) == expected
    assert _connected(env, second) == expected


def test_start_activation_with_the_real_default_pack_takes_five_by_priority(env: Env) -> None:
    ws = env.workspace(SubscriptionPlan.START, "real", 203)
    registry = load_registry(SEED_REGISTRY_PATH, use_cache=False)
    by_priority = sorted(
        (s for s in registry if s.id in DEFAULT_SOURCE_PACK_IDS),
        key=lambda s: (s.priority, s.id),
    )

    run(env.sources.assign_default_sources(ws))

    assert _connected(env, ws) == {s.id for s in by_priority[:5]}
    assert env.count("workspace_source_subscriptions", ws) == 5


def test_default_pack_is_idempotent_and_never_exceeds_the_slot_limit(env: Env) -> None:
    ws = env.workspace(SubscriptionPlan.START, "idem", 204)
    seed = _pack_seed(env.tmp)
    run(env.sources.assign_default_sources(ws, tuple(_PACK_PRIORITIES), seed_path=seed))

    again = run(env.sources.assign_default_sources(ws, tuple(_PACK_PRIORITIES), seed_path=seed))

    assert again == 0
    assert env.count("workspace_source_subscriptions", ws) == 5


def test_default_pack_counts_sources_the_workspace_already_has(env: Env) -> None:
    ws = env.workspace(SubscriptionPlan.START, "partial", 205)
    seed = _pack_seed(env.tmp)
    env.fill_sources(ws, 2)  # 2 own sources -> only 3 slots left for the pack

    run(env.sources.assign_default_sources(ws, tuple(_PACK_PRIORITIES), seed_path=seed))

    assert env.count("workspace_source_subscriptions", ws) == 5
    pack_connected = _connected(env, ws) & set(_PACK_PRIORITIES)
    assert pack_connected == {"p2", "p4", "p3"}


def test_default_pack_takes_no_slot_for_a_source_already_connected(env: Env) -> None:
    ws = env.workspace(SubscriptionPlan.START, "known", 206)
    seed = _pack_seed(env.tmp)
    run(env.sources.assign_default_sources(ws, ("p2", "p4", "p3", "p6"), seed_path=seed))
    run(env.sources.set_enabled(ws, "p2", False))  # the user disabled one

    run(env.sources.assign_default_sources(ws, tuple(_PACK_PRIORITIES), seed_path=seed))

    assert env.count("workspace_source_subscriptions", ws) == 5
    assert not next(s for s in run(env.sources.list_for_workspace(ws)) if s.id == "p2").enabled


def test_default_pack_without_a_subscription_connects_nothing(env: Env) -> None:
    ws = env.workspace(None, "nosub", 207)
    seed = _pack_seed(env.tmp)

    with pytest.raises(SlotLimitReached) as err:
        run(env.sources.assign_default_sources(ws, tuple(_PACK_PRIORITIES), seed_path=seed))

    assert "подписка не найдена" in err.value.message
    assert env.count("workspace_source_subscriptions", ws) == 0


# ── 2. approval with a full limit ───────────────────────────────────────────


def _request(env: Env, ws: int, address: str, user: int):
    return run(env.sources.submit_source_request(ws, user, address)).request


def test_approval_with_a_full_limit_is_refused_and_the_request_stays_pending(env: Env) -> None:
    ws = env.workspace(SubscriptionPlan.START, "full", 210)
    env.fill_sources(ws, 5)
    request = _request(env, ws, "https://new-one.example.net/", 210)

    with pytest.raises(SlotLimitReached) as err:
        run(env.sources.approve_source_request(request.id, ws))

    assert "5 источников" in err.value.message and "START" in err.value.message
    stored = run(env.sources.get_source_request(request.id))
    assert stored.status == "pending" and stored.reviewed_at is None
    assert env.count("workspace_source_subscriptions", ws) == 5


def test_approval_succeeds_after_a_slot_is_freed(env: Env) -> None:
    ws = env.workspace(SubscriptionPlan.START, "free", 211)
    ids = env.fill_sources(ws, 5)
    request = _request(env, ws, "https://wait.example.net/", 211)
    with pytest.raises(SlotLimitReached):
        run(env.sources.approve_source_request(request.id, ws))
    with sqlite3.connect(env.db) as db:
        db.execute("DELETE FROM workspace_source_subscriptions WHERE workspace_id=? AND source_id=?",
                   (ws, ids[0]))

    approved = run(env.sources.approve_source_request(request.id, ws))

    assert approved.status == "approved"
    assert env.count("workspace_source_subscriptions", ws) == 5


def test_approval_on_beta_is_unlimited(env: Env) -> None:
    ws = env.workspace(SubscriptionPlan.BETA, "beta", 212)
    env.fill_sources(ws, 8)
    request = _request(env, ws, "https://beta-extra.example.net/", 212)

    assert run(env.sources.approve_source_request(request.id, ws)).status == "approved"
    assert env.count("workspace_source_subscriptions", ws) == 9


# ── 3. re-connecting an existing source costs no slot ───────────────────────


def test_reconnecting_an_existing_source_takes_no_slot_at_a_full_limit(env: Env) -> None:
    ws = env.workspace(SubscriptionPlan.START, "re", 220)
    ids = env.fill_sources(ws, 5)
    run(env.sources.set_enabled(ws, ids[0], False))  # disabled still occupies its slot

    again = run(env.sources.add_source(ws, "https://fill{}-0.example.org/".format(ws)))

    assert again.outcome == "already_disabled"
    assert env.count("workspace_source_subscriptions", ws) == 5
    run(env.sources.set_enabled(ws, ids[0], True))  # toggling back works too


def test_approving_a_request_for_an_already_connected_source_takes_no_slot(env: Env) -> None:
    ws = env.workspace(SubscriptionPlan.START, "reapp", 221)
    # the request exists first (a free slot at that time), then the source
    # gets connected another way, is disabled, and the limit fills up
    request = _request(env, ws, "https://shared.example.net/", 221)
    run(env.sources.add_source(ws, "https://shared.example.net/"))
    run(env.sources.set_enabled(ws, request.source_id, False))
    env.fill_sources(ws, 4)
    assert env.count("workspace_source_subscriptions", ws) == 5

    approved = run(env.sources.approve_source_request(request.id, ws))

    assert approved.status == "approved"
    assert env.count("workspace_source_subscriptions", ws) == 5
    assert next(s for s in run(env.sources.list_for_workspace(ws)) if s.id == request.source_id).enabled


# ── 4. parallel adds into the last free slot ────────────────────────────────


def test_parallel_source_adds_cannot_exceed_the_last_free_slot(env: Env) -> None:
    ws = env.workspace(SubscriptionPlan.START, "race-s", 230)
    env.fill_sources(ws, 4)  # one slot left

    async def go():
        return await asyncio.gather(
            *(env.sources.add_source(ws, f"https://race-{i}.example.net/") for i in range(8)),
            return_exceptions=True,
        )

    results = run(go())

    ok = [r for r in results if not isinstance(r, BaseException)]
    refused = [r for r in results if isinstance(r, SlotLimitReached)]
    assert len(ok) == 1 and len(refused) == 7
    assert env.count("workspace_source_subscriptions", ws) == 5


def test_parallel_competitor_adds_cannot_exceed_the_last_free_slot(env: Env) -> None:
    ws = env.workspace(SubscriptionPlan.START, "race-c", 231)
    env.raw_competitors(ws, 2)  # START allows 3 -> one slot left

    async def go():
        return await asyncio.gather(
            *(env.competitors.add_competitor(ws, f"https://r{i}.example.com") for i in range(8)),
            return_exceptions=True,
        )

    results = run(go())

    assert sum(1 for r in results if not isinstance(r, BaseException)) == 1
    assert sum(1 for r in results if isinstance(r, SlotLimitReached)) == 7
    assert env.count("competitors", ws) == 3


def test_parallel_approvals_cannot_exceed_the_last_free_slot(env: Env) -> None:
    ws = env.workspace(SubscriptionPlan.START, "race-a", 232)
    env.fill_sources(ws, 4)
    requests = [_request(env, ws, f"https://appr-{i}.example.net/", 232) for i in range(4)]

    async def go():
        return await asyncio.gather(
            *(env.sources.approve_source_request(r.id, ws) for r in requests),
            return_exceptions=True,
        )

    results = run(go())

    assert sum(1 for r in results if not isinstance(r, BaseException)) == 1
    assert env.count("workspace_source_subscriptions", ws) == 5
    statuses = sorted(run(env.sources.get_source_request(r.id)).status for r in requests)
    assert statuses == ["approved", "pending", "pending", "pending"]  # no approval without a connection


# ── 5. competitor limits in Telegram ────────────────────────────────────────


def _message(text: str) -> SimpleNamespace:
    return SimpleNamespace(text=text, answer=AsyncMock())


def test_telegram_manual_add_is_refused_at_the_limit_even_without_the_precheck(env: Env) -> None:
    from app.handlers.competitors import receive_competitor_url

    ws = env.workspace(SubscriptionPlan.START, "tg", 240)
    env.raw_competitors(ws, 3)
    message, state = _message("https://another.example.com"), MagicMock(clear=AsyncMock())
    context = SimpleNamespace(workspace_id=ws)

    # plan_quota_service=None: the atomic guard alone must refuse
    run(receive_competitor_url(message, state, env.competitors, context, None))

    assert env.count("competitors", ws) == 3
    assert "3 конкурентов" in message.answer.await_args.args[0]
    state.clear.assert_awaited()


def test_telegram_manual_add_works_under_the_limit(env: Env) -> None:
    from app.handlers.competitors import receive_competitor_url

    ws = env.workspace(SubscriptionPlan.START, "tg-ok", 241)
    message, state = _message("https://fine.example.com"), MagicMock(clear=AsyncMock())

    run(receive_competitor_url(message, state, env.competitors, SimpleNamespace(workspace_id=ws), None))

    assert env.count("competitors", ws) == 1


def test_telegram_candidate_add_at_the_limit_is_refused_and_candidate_not_marked_added(
    env: Env,
) -> None:
    from app.handlers.competitors import add_competitor_candidate

    ws = env.workspace(SubscriptionPlan.START, "tg-cand", 242)
    env.raw_competitors(ws, 3)
    candidate = run(env.competitors.upsert_candidate(
        ws, name="Cand", discovered_url="https://cand.example.com", source_title="t",
        source_url="https://s.example.com", description="d", evidence=("e",),
        confidence=CandidateConfidence.HIGH,
        classification=CandidateClassification.DIRECT_COMPETITOR, why_it_matters="w",
    ))
    callback = SimpleNamespace(
        data=f"{COMPETITOR_DISCOVERY_ADD_PREFIX}{candidate.candidate_id}",
        answer=AsyncMock(), message=SimpleNamespace(answer=AsyncMock()),
    )

    run(add_competitor_candidate(callback, env.competitors, SimpleNamespace(workspace_id=ws), None))

    assert env.count("competitors", ws) == 3
    assert "3 конкурентов" in callback.message.answer.await_args.args[0]
    stored = run(env.competitors.get_candidate_for_workspace(ws, candidate.candidate_id))
    assert stored.status != CandidateStatus.ADDED


# ── 6. unknown plan, no subscription, explicit beta ─────────────────────────


def test_no_subscription_blocks_new_competitors_and_sources(env: Env) -> None:
    ws = env.workspace(None, "nosub2", 250)

    with pytest.raises(SlotLimitReached) as err:
        run(env.competitors.add_competitor(ws, "https://x.example.com"))
    assert "подписка не найдена" in err.value.message
    with pytest.raises(SlotLimitReached):
        run(env.sources.add_source(ws, "https://x.example.net/"))
    assert env.count("competitors", ws) == 0
    assert env.count("workspace_source_subscriptions", ws) == 0


def test_unknown_plan_blocks_with_a_clear_reason(env: Env, monkeypatch) -> None:
    ws = env.workspace(SubscriptionPlan.STANDARD, "unk", 251)

    async def _unknown(db, workspace_id):
        return "enterprise-2099"

    monkeypatch.setattr(slot_guard, "_plan_of", _unknown)

    with pytest.raises(SlotLimitReached) as err:
        run(env.competitors.add_competitor(ws, "https://x.example.com"))
    assert "не распознан" in err.value.message and "enterprise" not in err.value.message
    with pytest.raises(SlotLimitReached):
        run(env.sources.add_source(ws, "https://x.example.net/"))


def test_explicit_beta_stays_unlimited(env: Env) -> None:
    ws = env.workspace(SubscriptionPlan.BETA, "beta2", 252)
    env.raw_competitors(ws, 40)

    run(env.competitors.add_competitor(ws, "https://still-ok.example.com"))
    env.fill_sources(ws, 50)

    assert env.count("competitors", ws) == 41
    assert env.count("workspace_source_subscriptions", ws) == 50


# ── 7. workspace isolation ──────────────────────────────────────────────────


def test_a_full_workspace_does_not_use_up_another_workspaces_slots(env: Env) -> None:
    full = env.workspace(SubscriptionPlan.START, "iso-a", 260)
    other = env.workspace(SubscriptionPlan.START, "iso-b", 261)
    env.raw_competitors(full, 3)
    env.fill_sources(full, 5)

    with pytest.raises(SlotLimitReached):
        run(env.competitors.add_competitor(full, "https://no.example.com"))
    with pytest.raises(SlotLimitReached):
        run(env.sources.add_source(full, "https://no.example.net/"))
    run(env.competitors.add_competitor(other, "https://yes.example.com"))
    run(env.sources.add_source(other, "https://yes.example.net/"))

    assert env.count("competitors", other) == 1
    assert env.count("workspace_source_subscriptions", other) == 1


def test_the_same_public_source_costs_each_workspace_its_own_slot(env: Env) -> None:
    a = env.workspace(SubscriptionPlan.START, "same-a", 262)
    b = env.workspace(SubscriptionPlan.START, "same-b", 263)

    run(env.sources.add_source(a, "https://shared-site.example.net/"))
    run(env.sources.add_source(b, "https://shared-site.example.net/"))

    assert env.count("workspace_source_subscriptions", a) == 1
    assert env.count("workspace_source_subscriptions", b) == 1


# ── 8. existing over-limit data is preserved ────────────────────────────────


def test_existing_overage_is_kept_visible_and_workable_but_new_slots_stay_blocked(
    env: Env,
) -> None:
    ws = env.workspace(SubscriptionPlan.STANDARD, "over", 270)  # 10 / 15
    env.raw_competitors(ws, 11)
    env.raw_sources(ws, 33)

    with pytest.raises(SlotLimitReached):
        run(env.competitors.add_competitor(ws, "https://more.example.com"))
    with pytest.raises(SlotLimitReached):
        run(env.sources.add_source(ws, "https://more.example.net/"))

    # nothing deleted or disabled, everything still readable and workable
    assert env.count("competitors", ws) == 11
    assert env.count("workspace_source_subscriptions", ws) == 33
    assert run(env.competitors.count_for_workspace(ws)) == 11
    assert run(env.sources.count_for_workspace(ws)) == 33
    assert len(run(env.sources.list_for_workspace(ws))) == 33
    assert all(s.enabled for s in run(env.sources.list_for_workspace(ws)))
    first = run(env.competitors.list_for_workspace(ws, limit=50))[0]
    assert run(env.competitors.update_label(ws, first.id, "Renamed")).label == "Renamed"
    run(env.sources.set_enabled(ws, "raw_%d_0" % ws, False))
    run(env.sources.set_enabled(ws, "raw_%d_0" % ws, True))
    assert env.count("workspace_source_subscriptions", ws) == 33


def test_new_slots_open_only_once_enough_room_is_freed(env: Env) -> None:
    ws = env.workspace(SubscriptionPlan.STANDARD, "free2", 271)
    env.raw_competitors(ws, 11)
    with sqlite3.connect(env.db) as db:
        ids = [r[0] for r in db.execute("SELECT id FROM competitors WHERE workspace_id=? ORDER BY id", (ws,))]
        db.execute("DELETE FROM competitors WHERE id = ?", (ids[0],))  # 10 / 10
    with pytest.raises(SlotLimitReached):
        run(env.competitors.add_competitor(ws, "https://x1.example.com"))
    with sqlite3.connect(env.db) as db:
        db.execute("DELETE FROM competitors WHERE id = ?", (ids[1],))  # 9 / 10

    run(env.competitors.add_competitor(ws, "https://x2.example.com"))

    assert env.count("competitors", ws) == 10


def test_upgrading_the_tariff_opens_new_slots_for_an_over_limit_workspace(env: Env) -> None:
    ws = env.workspace(SubscriptionPlan.STANDARD, "up", 272)
    env.raw_competitors(ws, 11)
    with pytest.raises(SlotLimitReached):
        run(env.competitors.add_competitor(ws, "https://no.example.com"))
    paid = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    run(env.subs.mark_paid(ws, external_payment_id="up", payment_provider="robokassa",
                           paid_until=paid, plan=SubscriptionPlan.FULL))

    run(env.competitors.add_competitor(ws, "https://yes.example.com"))

    assert env.count("competitors", ws) == 12  # FULL allows 25


# ── legacy import ───────────────────────────────────────────────────────────


def _legacy_import(tmp: Path, plan: SubscriptionPlan | None, *, with_sub_table: bool = True):
    db = tmp / "imp.sqlite3"
    partners = PartnerRepository(db)
    run(partners.init())
    owner = run(partners.bootstrap_owner_membership(100)).workspace_id
    if with_sub_table:
        subs = SubscriptionRepository(db)
        run(subs.init())
        if plan is not None:
            paid = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
            run(subs.mark_paid(owner, external_payment_id="x", payment_provider="robokassa",
                               paid_until=paid, plan=plan))
    legacy = _legacy_file(
        tmp / "l.json", [_web_source(f"l{i}", priority=10 + i) for i in range(8)],
    )
    repo = SourceCatalogRepository(db, tmp / "p.json", enforce_plan_slots=True)
    run(repo.init(owner, legacy_path=legacy))
    return repo, owner


def test_legacy_import_for_an_owner_on_start_connects_only_five_by_priority(tmp_path: Path) -> None:
    repo, owner = _legacy_import(tmp_path, SubscriptionPlan.START)

    assert {s.id for s in run(repo.list_for_workspace(owner))} == {f"l{i}" for i in range(5)}
    with sqlite3.connect(tmp_path / "imp.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM source_catalog").fetchone()[0] == 8


def test_legacy_import_on_a_fresh_database_is_not_capped(tmp_path: Path) -> None:
    repo, owner = _legacy_import(tmp_path, None, with_sub_table=False)

    assert len(run(repo.list_for_workspace(owner))) == 8


def test_legacy_import_for_a_beta_owner_is_not_capped(tmp_path: Path) -> None:
    repo, owner = _legacy_import(tmp_path, None)  # backfilled as beta

    assert len(run(repo.list_for_workspace(owner))) == 8


# ── production wiring ───────────────────────────────────────────────────────


def test_every_production_construction_site_enables_slot_enforcement() -> None:
    root = Path(__file__).resolve().parents[1] / "app"
    main_src = (root / "main.py").read_text(encoding="utf-8")
    web_src = (root / "web_api.py").read_text(encoding="utf-8")
    admin_src = (root / "admin.py").read_text(encoding="utf-8")

    assert main_src.count("enforce_plan_slots=True") == 2
    assert web_src.count("enforce_plan_slots=True") == 2
    assert admin_src.count("enforce_plan_slots=True") == 1
