"""The contact-lookup action reconciler (Phase 1b-2, 2c-ii).

Contract: tasks/todo-lookup-contacts.md, "## Phase 1b-2c — the reconciler" as amended by
AA1-AA9 and AB1-AB4. Actions are claimed by the REAL 2b worker; their pending rows are
then moved to the states ingest and the dispatcher leave them in. Real PG + Redis. The
only stand-ins are pass-through spies that count calls.
"""
from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import text

from src.db.session import system_sync_session
from src.workers import contact_lookup_action as cla
from src.workers.scheduler_helpers import contact_lookups as rec
from tests.test_contact_lookup_action import (
    _action,
    _age,
    _events,
    _hops,
    _job,
    _pending,
    _seed,
    _start,
    _state,
    _verdicts,
)


@pytest.fixture
def lookups_on(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "test-token-not-real")


def _claimed(user_id: str, n: int) -> tuple[str, str, list[str]]:
    """An action the real worker has claimed: n leads, all newly_queued."""
    job = _job(user_id)
    rids = sorted(_seed(user_id, job, [{} for _ in range(n)]))
    aid = _action(user_id, job, rids)
    assert cla.run_action(aid)["outcome"] == "claimed"
    return aid, job, rids


def _row(aid: str, rid: str, status: str, *, queue: int | None = None,
         submitted_ago: int | None = None) -> None:
    """Move the action's pending row for `rid` as ingest / the dispatcher would."""
    with system_sync_session() as s:
        s.execute(text(
            "UPDATE pending_skip_trace_rows SET status = :st, "
            "  tracerfy_queue_id = COALESCE(CAST(:q AS integer), tracerfy_queue_id), "
            "  submitted_at = CASE WHEN CAST(:ago AS integer) IS NULL THEN submitted_at "
            "                 ELSE now() - make_interval(secs => CAST(:ago AS integer)) END "
            "WHERE action_id = :a AND result_id = :r"),
            {"st": status, "q": queue, "ago": submitted_ago, "a": aid, "r": rid})
        s.commit()


def _result(rid: str, status: str | None) -> None:
    with system_sync_session() as s:
        s.execute(text("UPDATE results SET skip_trace_status = :st WHERE id = :r"),
                  {"st": status, "r": rid})
        s.commit()


def _queue(user_id: str, job: str, *, uploaded: int, status: str = "completed",
           sent: int | None = None, decision: bool | None = None) -> int:
    """A Tracerfy queue row. `sent` is what the dispatcher records (`rows_sent`);
    `decision` is what billing recorded for its unmatched rows (`unmatched_billed`),
    NULL until billing runs."""
    qid = 7_000_000 + uuid.uuid4().int % 1_000_000_000
    with system_sync_session() as s:
        s.execute(text(
            "INSERT INTO skip_trace_queues (id, tracerfy_queue_id, job_id, user_id, "
            "  trace_type, status, rows_uploaded, rows_sent, unmatched_billed, "
            "  credits_deducted, submitted_at) "
            "VALUES (:id, :q, :j, :u, 'normal', :st, :n, :sent, :d, :n, now())"),
            {"id": str(uuid.uuid4()), "q": qid, "j": job, "u": user_id, "st": status,
             "n": uploaded, "sent": sent, "d": decision})
        s.commit()
    return qid


def _decide(qid: int, decision: bool | None) -> None:
    with system_sync_session() as s:
        s.execute(text("UPDATE skip_trace_queues SET unmatched_billed = :d "
                       "WHERE tracerfy_queue_id = :q"), {"d": decision, "q": qid})
        s.commit()


def _tick() -> dict:
    return rec._reconcile_contact_lookups_impl()


@pytest.fixture
def alerts(monkeypatch):
    """Pass-through spy on the ops alert: records each call, then sends as usual."""
    from src.workers import ops_alerts

    calls: list = []
    real = ops_alerts.send_ops_alert

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(ops_alerts, "send_ops_alert", spy)
    return calls


# ── P4: settle claimed ───────────────────────────────────────────────────────


async def test_every_pending_outcome_maps_to_its_verdict_and_the_action_settles(
    db, business_user, lookups_on,
):
    uid = business_user.id
    aid, job, (hit, miss, um_ok, um_short, err, cxl) = _claimed(uid, 6)
    # Billing billed this queue's unmatched rows, and did not bill that one's.
    full = _queue(uid, job, uploaded=3, sent=3, decision=True)
    short = _queue(uid, job, uploaded=0, sent=3, decision=False)
    _row(aid, hit, "completed", queue=full)
    _result(hit, "hit")
    _row(aid, miss, "completed", queue=full)
    _result(miss, "miss")
    _row(aid, um_ok, "unmatched", queue=full)
    _row(aid, um_short, "unmatched", queue=short)
    _row(aid, err, "errored")
    _row(aid, cxl, "cancelled")

    out = _tick()

    assert _verdicts(aid) == {
        hit: "answered_hit", miss: "answered_miss", um_ok: "unmatched_billable",
        um_short: "unmatched_unbilled", err: "errored_unsubmitted", cxl: "released"}
    st = _state(aid)
    assert (st.status, st.status_reason, st.billable_rows) == ("settled", None, 3)
    assert _hops(aid)[-1] == ("claimed", "settled", None)
    assert out["settled"] == 1 and out["mapped"] == 6


async def test_the_billable_verdict_is_what_billing_charged_for_that_queue(
    db, business_user, lookups_on,
):
    """Parity (V2, W3, AA2): per queue, billing's billed row count for this user equals
    the action's billable verdicts in that queue. Billing runs for REAL and commits, as
    ingest does, so the reconciler reads the decision billing recorded (AD3)."""
    from src.api.billing.skip_trace_usage import report_usage_from_webhook

    uid = business_user.id
    aid, job, (a1, a2, b1, b2) = _claimed(uid, 4)
    # Both batches sent 2; Tracerfy uploaded 2 of one and 1 of the other.
    full = _queue(uid, job, uploaded=2, sent=2)
    short = _queue(uid, job, uploaded=1, sent=2)
    for rid, q in ((a1, full), (a2, full), (b1, short), (b2, short)):
        _row(aid, rid, "unmatched", queue=q)
    _row(aid, b2, "completed")
    _result(b2, "hit")

    billed = {}
    with system_sync_session() as s:
        for q in (full, short):
            users = report_usage_from_webhook(s, q)["users"]
            billed[q] = sum(u["n"] for u in users)
        s.commit()
    _tick()
    v = _verdicts(aid)
    billable = {"answered_hit", "answered_miss", "unmatched_billable"}
    assert billed[full] == sum(1 for r in (a1, a2) if v[r] in billable) == 2
    assert billed[short] == sum(1 for r in (b1, b2) if v[r] in billable) == 1
    assert v[b1] == "unmatched_unbilled"


async def test_nothing_settles_while_a_lookup_is_in_flight(db, business_user, lookups_on):
    uid = business_user.id
    aid, job, (done, waiting) = _claimed(uid, 2)
    _row(aid, done, "completed")
    _result(done, "hit")
    _tick()
    assert _verdicts(aid) == {done: "answered_hit", waiting: "newly_queued"}
    st = _state(aid)
    assert (st.status, st.status_reason, st.billable_rows) == ("claimed", None, 1)
    _row(aid, waiting, "completed")
    _result(waiting, "miss")
    _tick()
    assert _state(aid).status == "settled" and _state(aid).billable_rows == 2


# `results.skip_trace_status` is NOT NULL, so there is no NULL case to test.
@pytest.mark.parametrize("result_status", ["purged", "queued", "errored", "not_attempted"])
async def test_a_completed_lookup_whose_lead_is_not_hit_or_miss_is_flagged_never_guessed(
    db, business_user, lookups_on, alerts, result_status,
):
    uid = business_user.id
    aid, job, (rid,) = _claimed(uid, 1)
    _row(aid, rid, "completed")
    _result(rid, result_status)
    _tick()
    _tick()
    st = _state(aid)
    assert (st.status, st.status_reason) == ("claimed", rec.FLAG_RESULT_STATE_UNEXPECTED)
    assert _verdicts(aid) == {rid: "newly_queued"}
    assert sum(1 for f, t, why in _hops(aid) if why == rec.FLAG_RESULT_STATE_UNEXPECTED) == 1
    assert len(alerts) == 1  # once per change, not per tick


async def test_a_lead_with_no_pending_row_is_flagged_once_and_never_settled(
    db, business_user, lookups_on, alerts,
):
    uid = business_user.id
    aid, job, (rid, other) = _claimed(uid, 2)
    with system_sync_session() as s:  # an operator delete (the owner role may)
        s.execute(text("DELETE FROM pending_skip_trace_rows WHERE action_id = :a "
                       "AND result_id = :r"), {"a": aid, "r": rid})
        s.commit()
    _row(aid, other, "submitted")  # in flight: the visit below is for the missing row
    for _ in range(2):
        _tick()
    assert _state(aid).status_reason == rec.FLAG_PENDING_ROW_MISSING
    # The other lead settles later: the action is VISITED again, still blocked, and
    # must not alert a second time.
    _row(aid, other, "completed")
    _result(other, "hit")
    assert _tick()["mapped"] == 1
    st = _state(aid)
    assert (st.status, st.status_reason) == ("claimed", rec.FLAG_PENDING_ROW_MISSING)
    assert len(alerts) == 1
    assert _verdicts(aid) == {rid: "newly_queued", other: "answered_hit"}


@pytest.mark.parametrize("stuck", ["stale_submitting", "submitted_in_errored_queue"])
async def test_a_stuck_lookup_flags_then_clears_when_it_moves_again(
    db, business_user, lookups_on, alerts, stuck,
):
    uid = business_user.id
    aid, job, (rid,) = _claimed(uid, 1)
    if stuck == "stale_submitting":
        _row(aid, rid, "submitting", submitted_ago=31 * 60)
    else:
        _row(aid, rid, "submitted", queue=_queue(uid, job, uploaded=1, status="errored"))
    _tick()
    _tick()
    assert _state(aid).status_reason == rec.FLAG_PROVIDER_RECONCILIATION
    assert len(alerts) == 1
    # The dispatcher's stale-claim reconcile releases it (no queue matched): active again.
    _row(aid, rid, "queued", submitted_ago=0)
    _tick()
    st = _state(aid)
    assert (st.status, st.status_reason) == ("claimed", None)  # group (d) cleared it
    assert _hops(aid)[-1] == ("claimed", "claimed", "flag_cleared")
    _row(aid, rid, "completed")
    _result(rid, "hit")
    _tick()
    assert _state(aid).status == "settled"


async def test_a_fresh_submitting_row_is_not_stuck(db, business_user, lookups_on, alerts):
    """Visited (its other lead is mappable), and the in-flight one is not a blocker."""
    uid = business_user.id
    aid, job, (rid, done) = _claimed(uid, 2)
    _row(aid, rid, "submitting", submitted_ago=60)
    _row(aid, done, "errored")
    assert _tick()["mapped"] == 1
    st = _state(aid)
    assert (st.status, st.status_reason) == ("claimed", None) and alerts == []


async def test_the_verdict_is_the_recorded_decision_not_a_recomputed_rule(
    db, business_user, lookups_on,
):
    """AA2 / AD5: each queue's counts are then rewritten the OPPOSITE way to its
    recorded decision. The verdicts follow what billing did, never the rule re-run."""
    uid = business_user.id
    aid, job, (a, b, c) = _claimed(uid, 3)
    billed = _queue(uid, job, uploaded=0, sent=2, decision=True)     # rule now: False
    unbilled = _queue(uid, job, uploaded=5, sent=1, decision=False)  # rule now: True
    _row(aid, a, "unmatched", queue=billed)
    _row(aid, b, "unmatched", queue=billed)
    _row(aid, c, "unmatched", queue=unbilled)

    _tick()

    assert _verdicts(aid) == {a: "unmatched_billable", b: "unmatched_billable",
                              c: "unmatched_unbilled"}
    st = _state(aid)
    assert (st.status, st.billable_rows) == ("settled", 2)


@pytest.mark.parametrize("queue_row", ["undecided", "absent"])
async def test_an_unmatched_lead_without_a_billing_decision_is_flagged_never_guessed(
    db, business_user, lookups_on, alerts, queue_row,
):
    """No decision on its queue (NULL), or no queue row at all (AH3): the lead is not
    mapped either way, the action is flagged ONCE, and nothing settles. When a
    decision appears, the lead maps and the action settles.

    The other lead stays IN FLIGHT while the flag is raised, so nothing is mappable
    and nothing can settle: the blocker alone must select the action (group c)."""
    uid = business_user.id
    aid, job, (lead, other) = _claimed(uid, 2)
    qid = (_queue(uid, job, uploaded=1, sent=1) if queue_row == "undecided"
           else 7_000_000 + uuid.uuid4().int % 1_000_000_000)
    _row(aid, lead, "unmatched", queue=qid)

    _tick()
    _tick()

    assert _verdicts(aid)[lead] == "newly_queued"
    st = _state(aid)
    assert (st.status, st.status_reason) == ("claimed", rec.FLAG_BILLING_DECISION_UNKNOWN)
    assert [a for a in alerts if rec.FLAG_BILLING_DECISION_UNKNOWN in a[1]] != []
    assert len([a for a in alerts if aid in a[1]]) == 1

    if queue_row == "absent":
        qid = _queue(uid, job, uploaded=1, sent=1)
        _row(aid, lead, "unmatched", queue=qid)
    _decide(qid, False)
    _row(aid, other, "cancelled")
    _tick()

    assert _verdicts(aid)[lead] == "unmatched_unbilled"
    st = _state(aid)
    assert (st.status, st.status_reason) == ("settled", None)


# ── P1 expire, P2 lease, P3 re-publish ───────────────────────────────────────


async def test_an_action_never_started_expires_at_its_deadline_and_buys_nothing(
    db, business_user, lookups_on,
):
    job = _job(business_user.id)
    rids = _seed(business_user.id, job, [{}, {}])
    aid = _action(business_user.id, job, rids)
    _age(aid, cla.ACTION_DEADLINE_SECONDS + 1)
    assert _tick()["expired"] == 1
    st = _state(aid)
    assert (st.status, st.status_reason) == ("expired", "deadline")
    assert _verdicts(aid) == dict.fromkeys(rids, "abandoned")
    assert sorted(r for r, _f, _t, why in _events(aid) if r and why == "deadline") == sorted(rids)
    assert _pending(job) == {}
    assert cla.run_action(aid) == {"outcome": "not_dispatching"}


async def test_an_action_inside_its_deadline_is_not_expired(db, business_user, lookups_on):
    job = _job(business_user.id)
    [rid] = _seed(business_user.id, job, [{}])
    aid = _action(business_user.id, job, [rid])
    _age(aid, cla.ACTION_DEADLINE_SECONDS - 60)
    _tick()
    assert _state(aid).status == "dispatching"


async def test_an_expired_lease_is_taken_back_republished_and_then_claimed(
    db, business_user, lookups_on, monkeypatch,
):
    published: list = []
    real = cla.lookup_contacts.apply_async

    def spy(*args, **kwargs):
        published.append(kwargs.get("args"))
        return real(*args, **kwargs)

    monkeypatch.setattr(cla.lookup_contacts, "apply_async", spy)
    job = _job(business_user.id)
    [rid] = _seed(business_user.id, job, [{}])
    aid = _action(business_user.id, job, [rid])
    _age(aid, 120)
    _start(aid)  # T1, then the worker dies
    with system_sync_session() as s:
        s.execute(text("UPDATE contact_lookup_actions SET lease_expires_at = now() "
                       "- interval '1 second' WHERE id = :a"), {"a": aid})
        s.commit()
    out = _tick()
    assert out["lease_taken_back"] == 1 and out["republished"] == 1
    assert published == [[aid]]
    st = _state(aid)
    assert (st.status, st.status_reason, st.lease_token) == ("dispatching", "lease_expired", None)
    assert cla.run_action(aid)["outcome"] == "claimed"  # the delivery the publish makes
    assert set(_pending(job)) == {rid}


async def test_republish_waits_a_minute_then_every_five_and_stamps_each_attempt(
    db, business_user, lookups_on, monkeypatch,
):
    published: list = []
    monkeypatch.setattr(cla.lookup_contacts, "apply_async",
                        lambda *a, **k: published.append(k.get("args")))
    job = _job(business_user.id)
    [rid] = _seed(business_user.id, job, [{}])
    aid = _action(business_user.id, job, [rid])
    _tick()
    assert published == []  # confirmed seconds ago: the API's own publish gets first try
    _age(aid, 90)
    _tick()
    assert published == [[aid]] and _state(aid).status == "dispatching"
    with system_sync_session() as s:
        stamped = s.execute(text("SELECT dispatched_at > now() - interval '1 minute' "
                                 "FROM contact_lookup_actions WHERE id = :a"),
                            {"a": aid}).scalar_one()
    assert stamped
    _tick()
    assert published == [[aid]]  # not again within five minutes


async def test_an_action_past_its_deadline_is_never_republished_even_if_p1_is_full(
    db, business_user, lookups_on, monkeypatch,
):
    """P1 normally expires it first in the same tick; when P1's LIMIT is full, P3's own
    deadline predicate is what stops a pointless publish (T1 would refuse it anyway)."""
    published: list = []
    monkeypatch.setattr(cla.lookup_contacts, "apply_async",
                        lambda *a, **k: published.append(1))
    monkeypatch.setattr(rec, "_EXPIRE_LIMIT", 0)
    job = _job(business_user.id)
    [rid] = _seed(business_user.id, job, [{}])
    aid = _action(business_user.id, job, [rid])
    _age(aid, cla.ACTION_DEADLINE_SECONDS + 1)
    _tick()
    assert published == [] and _state(aid).status == "dispatching"


async def test_a_failed_publish_is_still_stamped_so_it_rotates(
    db, business_user, lookups_on, monkeypatch,
):
    def broker_down(*_a, **_k):
        raise ConnectionError("broker down")

    monkeypatch.setattr(cla.lookup_contacts, "apply_async", broker_down)
    job = _job(business_user.id)
    [rid] = _seed(business_user.id, job, [{}])
    aid = _action(business_user.id, job, [rid])
    _age(aid, 90)
    out = _tick()
    assert out["publish_failed"] == 1 and out["republished"] == 0
    with system_sync_session() as s:
        assert s.execute(text("SELECT dispatched_at IS NOT NULL FROM contact_lookup_actions "
                              "WHERE id = :a"), {"a": aid}).scalar_one()


async def test_the_kill_switch_stops_republishing_and_the_deadline_expires_it(
    db, business_user, lookups_on, monkeypatch,
):
    from src.config import settings

    published: list = []
    monkeypatch.setattr(cla.lookup_contacts, "apply_async",
                        lambda *a, **k: published.append(1))
    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", False)
    job = _job(business_user.id)
    [rid] = _seed(business_user.id, job, [{}])
    aid = _action(business_user.id, job, [rid])
    _age(aid, 600)
    _tick()
    assert published == [] and _state(aid).status == "dispatching"
    _age(aid, cla.ACTION_DEADLINE_SECONDS + 1)
    _tick()
    assert _state(aid).status == "expired" and _verdicts(aid) == {rid: "abandoned"}


# ── Locks and fairness ───────────────────────────────────────────────────────


async def test_an_action_locked_by_someone_else_is_skipped_not_waited_on(
    db, business_user, lookups_on,
):
    job = _job(business_user.id)
    [rid] = _seed(business_user.id, job, [{}])
    aid = _action(business_user.id, job, [rid])
    _age(aid, cla.ACTION_DEADLINE_SECONDS + 1)
    with system_sync_session() as holder:
        holder.execute(text("SELECT 1 FROM contact_lookup_actions WHERE id = :a FOR UPDATE"),
                       {"a": aid})
        done = threading.Event()
        out: list = []

        def tick():
            out.append(_tick())
            done.set()

        threading.Thread(target=tick, daemon=True).start()
        assert done.wait(30), "the reconciler waited on a locked action"
        holder.rollback()
    assert out[0]["expired"] == 0 and _state(aid).status == "dispatching"
    _tick()
    assert _state(aid).status == "expired"


async def test_a_permanently_blocked_action_never_starves_the_others(
    db, business_user, lookups_on, alerts, monkeypatch,
):
    for name in ("_MAPPABLE_LIMIT", "_SETTLE_LIMIT", "_STUCK_LIMIT", "_UNFLAG_LIMIT"):
        monkeypatch.setattr(rec, name, 1)
    uid = business_user.id
    # Claimed FIRST (the oldest claimed_at), each blocked for good: a missing row, and a
    # completed lookup whose lead is neither hit nor miss (never "mappable", AA4/AA5).
    blocked, _job_b, _ = _claimed(uid, 1)
    with system_sync_session() as s:
        s.execute(text("DELETE FROM pending_skip_trace_rows WHERE action_id = :a"),
                  {"a": blocked})
        s.commit()
    odd, _job_c, (odd_rid,) = _claimed(uid, 1)
    _row(odd, odd_rid, "completed")
    _result(odd_rid, "purged")
    # And an `unmatched` lead whose queue holds no billing decision (O-C): it must not
    # count as mappable either, or it would hold (a)'s slot on every tick.
    undecided, job_u, (und_rid,) = _claimed(uid, 1)
    _row(undecided, und_rid, "unmatched", queue=_queue(uid, job_u, uploaded=1, sent=1))
    # Each other action has one answered lead AND one still in flight, so the ONLY
    # group that can reach it is (a): a blocked action holding (a)'s slot would starve it.
    others = []
    for _ in range(3):
        aid, _job_o, (rid, waiting) = _claimed(uid, 2)
        _row(aid, rid, "completed")
        _result(rid, "hit")
        others.append((aid, rid, waiting))
    for _ in range(4):
        _tick()
    assert [_verdicts(a)[r] for a, r, _w in others] == ["answered_hit"] * 3
    assert [_verdicts(a)[w] for a, _r, w in others] == ["newly_queued"] * 3
    assert _state(blocked).status_reason == rec.FLAG_PENDING_ROW_MISSING
    assert _state(odd).status_reason == rec.FLAG_RESULT_STATE_UNEXPECTED
    assert _state(undecided).status_reason == rec.FLAG_BILLING_DECISION_UNKNOWN
    assert len(alerts) == 3


async def test_one_action_with_many_rows_is_one_candidate(db, business_user, lookups_on):
    uid = business_user.id
    big, _j, rids = _claimed(uid, 6)
    for rid in rids:
        _row(big, rid, "errored")
    with system_sync_session() as s:
        assert rec._p4_candidates(s) == [big]


# ── 2c-ii diff review r1 (AC1-AC4) ───────────────────────────────────────────


async def test_a_locked_prefix_does_not_starve_the_actions_behind_it(
    db, business_user, lookups_on, monkeypatch,
):
    """AC1: with LIMIT 1, the oldest candidate held by another transaction is skipped
    BEFORE the limit applies, so the next one is still expired this tick."""
    monkeypatch.setattr(rec, "_EXPIRE_LIMIT", 1)
    job = _job(business_user.id)
    first, second = _seed(business_user.id, job, [{}, {}])
    held = _action(business_user.id, job, [first])
    behind = _action(business_user.id, job, [second])
    _age(held, cla.ACTION_DEADLINE_SECONDS + 120)    # the oldest: first in line
    _age(behind, cla.ACTION_DEADLINE_SECONDS + 60)
    with system_sync_session() as holder:
        holder.execute(text("SELECT 1 FROM contact_lookup_actions WHERE id = :a FOR UPDATE"),
                       {"a": held})
        out: list = []
        t = threading.Thread(target=lambda: out.append(_tick()), daemon=True)
        t.start()
        t.join(30)
        holder.rollback()
    assert out and out[0]["expired"] == 1
    assert (_state(held).status, _state(behind).status) == ("dispatching", "expired")


async def test_one_failing_action_does_not_stop_the_rest_of_its_pass(
    db, business_user, lookups_on, monkeypatch,
):
    """AC2: fault injection on ONE action's abandon step; the other still expires, and
    the failed one is rolled back whole (still dispatching, still quoted)."""
    job = _job(business_user.id)
    r1, r2 = _seed(business_user.id, job, [{}, {}])
    bad = _action(business_user.id, job, [r1])
    good = _action(business_user.id, job, [r2])
    _age(bad, cla.ACTION_DEADLINE_SECONDS + 120)
    _age(good, cla.ACTION_DEADLINE_SECONDS + 60)
    real = cla._quoted_ids

    def flaky(db_, aid, uid):
        if aid == bad:
            raise RuntimeError("injected")
        return real(db_, aid, uid)

    monkeypatch.setattr(cla, "_quoted_ids", flaky)
    out = _tick()
    assert out["errors"] == 1 and out["expired"] == 1
    assert _state(good).status == "expired"
    assert (_state(bad).status, _verdicts(bad)) == ("dispatching", {r1: "quoted"})


async def test_a_lead_with_an_old_terminal_row_and_an_active_one_is_not_mappable(
    db, business_user, lookups_on,
):
    """AC3: should a lead ever carry two rows, the ACTIVE one decides, in the selection
    exactly as in the visit: the action is not a candidate and nothing settles."""
    uid = business_user.id
    aid, job, (rid,) = _claimed(uid, 1)  # its row is 'queued' (active)
    with system_sync_session() as s:
        # The active row made OLDER, and a terminal row for the same lead made NEWER:
        # recency alone would pick the terminal one, so only active-first decides.
        s.execute(text("UPDATE pending_skip_trace_rows SET enqueued_at = now() "
                       "- interval '2 days' WHERE action_id = :a"), {"a": aid})
        s.execute(text(
            "INSERT INTO pending_skip_trace_rows (id, job_id, result_id, user_id, "
            "  property_address, trace_type, status, enqueued_at, action_id) "
            "VALUES (:i, :j, :r, :u, '1 OLD ST', 'normal', 'cancelled', now(), :a)"),
            {"i": str(uuid.uuid4()), "j": job, "r": rid, "u": uid, "a": aid})
        s.commit()
    with system_sync_session() as s:
        assert aid not in rec._p4_candidates(s)
    _tick()
    assert _verdicts(aid) == {rid: "newly_queued"} and _state(aid).status == "claimed"


def test_the_reconciler_runs_every_two_minutes_on_beat():
    import src.workers.scheduler  # noqa: F401 - registers the beat schedule and the task
    from src.workers import app

    entry = app.conf.beat_schedule["reconcile-contact-lookups"]
    assert entry["task"] == "src.workers.scheduler.reconcile_contact_lookups"
    assert entry["schedule"] == 120.0
    assert entry["task"] in app.tasks


async def test_an_action_locked_between_selection_and_visit_is_skipped_not_waited_on(
    db, business_user, lookups_on, monkeypatch,
):
    """The selection skips held rows, so the visit's own SKIP LOCKED guards the gap
    after it: a pass-through spy lets the real selection run, then another session
    takes the row before the visit. The tick must skip it, not wait."""
    job = _job(business_user.id)
    [rid] = _seed(business_user.id, job, [{}])
    aid = _action(business_user.id, job, [rid])
    _age(aid, cla.ACTION_DEADLINE_SECONDS + 1)
    holder = system_sync_session().__enter__()
    real = rec._candidates

    def select_then_lock(db_, predicate, order, limit, params):
        ids = real(db_, predicate, order, limit, params)
        if aid in ids:
            holder.execute(text("SELECT 1 FROM contact_lookup_actions WHERE id = :a "
                                "FOR UPDATE"), {"a": aid})
        return ids

    monkeypatch.setattr(rec, "_candidates", select_then_lock)
    out: list = []
    t = threading.Thread(target=lambda: out.append(_tick()), daemon=True)
    t.start()
    t.join(20)
    finished = not t.is_alive()
    holder.rollback()
    holder.close()
    t.join(30)
    assert finished, "the visit waited on a row another session held"
    assert out[0]["expired"] == 0 and _state(aid).status == "dispatching"
