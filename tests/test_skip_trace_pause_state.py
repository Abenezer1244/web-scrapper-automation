"""The spend cap's PAUSE STATE (Phase 1b-1b-iii-b): when a binding cap lets lookups
run again, published to Redis on every dispatcher tick for the API to read.

Contract: tasks/todo-lookup-contacts.md, "FINAL contract" (+ r4/r5 amendments).
Real Postgres and the local test Redis; the only seam at Tracerfy is the
non-HTTPS base URL (see tests/test_skip_trace_credit_cap.py, whose seeding helpers
are reused here).
"""
# The fixtures imported from test_skip_trace_credit_cap are requested by parameter
# name, which ruff reads as redefining the import. Only F811, only this file.
# ruff: noqa: F811
import inspect
import json
import logging
import time
from datetime import UTC, datetime, timedelta

import pytest
import redis as sync_redis
from sqlalchemy import event, text

from src.config import settings
from src.db.session import sync_engine, system_sync_session
from src.utils import skip_trace_pause_state as pause
from src.utils.skip_trace_pause_state import NEVER, NOT_PAUSED, PAUSED, UNKNOWN, ScopeResume
from src.workers import skip_trace_capacity as cap
from src.workers import skip_trace_dispatcher
from src.workers.skip_trace_dispatcher import dispatch_pending_skip_trace
from tests.test_skip_trace_credit_cap import (  # noqa: F401 - fixtures used by name
    _seed,
    dispatcher,
    make_account,
)

H = timedelta(hours=1)
DAY = cap.SPEND_WINDOW
MARGIN = cap.RESUME_MARGIN


@pytest.fixture
def r():
    """A real client built the way the dispatcher builds it; the key is removed
    and the client closed whatever the test did."""
    client = sync_redis.from_url(settings.REDIS_URL, **settings.redis_kwargs())
    client.delete(pause.KEY)
    try:
        yield client
    finally:
        client.delete(pause.KEY)
        client.close()


def _spend(user_id: str, *at_types, queue_id=900000):
    """Accepted rows at the given (submitted_at, trace_type) pairs."""
    for at, tt in at_types:
        _seed(user_id, trace_type=tt, status="submitted", submitted_at=at,
              tracerfy_queue_id=queue_id)


def _resume(caps: cap.Caps, now: datetime) -> cap.PauseSnapshot:
    with system_sync_session() as db:
        out = cap.resume_times(db, now, caps)
        db.rollback()
    return out


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


# ── The resume rule, per scope ────────────────────────────────────────────────


def test_scope_resume_rules():
    t = datetime(2026, 9, 27, 12, tzinfo=UTC)
    assert cap.scope_resume(0, 99, {}) == (None, None)            # cap off
    assert cap.scope_resume(1, 0, {}) == (None, NEVER)            # 1 never admits advanced
    assert cap.scope_resume(5, 3, {}) == (None, None)             # both fit
    assert cap.scope_resume(5, 4, {2: t}) == (None, t + DAY + MARGIN)
    assert cap.scope_resume(5, 5, {1: t, 2: t + H}) == (t + DAY + MARGIN, t + H + DAY + MARGIN)
    with pytest.raises(ValueError):
        cap.scope_resume(5, 5, {1: None, 2: t})


# ── resume_times against real rows ────────────────────────────────────────────


async def test_normal_and_advanced_resume_at_the_row_that_frees_enough_credit(make_account):
    u = await make_account()
    now = _now()
    _spend(u, (now - 5 * H, "normal"), (now - 4 * H, "normal"), (now - 3 * H, "normal"))
    snap = _resume(cap.Caps(0, 3, "t"), now)
    # S=3, C=3: a normal lookup needs 1 credit out (the 5h row), an advanced one 2.
    assert snap.accounts[u] == (now - 5 * H + DAY + MARGIN, now - 4 * H + DAY + MARGIN)


async def test_an_advanced_row_straddling_the_threshold_counts_both_its_credits(make_account):
    u = await make_account()
    now = _now()
    _spend(u, (now - 5 * H, "advanced"), (now - 4 * H, "normal"))
    snap = _resume(cap.Caps(0, 2, "t"), now)
    # S=3, C=2: normal needs 2 out -> the advanced row alone; advanced needs 3.
    assert snap.accounts[u] == (now - 5 * H + DAY + MARGIN, now - 4 * H + DAY + MARGIN)


async def test_rows_tied_on_submitted_at_give_one_answer(make_account):
    u = await make_account()
    now = _now()
    _spend(u, (now - 2 * H, "normal"), queue_id=7)
    _spend(u, (now - 2 * H, "normal"), queue_id=3)
    _spend(u, (now - 1 * H, "normal"), queue_id=5)
    snap = _resume(cap.Caps(0, 2, "t"), now)
    # S=3, C=2: normal needs 2 out (both tied rows), advanced 3 (the 1h row too).
    assert snap.accounts[u] == (now - 2 * H + DAY + MARGIN, now - 1 * H + DAY + MARGIN)


async def test_released_and_expired_rows_are_not_spend(make_account):
    u = await make_account()
    now = _now()
    _seed(u, status="errored", submitted_at=None)                          # released
    _spend(u, (now - DAY - timedelta(seconds=1), "advanced"))              # left the window
    _spend(u, (now - H, "normal"), (now - H, "normal"))
    snap = _resume(cap.Caps(0, 2, "t"), now)
    assert snap.accounts[u] == (now - H + DAY + MARGIN, now - H + DAY + MARGIN)


async def test_resume_time_is_exactly_when_spent_credits_first_has_room(make_account):
    """The INVARIANT, against the cap's own spend read: at resume_at(c) a lookup of
    cost c fits; one second earlier it does not."""
    u = await make_account()
    now = _now()
    _spend(u, (now - 7 * H, "advanced"), (now - 6 * H, "normal"), (now - 5 * H, "advanced"),
           (now - 5 * H, "normal"), (now - 2 * H, "advanced"))
    account_cap = 5  # S = 8
    got = _resume(cap.Caps(0, account_cap, "t"), now).accounts[u]
    for cost, at in ((1, got.normal), (2, got.advanced)):
        with system_sync_session() as db:
            _, room = cap.spent_credits(db, at - DAY)
            _, before = cap.spent_credits(db, at - timedelta(seconds=1) - DAY)
        assert room.get(u, 0) + cost <= account_cap, (cost, at)
        assert before.get(u, 0) + cost > account_cap, (cost, at)


async def test_global_and_account_scopes_are_separate(make_account):
    a, b = await make_account(), await make_account()
    now = _now()
    _spend(a, (now - 3 * H, "normal"), (now - 2 * H, "normal"))
    _spend(b, (now - H, "normal"))
    # Account cap 3: a (2 spent) has no room for an advanced lookup, b (1 spent)
    # does. The global cap of 3 (3 spent) binds everyone.
    snap = _resume(cap.Caps(3, 3, "t"), now)
    assert set(snap.accounts) == {a}
    assert snap.accounts[a] == (None, now - 3 * H + DAY + MARGIN)
    assert snap.global_scope == (now - 3 * H + DAY + MARGIN, now - 2 * H + DAY + MARGIN)
    assert snap.account_default == (None, None)


async def test_a_cap_of_one_never_admits_an_advanced_lookup(make_account):
    u = await make_account()
    now = _now()
    _spend(u, (now - H, "normal"))
    snap = _resume(cap.Caps(1, 1, "t"), now)
    assert snap.global_scope == (now - H + DAY + MARGIN, NEVER)
    assert snap.accounts[u] == (now - H + DAY + MARGIN, NEVER)
    assert snap.account_default == (None, NEVER)  # an account that spent nothing


async def test_a_lowered_cap_walks_past_the_first_two_credits(make_account):
    """The caps are hard, so normally S <= C and the threshold is within a scope's
    first two credits; a cap LOWERED over existing spend puts it further in, and
    the walk must go there (Codex K3)."""
    u = await make_account()
    now = _now()
    _spend(u, *[(now - (9 - i) * H, "normal") for i in range(6)])  # 9h..4h ago, 6 credits
    snap = _resume(cap.Caps(2, 2, "t"), now)
    # S=6, C=2: normal needs 5 credits out (the 5h row), advanced 6 (the 4h row).
    expected = (now - 5 * H + DAY + MARGIN, now - 4 * H + DAY + MARGIN)
    assert snap.accounts[u] == expected
    assert snap.global_scope == expected


def test_a_global_scope_with_nothing_spent_still_reports(monkeypatch):
    """The global row is unconditional (LEFT JOIN), so an empty window still yields
    the scope: fits under a cap of 5, and NEVER for advanced under a cap of 1 (K2)."""
    far = datetime(2001, 1, 1, tzinfo=UTC)  # a window with no rows in it
    assert _resume(cap.Caps(5, 0, "t"), far).global_scope == (None, None)
    assert _resume(cap.Caps(1, 0, "t"), far).global_scope == (None, NEVER)


async def test_resume_times_is_one_statement_so_one_snapshot(make_account):
    """Totals and both scopes' walks come from ONE statement (Codex K4): a claim or
    release between two statements could otherwise mix two views."""
    u = await make_account()
    now = _now()
    _spend(u, (now - H, "normal"), (now - H, "normal"))
    statements = []

    def seen(conn, cursor, statement, *a):
        statements.append(statement)
    event.listen(sync_engine, "before_cursor_execute", seen)
    try:
        snap = _resume(cap.Caps(2, 2, "t"), now)
    finally:
        event.remove(sync_engine, "before_cursor_execute", seen)
    assert u in snap.accounts and snap.global_scope != (None, None)
    assert len([s for s in statements if "pending_skip_trace_rows" in s]) == 1


def test_the_resume_statement_runs_under_a_5s_ceiling_that_ends_with_its_transaction(
        monkeypatch, dispatcher):
    """L1: statement_timeout is set after a successful fence, in the same
    transaction, before the one resume statement, and is gone afterwards."""
    dispatcher(account_cap=5)
    seen = {}
    real = skip_trace_dispatcher.resume_times

    def recording(db, now, caps):
        seen["inside"] = db.execute(text("SHOW statement_timeout")).scalar()
        return real(db, now, caps)
    monkeypatch.setattr(skip_trace_dispatcher, "resume_times", recording)
    skip_trace_dispatcher._publish_pause_state()
    assert seen["inside"] == "5s"
    with system_sync_session() as db:
        assert db.execute(text("SHOW statement_timeout")).scalar() != "5s"


def test_both_caps_off_reads_nothing():
    statements = []

    def seen(conn, cursor, statement, *a):
        statements.append(statement)
    event.listen(sync_engine, "before_cursor_execute", seen)
    try:
        snap = _resume(cap.Caps(0, 0, "t"), _now())
    finally:
        event.remove(sync_engine, "before_cursor_execute", seen)
    assert snap == cap.PauseSnapshot((None, None), (None, None), {})
    assert not [s for s in statements if "pending_skip_trace_rows" in s]


# ── TTL ───────────────────────────────────────────────────────────────────────


def test_ttl_follows_the_interval_rounds_up_covers_resumes_and_ignores_never():
    now = datetime(2026, 9, 27, tzinfo=UTC)
    assert pause.ttl_seconds(now, 120, []) == 2 * 120 + 120
    assert pause.ttl_seconds(now, 300, [ScopeResume(None, NEVER)]) == 720
    far = now + timedelta(hours=20)
    assert pause.ttl_seconds(now, 300, [ScopeResume(far, None)]) == 20 * 3600 + 120
    frac = now + timedelta(seconds=1000, milliseconds=300)
    assert pause.ttl_seconds(now, 60, [ScopeResume(None, frac)]) == 1121


def test_the_published_key_carries_that_ttl(r):
    now = datetime.now(UTC)
    far = now + timedelta(hours=20)
    assert pause.publish(r, fence=pause.fence_str(1), now=now, interval_s=300,
                         global_scope=ScopeResume(far, far), account_default=ScopeResume(None, None),
                         accounts={})
    assert 20 * 3600 < r.ttl(pause.KEY) <= 20 * 3600 + 121


# ── The fence ─────────────────────────────────────────────────────────────────


def _pub(r, fence: int, scope: ScopeResume, *, user="u1", now=None) -> bool:
    now = now or datetime.now(UTC)
    return pause.publish(r, fence=pause.fence_str(fence), now=now, interval_s=300,
                         global_scope=ScopeResume(None, None),
                         account_default=ScopeResume(None, None), accounts={user: scope})


def test_an_older_or_equal_fence_never_overwrites_a_newer_one(r):
    later = datetime.now(UTC) + H
    assert _pub(r, 20, ScopeResume(later, later))
    assert not _pub(r, 19, ScopeResume(None, None))
    assert not _pub(r, 20, ScopeResume(None, None))
    assert pause.read_pause_state(r, "u1", datetime.now(UTC)).status == PAUSED
    assert _pub(r, 21, ScopeResume(None, None)) is True
    assert pause.read_pause_state(r, "u1", datetime.now(UTC)).status == NOT_PAUSED


def test_a_malformed_stored_fence_is_replaced(r):
    r.hset(pause.KEY, mapping={"fence": "99999999999999999999x"})
    assert _pub(r, 1, ScopeResume(None, None))
    assert r.hget(pause.KEY, "fence") == pause.fence_str(1)


def test_a_fence_compares_as_a_number_not_as_text(r):
    assert _pub(r, 9, ScopeResume(None, None))
    assert _pub(r, 10, ScopeResume(None, None))  # "10" < "9" as text, not zero-padded


def test_a_tombstone_reads_unknown_and_an_older_publish_cannot_undo_it(r):
    later = datetime.now(UTC) + H
    assert _pub(r, 5, ScopeResume(later, later))
    assert pause.publish_tombstone(r, fence=pause.fence_str(6), interval_s=300)
    assert pause.read_pause_state(r, "u1", datetime.now(UTC)).status == UNKNOWN
    assert not _pub(r, 5, ScopeResume(later, later))
    assert pause.read_pause_state(r, "u1", datetime.now(UTC)).status == UNKNOWN


def test_after_a_tombstone_expires_a_stale_publish_lands_and_the_next_tick_replaces_it(r):
    """The accepted residual (plan G2): only a publisher stalled past the TTL."""
    later = datetime.now(UTC) + H
    assert pause.publish_tombstone(r, fence=pause.fence_str(6), interval_s=300)
    r.pexpire(pause.KEY, 50)
    time.sleep(0.2)
    assert _pub(r, 5, ScopeResume(later, later))
    assert pause.read_pause_state(r, "u1", datetime.now(UTC)).status == PAUSED
    assert _pub(r, 7, ScopeResume(None, None))
    assert pause.read_pause_state(r, "u1", datetime.now(UTC)).status == NOT_PAUSED


async def test_a_publisher_that_read_before_a_claim_cannot_undo_the_claims_publish(
        make_account, dispatcher, r):
    """The race behind the fence (plan E2/G1), on real sessions: L takes its fence
    and reads (nothing paused), a claim commits, the claiming tick publishes
    'paused', and only then does L write. L must lose."""
    u = await make_account()
    dispatcher(account_cap=2)
    now = _now()
    with system_sync_session() as db_l:
        fence_l = skip_trace_dispatcher._pause_fence(db_l)
        stale = cap.resume_times(db_l, now, cap.resolve_caps())
        db_l.rollback()
    assert u not in stale.accounts

    _spend(u, (now - H, "normal"), (now - H, "normal"))  # the claim commits
    skip_trace_dispatcher._publish_pause_state()           # its tick publishes

    assert not pause.publish(r, fence=fence_l, now=now, interval_s=300,
                             global_scope=stale.global_scope,
                             account_default=stale.account_default, accounts=stale.accounts)
    assert pause.read_pause_state(r, u, datetime.now(UTC)).status == PAUSED


def test_a_read_only_transaction_publishes_nothing(caplog):
    with system_sync_session() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        with caplog.at_level(logging.WARNING):
            assert skip_trace_dispatcher._pause_fence(db) is None
        db.rollback()
    assert any("read-only" in rec.getMessage() for rec in caplog.records)


# ── The reader ────────────────────────────────────────────────────────────────


def _valid(now: datetime, **over) -> dict:
    fields = {
        "fence": pause.fence_str(3),
        "published_at": now.isoformat(),
        "fresh_until": (now + timedelta(minutes=10)).isoformat(),
        "global": json.dumps({"normal_resume_at": None, "advanced_resume_at": None}),
        "account_default": json.dumps({"normal_resume_at": None, "advanced_resume_at": None}),
    }
    fields.update(over)
    return {k: v for k, v in fields.items() if v is not None}


def test_a_fresh_valid_state_with_no_own_field_is_not_paused(r):
    now = datetime.now(UTC)
    r.hset(pause.KEY, mapping=_valid(now))
    assert pause.read_pause_state(r, "someone", now) == (NOT_PAUSED, None, None)


_LATER = "2999-01-01T00:00:00+00:00"


@pytest.mark.parametrize("case,over", [
    ("fence missing", {"fence": None}),
    ("fence malformed", {"fence": "12"}),
    ("published_at missing", {"published_at": None}),
    ("published_at malformed", {"published_at": "yesterday"}),
    ("fresh_until missing", {"fresh_until": None}),
    ("fresh_until naive", {"fresh_until": "2999-01-01T00:00:00"}),
    ("fresh_until not UTC", {"fresh_until": "2999-01-01T00:00:00+02:00"}),
    ("fresh_until past", {"fresh_until": "2000-01-01T00:00:00+00:00"}),
    ("global missing", {"global": None}),
    ("account_default missing", {"account_default": None}),
    ("global not JSON", {"global": "{"}),
    ("global extra key", {"global": json.dumps(
        {"normal_resume_at": None, "advanced_resume_at": None, "x": 1})}),
    ("global wrong type", {"global": json.dumps({"normal_resume_at": 5, "advanced_resume_at": None})}),
    ("never as a normal time", {"global": json.dumps(
        {"normal_resume_at": NEVER, "advanced_resume_at": None})}),
    ("own field not UTC", {"me": json.dumps(
        {"normal_resume_at": "2999-01-01T00:00:00+05:00", "advanced_resume_at": None})}),
    ("own field a list", {"me": "[]"}),
])
def test_anything_missing_stale_or_malformed_reads_unknown(r, case, over):
    now = datetime.now(UTC)
    r.hset(pause.KEY, mapping=_valid(now, **over))
    assert pause.read_pause_state(r, "me", now).status == UNKNOWN, case


def test_no_key_at_all_reads_unknown(r):
    assert pause.read_pause_state(r, "me", datetime.now(UTC)).status == UNKNOWN


def test_redis_down_reads_unknown():
    down = sync_redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.5)
    try:
        assert pause.read_pause_state(down, "me", datetime.now(UTC)).status == UNKNOWN
    finally:
        down.close()


def test_combining_scopes_never_dominates_and_the_later_time_wins(r):
    now = datetime.now(UTC)
    soon, later = now + H, now + 5 * H
    r.hset(pause.KEY, mapping=_valid(
        now,
        **{"global": json.dumps({"normal_resume_at": later.isoformat(),
                                 "advanced_resume_at": soon.isoformat()}),
           "me": json.dumps({"normal_resume_at": soon.isoformat(),
                             "advanced_resume_at": NEVER})}))
    assert pause.read_pause_state(r, "me", now) == (PAUSED, later, NEVER)


def test_a_resume_time_already_past_is_not_paused(r):
    now = datetime.now(UTC)
    r.hset(pause.KEY, mapping=_valid(now, me=json.dumps(
        {"normal_resume_at": (now - timedelta(seconds=1)).isoformat(),
         "advanced_resume_at": (now - timedelta(seconds=1)).isoformat()})))
    assert pause.read_pause_state(r, "me", now).status == NOT_PAUSED


def test_account_default_applies_to_an_account_without_a_field(r):
    now = datetime.now(UTC)
    r.hset(pause.KEY, mapping=_valid(now, account_default=json.dumps(
        {"normal_resume_at": None, "advanced_resume_at": NEVER})))
    assert pause.read_pause_state(r, "anyone", now) == (PAUSED, None, NEVER)


class _Recording(sync_redis.Redis):
    """The real client, recording which commands it sends."""
    def execute_command(self, *args, **kwargs):
        self.sent.append(args)
        return super().execute_command(*args, **kwargs)


def test_the_reader_asks_for_a_fixed_field_list_never_the_whole_hash(r):
    now = datetime.now(UTC)
    r.hset(pause.KEY, mapping=_valid(now, other=json.dumps(
        {"normal_resume_at": _LATER, "advanced_resume_at": _LATER})))
    rec = _Recording.from_url(settings.REDIS_URL, **settings.redis_kwargs())
    rec.sent = []
    try:
        pause.read_pause_state(rec, "me", now)
    finally:
        rec.close()
    assert [a[0] for a in rec.sent] == ["HMGET"]
    assert list(rec.sent[0][1:]) == [pause.KEY, "published_at", "fresh_until", "fence",
                                     "global", "account_default", "me"]


# ── The dispatcher publishes ──────────────────────────────────────────────────


async def test_an_account_at_its_cap_with_no_queued_rows_is_published(make_account, dispatcher, r):
    u = await make_account()
    dispatcher(account_cap=2)
    now = _now()
    _spend(u, (now - H, "normal"), (now - H, "normal"))

    dispatch_pending_skip_trace()

    got = pause.read_pause_state(r, u, datetime.now(UTC))
    assert got == (PAUSED, now - H + DAY + MARGIN, now - H + DAY + MARGIN)


async def test_paused_then_resumed_leaves_no_stale_paused(make_account, dispatcher, r):
    u = await make_account()
    dispatcher(account_cap=2)
    now = _now()
    _spend(u, (now - H, "normal"), (now - H, "normal"))
    dispatch_pending_skip_trace()
    first = r.hget(pause.KEY, "published_at")
    assert r.hexists(pause.KEY, u)

    with system_sync_session() as db:  # the window moves past both rows
        db.execute(text("UPDATE pending_skip_trace_rows SET submitted_at = :t "
                        "WHERE user_id = :u"), {"t": now - DAY - H, "u": u})
        db.commit()
    dispatch_pending_skip_trace()

    assert not r.hexists(pause.KEY, u)
    assert (datetime.fromisoformat(r.hget(pause.KEY, "published_at"))
            > datetime.fromisoformat(first))
    assert pause.read_pause_state(r, u, datetime.now(UTC)).status == NOT_PAUSED


async def test_the_early_global_cap_exit_publishes(make_account, dispatcher, r):
    u = await make_account()
    dispatcher(global_cap=2)
    now = _now()
    _spend(u, (now - H, "normal"), (now - H, "normal"))

    out = dispatch_pending_skip_trace()

    assert out["skipped"] == "daily_cap"
    assert pause.read_pause_state(r, u, datetime.now(UTC)).status == PAUSED


async def test_a_tick_that_finds_the_claim_lock_held_still_publishes(dispatcher, r):
    dispatcher()
    with system_sync_session() as holder:
        holder.execute(text("SELECT pg_advisory_xact_lock(:k)"),
                       {"k": skip_trace_dispatcher._CLAIM_LOCK_KEY})
        out = dispatch_pending_skip_trace()
        holder.rollback()
    assert out.get("deferred") == "claim_locked"
    assert pause.read_pause_state(r, "anyone", datetime.now(UTC)).status == NOT_PAUSED


@pytest.mark.parametrize("switch", ["disabled", "no_token"])
async def test_a_switched_off_dispatcher_leaves_unknown_not_a_stale_paused(
        make_account, dispatcher, r, monkeypatch, switch):
    u = await make_account()
    dispatcher(account_cap=2)
    now = _now()
    _spend(u, (now - H, "normal"), (now - H, "normal"))
    dispatch_pending_skip_trace()
    assert pause.read_pause_state(r, u, datetime.now(UTC)).status == PAUSED

    if switch == "disabled":
        monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", False)
    else:
        monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "")
    assert dispatch_pending_skip_trace() == {"skipped": switch}

    assert r.hget(pause.KEY, "state") == "disabled"
    assert pause.read_pause_state(r, u, datetime.now(UTC)).status == UNKNOWN


async def test_a_tick_that_raises_still_raises_and_still_publishes(dispatcher, r, monkeypatch):
    dispatcher()

    def boom():
        raise RuntimeError("tick failed")
    monkeypatch.setattr(skip_trace_dispatcher, "_dispatch_tick", boom)

    with pytest.raises(RuntimeError, match="tick failed"):
        dispatch_pending_skip_trace()
    assert r.exists(pause.KEY)


async def test_a_failing_publish_never_changes_the_tick(dispatcher, monkeypatch, caplog):
    dispatcher()
    expected = dispatch_pending_skip_trace()
    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:1/0")
    with caplog.at_level(logging.WARNING):
        assert dispatch_pending_skip_trace() == expected
    assert any("pause state not published" in rec.getMessage() for rec in caplog.records)


async def test_a_failing_resume_read_never_changes_the_tick(dispatcher, monkeypatch, caplog):
    dispatcher()
    expected = dispatch_pending_skip_trace()

    def broken(db, now, caps):
        db.execute(text("SELECT 1/0"))
    monkeypatch.setattr(skip_trace_dispatcher, "resume_times", broken)
    with caplog.at_level(logging.WARNING):
        assert dispatch_pending_skip_trace() == expected
    assert any("DivisionByZero" in rec.getMessage() for rec in caplog.records)


async def test_the_database_session_is_closed_before_redis_is_touched(dispatcher, monkeypatch, r):
    """No pooled connection is held across Redis I/O, and the client is built with
    settings.redis_kwargs() (TLS verification in production)."""
    dispatcher()
    from src.db import session as session_mod

    real_session, real_from_url, real_kwargs = (
        session_mod.system_sync_session, sync_redis.from_url, settings.redis_kwargs)
    state = {"open": 0, "open_at_redis": None, "kwargs_used": False}

    from contextlib import contextmanager

    @contextmanager
    def counting():
        state["open"] += 1
        try:
            with real_session() as s:
                yield s
        finally:
            state["open"] -= 1

    def from_url(*a, **k):
        state["open_at_redis"] = state["open"]
        return real_from_url(*a, **k)

    def kwargs(*a, **k):
        state["kwargs_used"] = True
        return real_kwargs(*a, **k)

    monkeypatch.setattr(session_mod, "system_sync_session", counting)
    monkeypatch.setattr(sync_redis, "from_url", from_url)
    monkeypatch.setattr(type(settings), "redis_kwargs", lambda self, *a, **k: kwargs(*a, **k))
    skip_trace_dispatcher._publish_pause_state()
    assert state["open_at_redis"] == 0
    assert state["kwargs_used"]
    assert r.exists(pause.KEY)


def test_the_moved_tick_body_and_the_publisher_do_not_shadow_datetime():
    """The guard in test_skip_trace_credit_cap inspects the task, whose body now
    lives in _dispatch_tick: re-point it (a local `from datetime import` once made
    every later datetime.now raise UnboundLocalError)."""
    for fn in (skip_trace_dispatcher._dispatch_tick, skip_trace_dispatcher._publish_pause_state,
               skip_trace_dispatcher._pause_fence):
        code = "\n".join(line for line in inspect.getsource(fn).splitlines()
                         if not line.lstrip().startswith("#"))
        assert "from datetime import" not in code, fn.__name__
