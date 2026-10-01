"""The contact-lookup action worker, `lookup_contacts` (Phase 1b-2, 2b).

Contract: tasks/todo-lookup-contacts.md, "1b-2b" + "2b BUILD SPEC" (V1, W1, V8/W7,
Y1-Y4, Z1-Z3). Actions are seeded exactly as the 2d confirm will write them: an
action in `dispatching`, one `quoted` verdict per lead, the `-> dispatching` event.

Real PG + Redis, the real claim, the real scrape enqueue and the real dispatcher
sweep. No Tracerfy: nothing here gets past the queue.
"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from src.config import settings
from src.db.models import (
    CONTACT_LOOKUP_ACTION_STATUSES,
    CONTACT_LOOKUP_DISPOSITIONS,
    PendingSkipTraceRow,
    Result,
    SkipTraceCache,
)
from src.db.session import system_sync_session
from src.scrapers.enrichment.skip_trace import build_pending_row_payload, payload_subject_key
from src.workers import contact_lookup_action as cla
from src.workers.skip_trace_claim import lock_job_for_claim
from tests.test_audit4_paid_skip_trace_gate import _account
from tests.test_contact_lookup_planner import _branch_specs, _job, _seed, _tagged

_POLICY_OFF = {"policy": {"pierce_cv_owner_skip_trace_enabled": False}}


@pytest.fixture
def lookups_on(monkeypatch):
    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "test-token-not-real")


# ── Seeding and reading ──────────────────────────────────────────────────────


def _action(user_id: str, job_id: str, result_ids: list[str], *, quoted_count=None,
            snapshot=None) -> str:
    """An action as the 2d confirm writes it."""
    import json

    aid = str(uuid.uuid4())
    with system_sync_session() as s:
        s.execute(text(
            "INSERT INTO contact_lookup_actions (id, user_id, job_id, category, quote_id, "
            "status, unit_price_cents, currency, pricing_version, quoted_count, quote_snapshot) "
            "VALUES (:a, :u, :j, 'new', :q, 'dispatching', 8, 'USD', '2026-06', :n, "
            "        CAST(:snap AS jsonb))"
        ), {"a": aid, "u": user_id, "j": job_id, "q": f"q-{aid}",
            "n": len(result_ids) if quoted_count is None else quoted_count,
            "snap": json.dumps(_POLICY_OFF if snapshot is None else snapshot)})
        for rid in result_ids:
            s.execute(text(
                "INSERT INTO contact_lookup_action_results (id, action_id, user_id, result_id, "
                "disposition) VALUES (:i, :a, :u, :r, 'quoted')"
            ), {"i": str(uuid.uuid4()), "a": aid, "u": user_id, "r": rid})
        s.execute(text(
            "INSERT INTO contact_lookup_action_events (id, action_id, user_id, to_status) "
            "VALUES (:i, :a, :u, 'dispatching')"
        ), {"i": str(uuid.uuid4()), "a": aid, "u": user_id})
        s.commit()
    return aid


def _state(aid: str):
    with system_sync_session() as s:
        return s.execute(text(
            "SELECT status, status_reason, lease_token, lease_expires_at, started_at, "
            "       claimed_at, claimed_count, reused_count, newly_queued_count, "
            "       tracerfy_credits, billable_rows "
            "FROM contact_lookup_actions WHERE id = :a"), {"a": aid}).one()


def _verdicts(aid: str) -> dict[str, str]:
    with system_sync_session() as s:
        return {str(r): d for r, d in s.execute(text(
            "SELECT result_id, disposition FROM contact_lookup_action_results "
            "WHERE action_id = :a"), {"a": aid}).all()}


def _events(aid: str) -> list[tuple]:
    with system_sync_session() as s:
        return [(str(r) if r else None, f, t, why) for r, f, t, why in s.execute(text(
            "SELECT result_id, from_status, to_status, reason FROM contact_lookup_action_events "
            "WHERE action_id = :a ORDER BY at, id"), {"a": aid}).all()]


def _hops(aid: str) -> list[tuple]:
    """The action-level events (no lead), oldest first, as (from, to, reason)."""
    return [(f, t, why) for rid, f, t, why in _events(aid) if rid is None]


def _pending(job_id: str) -> dict[str, tuple]:
    with system_sync_session() as s:
        return {str(r): (str(a) if a else None, st, tt) for r, a, st, tt in s.execute(text(
            "SELECT result_id, action_id, status, trace_type FROM pending_skip_trace_rows "
            "WHERE job_id = :j"), {"j": job_id}).all()}


def _cache(user_id: str, result_id: str, *, phone: str = "2065550133") -> None:
    """A fresh cache entry for exactly the lookup this lead would buy."""
    with system_sync_session() as s:
        rec = s.get(Result, result_id)
        key = payload_subject_key(user_id, build_pending_row_payload(rec))
        s.add(SkipTraceCache(address_hash=key, phone=phone, phone_type="Mobile",
                             phones=[{"number": phone, "type": "Mobile"}],
                             fetched_at=datetime.now(UTC)))
        s.commit()


def _charged_unanswered(user_id: str, dedup_hash: str) -> None:
    """An earlier delivered lead whose lookup Tracerfy charged and we could not match."""
    earlier = _job(user_id)
    [original] = _seed(user_id, earlier, [{"property_address": "9 OLD ST",
                                          "dedup_hash": dedup_hash,
                                          "skip_trace_status": "errored"}])
    with system_sync_session() as s:
        s.add(PendingSkipTraceRow(
            job_id=earlier, result_id=original, user_id=user_id, property_address="9 OLD ST",
            city="VANCOUVER", state="WA", trace_type="normal", status="unmatched",
            enqueued_at=datetime.now(UTC) - timedelta(days=1)))
        s.commit()


# ── The transition matrix (V8 / W7) ──────────────────────────────────────────


def test_the_matrices_name_only_real_states():
    assert set(cla.ACTION_TRANSITIONS) == set(CONTACT_LOOKUP_ACTION_STATUSES)
    for targets in cla.ACTION_TRANSITIONS.values():
        assert targets <= set(CONTACT_LOOKUP_ACTION_STATUSES)
    for frm, targets in cla.VERDICT_TRANSITIONS.items():
        assert {frm, *targets} <= set(CONTACT_LOOKUP_DISPOSITIONS)
    # Every terminal answer the reconciler derives is reachable from newly_queued.
    assert cla.VERDICT_TRANSITIONS["newly_queued"] == {
        "answered_hit", "answered_miss", "unmatched_billable", "unmatched_unbilled",
        "errored_unsubmitted", "released"}


@pytest.mark.parametrize(("frm", "to"), [
    ("settled", "running"), ("failed", "claimed"), ("claimed", "running"),
    ("expired", "dispatching"), ("dispatching", "claimed"), ("claimed", "failed"),
])
def test_an_illegal_action_move_raises_before_any_sql(frm, to):
    token = "t" if to == "running" else None  # so only the MATRIX can refuse it
    with pytest.raises(cla.IllegalTransitionError, match=f"action '{frm}' -> '{to}'"):
        cla._move(None, str(uuid.uuid4()), frm, to, new_token=token)


@pytest.mark.parametrize(("frm", "to"), [
    ("answered_hit", "quoted"), ("reused", "newly_queued"), ("quoted", "answered_hit"),
    ("newly_queued", "reused"), ("abandoned", "newly_queued"),
])
def test_an_illegal_verdict_move_raises_before_any_sql(frm, to):
    with pytest.raises(cla.IllegalTransitionError):
        cla._set_verdicts(None, str(uuid.uuid4()), str(uuid.uuid4()), frm,
                          {str(uuid.uuid4()): to})


def test_a_lease_starts_exactly_on_entering_running():
    with pytest.raises(cla.IllegalTransitionError):
        cla._move(None, "a", "dispatching", "running")  # no token
    with pytest.raises(cla.IllegalTransitionError):
        cla._move(None, "a", "running", "claimed", new_token="t")


async def test_a_decided_verdict_is_never_rewritten(db, business_user):
    job = _job(business_user.id)
    [rid] = _seed(business_user.id, job, [{}])
    aid = _action(business_user.id, job, [rid])
    with system_sync_session() as s:
        cla._set_verdicts(s, aid, business_user.id, "quoted", {rid: "reused"})
        s.commit()
        with pytest.raises(RuntimeError, match="0 of 1"):
            cla._set_verdicts(s, aid, business_user.id, "quoted", {rid: "newly_queued"})
        s.rollback()
    assert _verdicts(aid) == {rid: "reused"}


# ── The happy path and every D1 row ──────────────────────────────────────────


async def test_every_quoted_lead_gets_exactly_one_verdict(db, business_user, lookups_on):
    uid = business_user.id
    _charged_unanswered(uid, "h-charged")
    job = _job(uid)
    ids = _tagged(uid, job, [
        {"id": "normal"},
        {"id": "advanced", "party_name": None},
        {"id": "cached"},
        {"id": "charged", "is_duplicate": True, "duplicate_reason": "prior_run",
         "dedup_hash": "h-charged"},
        # Quoted, then something happened before the worker ran:
        {"id": "queued_since"}, {"id": "answered_since"}, {"id": "errored_since"},
        {"id": "over_quota", "enrichment_data": {"delivery_excluded_reason": "over_quota"}},
        {"id": "superseded", "is_duplicate": True, "duplicate_reason": "superseded"},
        {"id": "no_address", "property_address": None, "mailing_address": None},
        {"id": "null_property", "property_address": None,
         "mailing_address": "9 ELM ST, SEATTLE, WA 98101"},
        {"id": "placeholder", "property_address": "UNKNOWN UNKNOWN, VANCOUVER WA 98661"},
        {"id": "not_quoted"},
    ])
    for tag in ("cached", "queued_since", "errored_since"):
        _cache(uid, ids[tag])
    quoted = [v for k, v in ids.items() if k != "not_quoted"]
    aid = _action(uid, job, quoted)
    with system_sync_session() as s:
        for tag, status in (("queued_since", "queued"), ("answered_since", "hit"),
                            ("errored_since", "errored")):
            s.execute(text("UPDATE results SET skip_trace_status = :st WHERE id = :i"),
                      {"st": status, "i": ids[tag]})
        s.commit()

    out = cla.run_action(aid)

    expected = {
        "normal": "newly_queued", "advanced": "newly_queued", "cached": "reused",
        "charged": "already_answered", "queued_since": "in_progress_elsewhere",
        "answered_since": "already_answered", "errored_since": "ineligible",
        "over_quota": "ineligible", "superseded": "ineligible", "no_address": "ineligible",
        "null_property": "ineligible", "placeholder": "excluded_placeholder_address",
    }
    assert _verdicts(aid) == {ids[t]: v for t, v in expected.items()}
    assert _pending(job) == {ids["normal"]: (aid, "queued", "normal"),
                             ids["advanced"]: (aid, "queued", "advanced")}
    st = _state(aid)
    assert (st.status, st.status_reason, st.lease_token, st.lease_expires_at) == (
        "claimed", None, None, None)  # 15-15: the lease is cleared at claim
    assert st.started_at is not None and st.claimed_at is not None
    assert (st.newly_queued_count, st.reused_count, st.claimed_count, st.tracerfy_credits,
            st.billable_rows) == (2, 1, 3, 3, 0)
    assert out == {"outcome": "claimed", "newly_queued_count": 2, "reused_count": 1,
                   "claimed_count": 3, "tracerfy_credits": 3, "quoted": 12}
    assert _hops(aid) == [(None, "dispatching", None), ("dispatching", "running", None),
                          ("running", "claimed", None)]
    with system_sync_session() as s:
        cached = s.get(Result, ids["cached"])
        assert (cached.skip_trace_status, cached.skip_trace_source, cached.phone) == (
            "hit", "reused", "2065550133")
        # Settled exactly as the enqueue settles it (the shared helper).
        assert s.get(Result, ids["charged"]).skip_trace_status == "errored"
        # A lead in flight elsewhere, or already attempted, is never overwritten by a
        # cached answer: its cache entry is deliberately fresh.
        for tag, status in (("queued_since", "queued"), ("errored_since", "errored")):
            lead = s.get(Result, ids[tag])
            assert (lead.skip_trace_status, lead.phone, lead.skip_trace_source) == (
                status, None, None)
        assert s.get(Result, ids["not_quoted"]).skip_trace_status == "not_attempted"


@pytest.mark.parametrize("atip_allowed", [False, True])
async def test_the_action_claims_exactly_what_the_scrape_enqueue_claims(
    db, lookups_on, redis_client, monkeypatch, atip_allowed,
):
    """PARITY: the same leads, one account through the real enqueue and one through
    the action, buy the same leads with the same trace types."""
    from tests.test_contact_lookup_planner import _enqueue

    monkeypatch.setattr(settings, "PIERCE_CV_OWNER_SKIP_TRACE_ENABLED", atip_allowed)
    scrape_user = await _account(db, plan="business")
    action_user = await _account(db, plan="business")
    scrape_job = _job(scrape_user.id, record_type="code_violation", status="enriching")
    action_job = _job(action_user.id, record_type="code_violation")
    specs, _ = _branch_specs()
    scrape_ids = _tagged(scrape_user.id, scrape_job, [dict(s) for s in specs])
    action_ids = _tagged(action_user.id, action_job, [dict(s) for s in specs])

    _enqueue(scrape_job, redis_client)
    aid = _action(action_user.id, action_job, list(action_ids.values()),
                  snapshot={"policy": {"pierce_cv_owner_skip_trace_enabled": atip_allowed}})
    cla.run_action(aid)

    by_scrape = {v: k for k, v in scrape_ids.items()}
    by_action = {v: k for k, v in action_ids.items()}
    scraped = {by_scrape[r]: t for r, (_a, _s, t) in _pending(scrape_job).items()}
    bought = {by_action[r]: t for r, (_a, _s, t) in _pending(action_job).items()}
    assert bought == scraped
    assert {"normal", "advanced", "open_cv"} <= set(bought)
    assert ("tacoma_atip" in bought) is atip_allowed


# ── Pinned policy (15-14, Y4) ────────────────────────────────────────────────


@pytest.mark.parametrize(("snapshot", "current", "bought"), [
    ({"policy": {"pierce_cv_owner_skip_trace_enabled": True}}, True, True),
    ({"policy": {"pierce_cv_owner_skip_trace_enabled": True}}, False, False),
    ({"policy": {"pierce_cv_owner_skip_trace_enabled": False}}, True, False),
    ({"policy": {"pierce_cv_owner_skip_trace_enabled": "true"}}, True, False),
    ({"policy": {}}, True, False),
    ({}, True, False),
])
async def test_the_pinned_policy_only_ever_excludes_more(
    db, business_user, lookups_on, monkeypatch, snapshot, current, bought,
):
    from tests.test_contact_lookup_planner import _tacoma

    monkeypatch.setattr(settings, "PIERCE_CV_OWNER_SKIP_TRACE_ENABLED", current)
    job = _job(business_user.id, record_type="code_violation")
    ed, pin = _tacoma()
    [rid] = _seed(business_user.id, job, [{
        "enrichment_data": ed, "parcel_id": pin, "property_address": "7 ELM ST",
        "property_city": "TACOMA", "property_zip": "98402"}])
    aid = _action(business_user.id, job, [rid], snapshot=snapshot)
    cla.run_action(aid)
    assert _verdicts(aid) == {rid: "newly_queued" if bought else "excluded_atip_policy"}
    assert bool(_pending(job)) is bought


@pytest.mark.parametrize(("before", "after", "credits"), [
    ({}, {"party_name": None}, 2),             # normal at quote, advanced now
    ({"party_name": None}, {"party_name": "SAARENAS AVELINO G"}, 1),
])
async def test_a_trace_type_that_drifted_since_the_quote_is_bought_as_it_is_now(
    db, business_user, lookups_on, before, after, credits,
):
    job = _job(business_user.id)
    [rid] = _seed(business_user.id, job, [before])
    aid = _action(business_user.id, job, [rid])
    with system_sync_session() as s:
        s.get(Result, rid).party_name = after["party_name"]
        s.commit()
    cla.run_action(aid)
    assert _state(aid).tracerfy_credits == credits
    assert _pending(job)[rid][2] == ("advanced" if credits == 2 else "normal")


# ── Gates ────────────────────────────────────────────────────────────────────


async def test_the_kill_switch_makes_the_action_wait_then_it_runs(
    db, business_user, lookups_on, monkeypatch,
):
    job = _job(business_user.id)
    [rid] = _seed(business_user.id, job, [{}])
    aid = _action(business_user.id, job, [rid])
    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", False)
    assert cla.run_action(aid) == {"outcome": "waiting", "reason": "kill_switch"}
    st = _state(aid)
    assert (st.status, st.status_reason, st.lease_token, st.lease_expires_at) == (
        "dispatching", "kill_switch", None, None)
    assert _verdicts(aid) == {rid: "quoted"} and _pending(job) == {}

    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
    assert cla.run_action(aid)["outcome"] == "claimed"  # the reconciler's re-drive
    assert _hops(aid)[1:] == [("dispatching", "running", None),
                              ("running", "dispatching", "kill_switch"),
                              ("dispatching", "running", None),
                              ("running", "claimed", None)]


async def test_an_empty_token_is_the_kill_switch_too(db, business_user, lookups_on,
                                                     monkeypatch):
    job = _job(business_user.id)
    [rid] = _seed(business_user.id, job, [{}])
    aid = _action(business_user.id, job, [rid])
    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "")
    assert cla.run_action(aid)["reason"] == "kill_switch"
    assert _state(aid).status == "dispatching"


@pytest.mark.parametrize("job_status", ["enriching", "failed", "cancelled"])
async def test_a_job_that_did_not_deliver_fails_and_abandons_every_lead(
    db, business_user, lookups_on, job_status,
):
    job = _job(business_user.id, status=job_status)
    rids = _seed(business_user.id, job, [{}, {}])
    aid = _action(business_user.id, job, rids)
    assert cla.run_action(aid) == {"outcome": "failed", "reason": "job_not_delivered"}
    assert _verdicts(aid) == dict.fromkeys(rids, "abandoned")
    assert _pending(job) == {}
    lead_events = [(r, f, t, why) for r, f, t, why in _events(aid) if r]
    assert sorted(lead_events) == sorted(
        (r, "quoted", "abandoned", "job_not_delivered") for r in rids)
    st = _state(aid)
    assert (st.status, st.status_reason, st.lease_token) == ("failed", "job_not_delivered", None)


@pytest.mark.parametrize(("account", "reason"), [
    ({"plan": "starter"}, "plan_not_eligible"),
    ({"plan": "pro", "subscription_status": "unpaid"}, "access_frozen"),
    ({"plan": "pro", "subscription_status": "active",
      "entitlement_ends_at": datetime.now(UTC) - timedelta(minutes=1)}, "access_ended"),
])
async def test_an_account_that_may_not_buy_fails_and_abandons(
    db, lookups_on, account, reason,
):
    user = await _account(db, **account)
    job = _job(user.id)
    [rid] = _seed(user.id, job, [{}])
    aid = _action(user.id, job, [rid])
    assert cla.run_action(aid) == {"outcome": "failed", "reason": reason}
    assert _verdicts(aid) == {rid: "abandoned"} and _pending(job) == {}


async def test_a_trial_buys_up_to_its_allowance_and_records_the_held_leads(
    db, lookups_on, monkeypatch,
):
    monkeypatch.setattr(settings, "SKIP_TRACE_TRIAL_CREDIT_ALLOWANCE", 3)
    trial = await _account(db, trial_ends_at=datetime.now(UTC) + timedelta(days=5))
    job = _job(trial.id)
    rids = _seed(trial.id, job, [{} for _ in range(5)])
    aid = _action(trial.id, job, rids)
    cla.run_action(aid)
    # The quote's window order decides who fits: (created_at, id), one created_at here.
    order = sorted(rids)
    assert _verdicts(aid) == {**dict.fromkeys(order[:3], "newly_queued"),
                              **dict.fromkeys(order[3:], "ineligible")}
    assert set(_pending(job)) == set(order[:3])
    held_events = sorted((r, why) for r, _f, t, why in _events(aid) if r)
    assert held_events == [(r, "trial_allowance") for r in order[3:]]


# ── V1: the durable quoted set ───────────────────────────────────────────────


async def test_a_quoted_set_that_shrank_fails_closed(db, business_user, lookups_on):
    job = _job(business_user.id)
    rids = _seed(business_user.id, job, [{}, {}])
    aid = _action(business_user.id, job, rids, quoted_count=3)  # one lead is gone
    assert cla.run_action(aid) == {"outcome": "failed", "reason": "quoted_set_mismatch"}
    assert _verdicts(aid) == dict.fromkeys(rids, "abandoned") and _pending(job) == {}


async def test_a_quoted_lead_of_another_job_is_never_bought(db, business_user, lookups_on):
    """Same tenant, other job: outside the action's job, so the durable set does not
    match and nothing at all is bought (V1, 20-3)."""
    job = _job(business_user.id)
    other = _job(business_user.id)
    [mine] = _seed(business_user.id, job, [{}])
    [theirs] = _seed(business_user.id, other, [{}])
    aid = _action(business_user.id, job, [mine, theirs])
    assert cla.run_action(aid)["reason"] == "quoted_set_mismatch"
    assert _pending(job) == {} and _pending(other) == {}


async def test_a_quoted_lead_outside_the_job_fails_closed_even_when_the_count_matches(
    db, business_user, lookups_on,
):
    """quoted_count names only the in-job lead, so a job-scoped count alone would pass
    and leave the outsider `quoted` with no verdict, forever."""
    job = _job(business_user.id)
    other = _job(business_user.id)
    [mine] = _seed(business_user.id, job, [{}])
    [theirs] = _seed(business_user.id, other, [{}])
    aid = _action(business_user.id, job, [mine, theirs], quoted_count=1)
    assert cla.run_action(aid)["reason"] == "quoted_set_mismatch"
    assert _verdicts(aid) == {mine: "abandoned", theirs: "abandoned"}
    assert _pending(job) == {} and _pending(other) == {}


# ── W1: the lease, the fence, redelivery ─────────────────────────────────────


def _start(aid: str) -> str:
    """T1 alone, as a worker that dies right after it."""
    token = uuid.uuid4().hex
    with system_sync_session() as s:
        assert cla._move(s, aid, "dispatching", "running", None, new_token=token)
        s.commit()
    return token


async def test_a_worker_killed_between_t1_and_t2_leaves_a_recoverable_lease(
    db, business_user, lookups_on,
):
    job = _job(business_user.id)
    [rid] = _seed(business_user.id, job, [{}])
    aid = _action(business_user.id, job, [rid])
    _start(aid)
    st = _state(aid)
    assert st.status == "running" and st.lease_token and st.started_at
    with system_sync_session() as s:
        left = s.execute(text("SELECT lease_expires_at - now() FROM contact_lookup_actions "
                              "WHERE id = :a"), {"a": aid}).scalar_one()
    assert timedelta(minutes=9) < left <= timedelta(minutes=10)
    assert _verdicts(aid) == {rid: "quoted"} and _pending(job) == {}
    # A redelivery while the lease lives does nothing (Y2)...
    assert cla.run_action(aid) == {"outcome": "not_dispatching"}
    # ...and once it expires, the reconciler's move (2c) hands it back and it runs.
    with system_sync_session() as s:
        s.execute(text("UPDATE contact_lookup_actions SET lease_expires_at = now() "
                       "- interval '1 second' WHERE id = :a"), {"a": aid})
        assert cla._move(s, aid, "running", "dispatching", "lease_expired")
        s.commit()
    assert cla.run_action(aid)["outcome"] == "claimed"
    assert set(_pending(job)) == {rid}


@pytest.mark.parametrize("lose", ["expired", "other_token", "moved_on"])
async def test_a_lost_fence_buys_nothing(db, business_user, lookups_on, lose):
    job = _job(business_user.id)
    [rid] = _seed(business_user.id, job, [{}])
    aid = _action(business_user.id, job, [rid])
    token = _start(aid)
    with system_sync_session() as s:
        if lose == "expired":
            s.execute(text("UPDATE contact_lookup_actions SET lease_expires_at = now() "
                           "- interval '1 second' WHERE id = :a"), {"a": aid})
        elif lose == "other_token":
            token = uuid.uuid4().hex
        else:
            assert cla._move(s, aid, "running", "dispatching", "lease_expired")
        s.commit()
    with system_sync_session() as s:
        assert cla._claim(s, aid, token) == {"outcome": "fenced"}
    assert _pending(job) == {} and _verdicts(aid) == {rid: "quoted"}


async def test_redelivery_buys_once(db, business_user, lookups_on):
    job = _job(business_user.id)
    rids = _seed(business_user.id, job, [{}, {}])
    aid = _action(business_user.id, job, rids)
    assert cla.run_action(aid)["outcome"] == "claimed"
    assert cla.run_action(aid) == {"outcome": "not_dispatching"}
    assert cla.lookup_contacts.run(aid) == {"outcome": "not_dispatching"}
    assert set(_pending(job)) == set(rids)


async def test_two_concurrent_deliveries_buy_once(db, business_user, lookups_on):
    job = _job(business_user.id)
    rids = _seed(business_user.id, job, [{} for _ in range(4)])
    aid = _action(business_user.id, job, rids)
    outs: list = []
    gate = threading.Barrier(2)

    def deliver():
        gate.wait()
        outs.append(cla.run_action(aid)["outcome"])

    threads = [threading.Thread(target=deliver) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert sorted(outs) == ["claimed", "not_dispatching"]
    with system_sync_session() as s:
        assert s.execute(text("SELECT count(*) FROM pending_skip_trace_rows "
                              "WHERE job_id = :j"), {"j": job}).scalar_one() == 4


def test_a_non_uuid_is_refused_without_a_query():
    assert cla.lookup_contacts.run("not-an-id") == {"outcome": "invalid_id"}


# ── Races with the scrape enqueue on the same leads ─────────────────────────


@pytest.mark.parametrize(("now", "verdict"), [
    ("hit", "already_answered"), ("submitted", "in_progress_elsewhere"),
    ("errored", "ineligible"),
])
async def test_a_lead_settled_by_another_writer_just_before_the_claim_takes_its_verdict_from_now(
    db, business_user, lookups_on, monkeypatch, now, verdict,
):
    """A writer that does not take the job lock (ingest, the dispatcher) settles the
    lead between our read and the claim. A pass-through spy lets that writer commit,
    then runs the REAL claim, which no longer finds the lead claimable."""
    from src.workers import skip_trace_claim

    job = _job(business_user.id)
    [rid] = _seed(business_user.id, job, [{}])
    aid = _action(business_user.id, job, [rid])
    real_claim = skip_trace_claim.claim_skip_trace_rows

    def settle_then_claim(db_, payloads, **kw):
        with system_sync_session() as other:
            other.execute(text("UPDATE results SET skip_trace_status = :s WHERE id = :i"),
                          {"s": now, "i": rid})
            other.commit()
        return real_claim(db_, payloads, **kw)

    monkeypatch.setattr(skip_trace_claim, "claim_skip_trace_rows", settle_then_claim)
    assert cla.run_action(aid)["outcome"] == "claimed"
    assert _verdicts(aid) == {rid: verdict}
    assert _pending(job) == {}


async def test_the_action_racing_the_scrape_enqueue_leaves_one_row_per_lead(
    db, business_user, lookups_on, redis_client,
):
    from tests.test_contact_lookup_planner import _enqueue

    job = _job(business_user.id)
    rids = _seed(business_user.id, job, [{} for _ in range(6)])
    aid = _action(business_user.id, job, rids)
    # Hold the job's claim lock so both writers queue behind it, then let go.
    holder = system_sync_session().__enter__()
    lock_job_for_claim(holder, job)
    errors: list = []

    def run(fn):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - surfaced by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(lambda: cla.run_action(aid),)),
               threading.Thread(target=run, args=(lambda: _enqueue(job, redis_client),))]
    for t in threads:
        t.start()
    # Prove the RACE: both writers must be blocked on this job's claim lock before it
    # is released, or the test would pass as two sequential runs (Codex 2b review P3).
    waiting = 0
    deadline = time.monotonic() + 20
    with system_sync_session() as probe:
        while waiting < 2 and time.monotonic() < deadline:
            waiting = probe.execute(text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
                "AND (classid, objid) = (SELECT classid, objid FROM pg_locks "
                "    WHERE locktype = 'advisory' AND granted AND pid = :holder)"
            ), {"holder": holder.execute(text("SELECT pg_backend_pid()")).scalar_one()}
            ).scalar_one()
            probe.rollback()
            time.sleep(0.05)
    holder.rollback()
    holder.close()
    assert waiting == 2, "both writers were not blocked on the job's claim lock"
    for t in threads:
        t.join(90)
    assert errors == []
    pending = _pending(job)
    assert set(pending) == set(rids)  # every lead exactly one row, none stranded
    verdicts = _verdicts(aid)
    for rid in rids:
        mine = pending[rid][0] == aid
        assert verdicts[rid] == ("newly_queued" if mine else "in_progress_elsewhere")
    assert _state(aid).newly_queued_count == sum(1 for p in pending.values() if p[0] == aid)


async def test_a_busy_claim_lock_makes_the_action_wait(
    db, business_user, lookups_on, monkeypatch,
):
    from src.workers import skip_trace_claim

    monkeypatch.setattr(skip_trace_claim, "_LOCK_WAIT_SECONDS", 1)
    job = _job(business_user.id)
    [rid] = _seed(business_user.id, job, [{}])
    aid = _action(business_user.id, job, [rid])
    with system_sync_session() as holder:
        lock_job_for_claim(holder, job)
        assert cla.run_action(aid) == {"outcome": "waiting", "reason": "claim_lock_busy"}
        holder.rollback()
    st = _state(aid)
    assert (st.status, st.status_reason, st.lease_token) == (
        "dispatching", "claim_lock_busy", None)
    assert _pending(job) == {} and _verdicts(aid) == {rid: "quoted"}


async def test_an_unexpected_error_waits_rolls_back_and_reraises(
    db, business_user, lookups_on, monkeypatch,
):
    """Fault injection (the only one here): an error mid-T2 must leave NOTHING bought
    or decided, hand the action back, and still surface."""
    from src.workers.tasks_helpers import enrich

    job = _job(business_user.id)
    [rid] = _seed(business_user.id, job, [{}])
    aid = _action(business_user.id, job, [rid])

    def boom(*_a, **_k):
        raise RuntimeError("injected")

    monkeypatch.setattr(enrich, "copy_cached_answer", boom)
    with pytest.raises(RuntimeError, match="injected"):
        cla.run_action(aid)
    st = _state(aid)
    assert (st.status, st.status_reason, st.lease_token) == ("dispatching", "worker_error", None)
    assert _pending(job) == {} and _verdicts(aid) == {rid: "quoted"}


# ── After the claim: the dispatcher's own guard (r5) ─────────────────────────


async def test_a_lead_over_quota_after_the_claim_is_withdrawn_before_any_submission(
    db, business_user, lookups_on,
):
    from src.workers.skip_trace_dispatcher import _cancel_undeliverable_queued

    job = _job(business_user.id)
    [rid] = _seed(business_user.id, job, [{}])
    aid = _action(business_user.id, job, [rid])
    cla.run_action(aid)
    assert _pending(job)[rid][1] == "queued"
    with system_sync_session() as s:
        s.execute(text("UPDATE results SET enrichment_data = "
                       "'{\"delivery_excluded_reason\": \"over_quota\"}'::jsonb WHERE id = :i"),
                  {"i": rid})
        s.commit()
        assert _cancel_undeliverable_queued(s) == 1
        s.commit()
    assert _pending(job)[rid] == (aid, "cancelled", "normal")
