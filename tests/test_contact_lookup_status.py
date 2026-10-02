"""GET /jobs/{job_id}/contact-lookups[/{action_id}]: the status (Phase 1b-2e).

Contract: tasks/todo-lookup-contacts.md, "## Phase 1b-2e — the status endpoint" as
amended by AP1-AP4, AQ1-AQ3 and AR1.

Real PG and real Redis, through the API client. Every state is reached by the REAL
chain: the quote and confirm endpoints, the worker (`cla.run_action`), and the
reconciler's tick; pending rows are moved exactly as ingest and the dispatcher leave
them (the reconciler tests' helpers). The pause reader is observed with a PASS-THROUGH
spy. Two writes are labelled: a `status_reason` no writer produces yet (the mapping's
`other` case), and a category flip for the list filter.
"""
from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import redis.asyncio as aioredis
from sqlalchemy import text

from src.api.routes import jobs as jobs_routes
from src.api.schemas import ContactLookupOutcomes
from src.config import settings
from src.db.models import CONTACT_LOOKUP_DISPOSITIONS
from src.db.session import system_sync_session
from src.utils.skip_trace_pause_state import ScopeResume
from src.workers import contact_lookup_action as cla
from src.workers.scheduler_helpers import contact_lookups as rec
from tests.test_contact_lookup_action import _action, _age, _start
from tests.test_contact_lookup_confirm import _confirm, _quoted
from tests.test_contact_lookup_quote import (
    _auth,
    _blackhole,
    _free_port,
    _job,
    _publish,
    _seed,
    _set_user,
    rate_limit_module,
)
from tests.test_contact_lookup_reconciler import _queue, _result, _row, _tick

_BUCKETS = tuple(ContactLookupOutcomes.model_fields)


# ── fixtures and helpers ─────────────────────────────────────────────────────


@pytest.fixture
def _lookups_on(monkeypatch):
    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "test-token-not-real")


@pytest.fixture(autouse=True)
def _fresh_lookup_client():
    jobs_routes._lookup_redis_client = None
    yield
    client, jobs_routes._lookup_redis_client = jobs_routes._lookup_redis_client, None
    if client is not None:
        client.close()


@pytest.fixture
def _other_clients_built():
    """Build auth's and the limiter's clients on the GOOD url first, so a test that
    then breaks REDIS_URL breaks only the route's own client."""
    from src.api.middleware import auth_hardening

    auth_hardening._get_redis()
    rate_limit_module._get_redis()


async def _bought(client, token: str, user_id: str, n: int = 1) -> tuple[str, str, list[str]]:
    """A run with `n` leads, quoted and confirmed through the real endpoints."""
    job = _job(user_id)
    rids = sorted(_seed(user_id, job, [{} for _ in range(n)]))
    r = await _confirm(client, token, job, await _quoted(client, token, job))
    assert r.status_code == 202, r.text
    return job, r.json()["action_id"], rids


async def _status(client, token: str, job: str, aid: str):
    return await client.get(f"/jobs/{job}/contact-lookups/{aid}", headers=_auth(token))


async def _ok(client, token: str, job: str, aid: str) -> dict:
    r = await _status(client, token, job, aid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert sum(body["outcomes"].values()) == body["quoted_count"]  # every lead, once
    assert body["billable"] == body["outcomes"]["found"] + body["outcomes"]["not_found"]
    return body


async def _list(client, token: str, job: str, **params):
    return await client.get(f"/jobs/{job}/contact-lookups", params=params,
                            headers=_auth(token))


def _only(**nonzero) -> dict:
    return {b: nonzero.get(b, 0) for b in _BUCKETS}


def _cached_billable_rows(aid: str) -> int:
    with system_sync_session() as s:
        return s.execute(text("SELECT billable_rows FROM contact_lookup_actions "
                              "WHERE id = :a"), {"a": aid}).scalar_one()


def _write_reason(aid: str, reason: str) -> None:
    """LABELLED: a `status_reason` no writer produces yet, as a future one might."""
    with system_sync_session() as s:
        s.execute(text("UPDATE contact_lookup_actions SET status_reason = :r WHERE id = :a"),
                  {"r": reason, "a": aid})
        s.commit()


@pytest.fixture
def pause_reads(monkeypatch):
    """PASS-THROUGH spy on the route's pause reader: records, then reads for real."""
    calls: list[str] = []
    real = jobs_routes.read_pause_state

    def spy(r, user_id, now):
        calls.append(user_id)
        return real(r, user_id, now)

    monkeypatch.setattr(jobs_routes, "read_pause_state", spy)
    return calls


# ── every status, reached for real ───────────────────────────────────────────


async def test_a_fresh_action_is_dispatching_with_every_lead_pending(
    db, client, business_user, business_token, _lookups_on, pause_reads,
):
    """AP1: no worker timestamp yet and only `quoted` verdicts: still a 200."""
    job, aid, _ = await _bought(client, business_token, business_user.id, n=3)
    pause_reads.clear()  # the quote read it too
    body = await _ok(client, business_token, job, aid)
    assert (body["action_id"], body["category"], body["status"], body["reason"]) == \
        (aid, "new", "dispatching", None)
    assert body["outcomes"] == _only(pending=3)
    assert (body["quoted_count"], body["billable"], body["truncated"]) == (3, 0, False)
    assert (body["started_at"], body["claimed_at"], body["settled_at"]) == (None, None, None)
    assert body["unit_price_cents"] > 0 and body["currency"] == "USD"
    assert body["created_at"] is not None and body["status_changed_at"] is not None
    assert body["pause"] is not None and pause_reads == [business_user.id]


async def test_a_running_action_has_started(
    db, client, business_user, business_token, _lookups_on, pause_reads,
):
    job, aid, _ = await _bought(client, business_token, business_user.id)
    pause_reads.clear()  # the quote read it too
    _start(aid)  # T1 alone, as a worker between its two transactions
    body = await _ok(client, business_token, job, aid)
    assert (body["status"], body["reason"]) == ("running", None)
    assert body["started_at"] is not None and body["claimed_at"] is None
    assert body["outcomes"] == _only(pending=1)
    assert body["pause"] is not None and len(pause_reads) == 1


async def test_a_claimed_action_shows_its_leads_being_looked_up(
    db, client, business_user, business_token, _lookups_on, pause_reads,
):
    job, aid, _ = await _bought(client, business_token, business_user.id, n=2)
    pause_reads.clear()  # the quote read it too
    assert cla.run_action(aid)["outcome"] == "claimed"
    body = await _ok(client, business_token, job, aid)
    assert (body["status"], body["reason"]) == ("claimed", None)
    assert body["outcomes"] == _only(in_progress=2)
    assert body["started_at"] is not None and body["claimed_at"] is not None
    assert body["settled_at"] is None
    assert body["pause"] is not None and len(pause_reads) == 1  # AP3: a lead still waits


async def test_a_settled_action_maps_every_ending_and_agrees_with_billing(
    db, client, business_user, business_token, _lookups_on, pause_reads,
):
    job, aid, rids = await _bought(client, business_token, business_user.id, n=6)
    pause_reads.clear()  # the quote read it too
    assert cla.run_action(aid)["outcome"] == "claimed"
    hit, miss, billed, unbilled, errored, cancelled = rids
    _row(aid, hit, "completed")
    _result(hit, "hit")
    _row(aid, miss, "completed")
    _result(miss, "miss")
    _row(aid, billed, "unmatched",
         queue=_queue(business_user.id, job, uploaded=1, sent=1, decision=True))
    _row(aid, unbilled, "unmatched",
         queue=_queue(business_user.id, job, uploaded=0, sent=1, decision=False))
    _row(aid, errored, "errored")
    _row(aid, cancelled, "cancelled")
    _tick()
    body = await _ok(client, business_token, job, aid)
    assert (body["status"], body["reason"]) == ("settled", None)
    assert body["outcomes"] == _only(found=1, not_found=2, not_found_no_charge=1,
                                     not_looked_up=2)
    assert body["billable"] == 3 == _cached_billable_rows(aid)  # the cache agrees, settled
    assert body["settled_at"] is not None
    assert body["pause"] is None and pause_reads == []  # final: no Redis read


async def test_a_flagged_action_is_under_review(
    db, client, business_user, business_token, _lookups_on,
):
    """A completed lookup whose lead is neither hit nor miss: the reconciler flags it."""
    job, aid, [rid] = await _bought(client, business_token, business_user.id)
    assert cla.run_action(aid)["outcome"] == "claimed"
    _row(aid, rid, "completed")
    _result(rid, "purged")  # NOT NULL column: a real non-answer
    _tick()
    body = await _ok(client, business_token, job, aid)
    assert (body["status"], body["reason"]) == ("claimed", "under_review")
    assert body["outcomes"] == _only(in_progress=1)


async def test_lookups_switched_off_wait_and_say_so(
    db, client, business_user, business_token, _lookups_on, monkeypatch,
):
    job, aid, _ = await _bought(client, business_token, business_user.id)
    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", False)
    assert cla.run_action(aid)["outcome"] == "waiting"
    body = await _ok(client, business_token, job, aid)  # no switch gate on a read
    assert (body["status"], body["reason"]) == ("dispatching", "lookups_switched_off")
    assert body["outcomes"] == _only(pending=1)


async def test_a_plan_lost_before_the_worker_fails_and_is_still_shown(
    db, client, business_user, business_token, _lookups_on, pause_reads,
):
    job, aid, _ = await _bought(client, business_token, business_user.id, n=2)
    pause_reads.clear()  # the quote read it too
    await _set_user(db, business_user.id, plan="starter")
    assert cla.run_action(aid)["outcome"] == "failed"
    body = await _ok(client, business_token, job, aid)  # no plan gate on a read
    assert (body["status"], body["reason"]) == ("failed", "plan_not_eligible")
    assert body["outcomes"] == _only(not_looked_up=2)
    assert body["billable"] == 0
    assert body["pause"] is None and pause_reads == []


async def test_a_frozen_account_still_sees_what_it_bought(
    db, client, business_user, business_token, _lookups_on,
):
    job, aid, _ = await _bought(client, business_token, business_user.id)
    await _set_user(db, business_user.id, subscription_status="unpaid")
    assert (await _ok(client, business_token, job, aid))["status"] == "dispatching"


async def test_an_action_never_started_expires(
    db, client, business_user, business_token, _lookups_on, pause_reads,
):
    job, aid, _ = await _bought(client, business_token, business_user.id, n=2)
    pause_reads.clear()  # the quote read it too
    _age(aid, cla.ACTION_DEADLINE_SECONDS + 1)
    _tick()
    body = await _ok(client, business_token, job, aid)
    assert (body["status"], body["reason"]) == ("expired", "not_started_in_time")
    assert body["outcomes"] == _only(not_looked_up=2)
    assert body["pause"] is None and pause_reads == []


async def test_an_action_with_nothing_left_to_buy_reads_no_pause(
    db, client, business_user, business_token, _lookups_on, pause_reads,
):
    """AP3: `claimed` with no lead in progress waits on nothing, so no Redis read."""
    job, aid, [rid] = await _bought(client, business_token, business_user.id)
    pause_reads.clear()  # the quote read it too
    _result(rid, "hit")  # answered elsewhere between the confirm and the claim
    assert cla.run_action(aid)["outcome"] == "claimed"
    body = await _ok(client, business_token, job, aid)
    assert body["status"] == "claimed"
    assert body["outcomes"] == _only(already_answered=1)
    assert body["pause"] is None and pause_reads == []


# ── the pause state ──────────────────────────────────────────────────────────


async def test_a_waiting_action_reports_the_published_pause(
    db, client, business_user, business_token, redis_client, _lookups_on,
):
    job, aid, _ = await _bought(client, business_token, business_user.id)
    at = datetime.now(UTC) + timedelta(hours=3)
    _publish(redis_client, accounts={business_user.id: ScopeResume(at, at)})
    p = (await _ok(client, business_token, job, aid))["pause"]
    assert p["status"] == "paused"
    assert datetime.fromisoformat(p["normal_resume_at"]) == at


async def test_redis_on_a_closed_port_reads_unknown_never_503(
    db, client, business_user, business_token, _lookups_on, _other_clients_built, monkeypatch,
):
    job, aid, _ = await _bought(client, business_token, business_user.id)
    jobs_routes._lookup_redis_client = None
    monkeypatch.setattr(settings, "REDIS_URL", f"redis://127.0.0.1:{_free_port()}/0")
    assert (await _ok(client, business_token, job, aid))["pause"]["status"] == "unknown"


async def test_a_blackholed_redis_reads_unknown_within_the_bound(
    db, client, business_user, business_token, _lookups_on, _other_clients_built, monkeypatch,
):
    job, aid, _ = await _bought(client, business_token, business_user.id)
    jobs_routes._lookup_redis_client = None
    with _blackhole() as port:
        monkeypatch.setattr(settings, "REDIS_URL", f"redis://127.0.0.1:{port}/0")
        started = time.monotonic()
        body = await _ok(client, business_token, job, aid)
        elapsed = time.monotonic() - started
    assert body["pause"]["status"] == "unknown"
    assert elapsed < 3.0, elapsed



async def test_a_redis_client_that_cannot_be_built_reads_unknown(
    db, client, business_user, business_token, _lookups_on, _other_clients_built, monkeypatch,
):
    """The route's OWN guard, not the reader's: `read_pause_state` absorbs a failing
    call, but building the client happens before it and can raise too."""
    job, aid, _ = await _bought(client, business_token, business_user.id)
    jobs_routes._lookup_redis_client = None
    monkeypatch.setattr(settings, "REDIS_URL", "notredis://127.0.0.1:1/0")
    with pytest.raises(ValueError):  # the fault is real: from_url refuses the scheme
        jobs_routes._lookup_redis()
    assert (await _ok(client, business_token, job, aid))["pause"]["status"] == "unknown"

# ── ownership ────────────────────────────────────────────────────────────────


async def test_one_404_for_anything_not_this_accounts_action_on_this_run(
    db, client, business_user, business_token, starter_token, _lookups_on,
):
    job, aid, _ = await _bought(client, business_token, business_user.id)
    other_job = _job(business_user.id)
    not_found = {"detail": "Contact lookup not found"}
    for token, j, a in (
        (starter_token, job, aid),                # another account's action
        (business_token, other_job, aid),         # this account's, another run
        (business_token, job, str(uuid.uuid4())),  # none at all
        (business_token, job, "not-a-uuid"),      # malformed: never a DataError 500
    ):
        r = await _status(client, token, j, a)
        assert (r.status_code, r.json()) == (404, not_found), (j, a, r.text)
    r = await _status(client, business_token, "not-a-uuid", aid)
    assert r.status_code == 404


async def test_an_upper_case_id_is_the_same_action(
    db, client, business_user, business_token, _lookups_on,
):
    job, aid, _ = await _bought(client, business_token, business_user.id)
    body = await _ok(client, business_token, job.upper(), aid.upper())
    assert body["action_id"] == aid


# ── what the body never carries ──────────────────────────────────────────────


async def test_the_bodies_carry_no_lead_id_quote_id_or_cost(
    db, client, business_user, business_token, _lookups_on,
):
    job = _job(business_user.id)
    rids = _seed(business_user.id, job, [{}, {}])
    quote_id = await _quoted(client, business_token, job)
    aid = (await _confirm(client, business_token, job, quote_id)).json()["action_id"]
    assert cla.run_action(aid)["outcome"] == "claimed"
    for r in (await _status(client, business_token, job, aid),
              await _list(client, business_token, job)):
        assert r.status_code == 200, r.text
        for secret in (*rids, quote_id, "credit", "tracerfy", "lease", "pricing_version",
                       "snapshot", "dispatched_at", "newly_queued"):
            assert secret not in r.text, secret


# ── the reason vocabulary (Design 6, AR1) ────────────────────────────────────


_KNOWN_REASONS = {
    "kill_switch": "lookups_switched_off",
    "claim_lock_busy": "retrying", "claim_unenforced": "retrying",
    "worker_error": "retrying", "lease_expired": "retrying",
    "plan_not_eligible": "plan_not_eligible", "access_starter": "plan_not_eligible",
    "access_frozen": "account_inactive", "access_ended": "account_inactive",
    "job_not_delivered": "run_unavailable",
    "quoted_set_mismatch": "leads_changed",
    "deadline": "not_started_in_time",
    **dict.fromkeys(rec.FLAGS, "under_review"),
}


def test_every_known_reason_maps_and_anything_else_is_other():
    assert {r: jobs_routes._customer_reason(r) for r in _KNOWN_REASONS} == _KNOWN_REASONS
    assert jobs_routes._customer_reason(None) is None
    assert jobs_routes._customer_reason("a_flag_added_later") == "other"


async def test_an_unknown_claimed_reason_is_other_on_both_routes(
    db, client, business_user, business_token, _lookups_on,
):
    job, aid, _ = await _bought(client, business_token, business_user.id)
    assert cla.run_action(aid)["outcome"] == "claimed"
    _write_reason(aid, "a_flag_added_later")
    assert (await _ok(client, business_token, job, aid))["reason"] == "other"
    [summary] = (await _list(client, business_token, job)).json()["actions"]
    assert summary["reason"] == "other"


# ── the outcome vocabulary (AP4) ─────────────────────────────────────────────


def test_every_verdict_is_in_exactly_one_bucket_and_billable_matches_billing():
    bucket = jobs_routes._OUTCOME_BUCKET
    assert sorted(bucket) == sorted(CONTACT_LOOKUP_DISPOSITIONS)
    assert set(bucket.values()) == set(_BUCKETS)
    billable = {d for d, b in bucket.items() if b in jobs_routes._BILLABLE_BUCKETS}
    assert billable == set(rec._BILLABLE_VERDICTS)


def test_an_unmapped_verdict_is_never_guessed_into_a_bucket():
    with pytest.raises(jobs_routes.UnmappedDispositionError):
        jobs_routes._outcomes({"quoted": 1, "a_verdict_added_later": 1})


# ── the limiter ──────────────────────────────────────────────────────────────


async def test_sixty_reads_a_minute_then_429(
    db, client, business_user, business_token, _lookups_on,
):
    job, aid, _ = await _bought(client, business_token, business_user.id)
    for i in range(60):
        r = await _status(client, business_token, job, aid)
        assert r.status_code == 200, f"read {i + 1}: {r.status_code}"
    assert (await _status(client, business_token, job, aid)).status_code == 429
    assert (await _list(client, business_token, job)).status_code == 429  # one bucket


async def test_a_stalled_rate_limiter_proceeds_within_the_bound(
    db, client, business_user, business_token, _lookups_on, _other_clients_built,
):
    """Q1: a read buys nothing, so a stalled limiter call proceeds instead of a 503."""
    job, aid, _ = await _bought(client, business_token, business_user.id)
    good = rate_limit_module._redis_client
    with _blackhole() as port:
        rate_limit_module._redis_client = aioredis.from_url(f"redis://127.0.0.1:{port}/0")
        try:
            started = time.monotonic()
            r = await _status(client, business_token, job, aid)
            elapsed = time.monotonic() - started
        finally:
            stalled, rate_limit_module._redis_client = rate_limit_module._redis_client, good
    await stalled.aclose()
    assert r.status_code == 200, r.text
    assert elapsed < 3.0, elapsed


# ── the list (AP2) ───────────────────────────────────────────────────────────


def _flip_category(aid: str) -> None:
    """LABELLED: an `already_delivered` action, for the filter."""
    with system_sync_session() as s:
        s.execute(text("UPDATE contact_lookup_actions SET category = 'already_delivered' "
                       "WHERE id = :a"), {"a": aid})
        s.commit()


async def test_the_list_is_the_newest_twenty_newest_first(
    db, client, business_user, business_token,
):
    job = _job(business_user.id)
    rids = _seed(business_user.id, job, [{} for _ in range(21)])
    aids = []
    for i, rid in enumerate(rids):  # as the confirm writes them, oldest first
        aid = _action(business_user.id, job, [rid])
        _age(aid, 1000 - i)
        aids.append(aid)
    r = await _list(client, business_token, job)
    assert r.status_code == 200, r.text
    listed = r.json()["actions"]
    assert [a["action_id"] for a in listed] == aids[::-1][:20]
    assert set(listed[0]) == {"action_id", "category", "status", "reason", "quoted_count",
                              "truncated", "created_at", "settled_at"}
    assert (listed[0]["status"], listed[0]["quoted_count"], listed[0]["settled_at"]) == \
        ("dispatching", 1, None)


async def test_the_list_filters_by_tab(db, client, business_user, business_token):
    job = _job(business_user.id)
    new_rid, old_rid = _seed(business_user.id, job, [{}, {}])
    new_aid = _action(business_user.id, job, [new_rid])
    old_aid = _action(business_user.id, job, [old_rid])
    _flip_category(old_aid)
    by_tab = {}
    for tab in ("new", "already_delivered"):
        r = await _list(client, business_token, job, category=tab)
        by_tab[tab] = [a["action_id"] for a in r.json()["actions"]]
    assert by_tab == {"new": [new_aid], "already_delivered": [old_aid]}
    assert (await _list(client, business_token, job, category="bogus")).status_code == 422


async def test_the_list_404s_a_run_that_is_not_this_accounts(
    db, client, business_user, business_token, starter_token,
):
    job = _job(business_user.id)
    [rid] = _seed(business_user.id, job, [{}])
    _action(business_user.id, job, [rid])
    for token, j in ((starter_token, job), (business_token, str(uuid.uuid4())),
                     (business_token, "not-a-uuid")):
        r = await _list(client, token, j)
        assert (r.status_code, r.json()) == (404, {"detail": "Job not found"}), (j, r.text)
    empty = _job(business_user.id)
    assert (await _list(client, business_token, empty)).json() == {"actions": []}


# ── as the real API role ─────────────────────────────────────────────────────


async def test_the_status_query_runs_as_the_api_role_under_rls(
    db, client, business_user, business_token, _lookups_on,
):
    """The route runs as `bridgeleads_app` under RLS with the tenant set: the query
    needs only its SELECT grants, and RLS alone hides another tenant's action."""
    job, aid, _ = await _bought(client, business_token, business_user.id, n=2)
    await db.execute(text(
        "DO $r$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = "
        "'bridgeleads_app') THEN CREATE ROLE bridgeleads_app NOLOGIN NOBYPASSRLS; "
        "END IF; END $r$"
    ))
    await db.execute(text("GRANT USAGE ON SCHEMA public TO bridgeleads_app"))
    await db.execute(text("GRANT SELECT ON public.contact_lookup_actions, "
                          "public.contact_lookup_action_results TO bridgeleads_app"))
    await db.execute(text("SET LOCAL ROLE bridgeleads_app"))
    try:
        params = {"a": aid, "u": business_user.id, "j": job}
        for tenant, found in ((business_user.id, True), (str(uuid.uuid4()), False)):
            await db.execute(text("SELECT set_config('app.current_user_id', :u, true)"),
                             {"u": tenant})
            # The tenant GUC alone decides: the WHERE still names the owner.
            row = (await db.execute(jobs_routes._ACTION_STATUS_SQL, params)).first()
            assert (row is not None) == found, tenant
            if found:
                assert row.outcomes == {"quoted": 2}
        current = (await db.execute(text("SELECT current_user"))).scalar_one()
        assert current == "bridgeleads_app"
    finally:
        await db.rollback()  # the role and the GUC are LOCAL: teardown runs as before
