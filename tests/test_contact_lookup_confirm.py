"""POST /jobs/{job_id}/contact-lookups: the confirm (Phase 1b-2d).

Contract: tasks/todo-lookup-contacts.md, "## Phase 1b-2d — the confirm endpoint" as
amended by AO1-AO5.

Real PG and real Redis, through the API client. Quotes are made by the REAL quote
endpoint, and the worker's claim runs for real (`run_action`). The publish is observed
with a PASS-THROUGH spy on `apply_async`: it records the call, then publishes to the test
broker as production would. Two cases are labelled FAULT INJECTION, the one place it
cannot be avoided: a broker that fails the publish, and the worker winning the race
between the publish and the `dispatched_at` stamp (AO2).
"""
from __future__ import annotations

import asyncio
import importlib
import json
import time
import uuid

import pytest
import redis.asyncio as aioredis
from sqlalchemy import text

from src.api.routes import jobs as jobs_routes
from src.config import settings
from src.db.session import system_sync_session
from src.workers import contact_lookup_action as cla
from src.workers.scheduler_helpers import contact_lookups as rec
from tests.test_contact_lookup_quote import _auth, _blackhole, _job, _quote, _seed, _stored

rate_limit_module = importlib.import_module("src.api.middleware.rate_limit")


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
def published(monkeypatch):
    """PASS-THROUGH spy: every publish is recorded, then really sent to the broker."""
    calls: list[str] = []
    real = cla.lookup_contacts.apply_async

    def spy(*args, **kwargs):
        calls.append(kwargs.get("args", args[0] if args else [None])[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(cla.lookup_contacts, "apply_async", spy)
    return calls


async def _confirm(client, token: str, job_id: str, quote_id: str, category: str = "new"):
    return await client.post(f"/jobs/{job_id}/contact-lookups",
                             json={"quote_id": quote_id, "category": category},
                             headers=_auth(token))


async def _quoted(client, token: str, job_id: str, category: str = "new") -> str:
    r = await _quote(client, token, job_id, category)
    assert r.status_code == 200, r.text
    return r.json()["quote_id"]


def _action(aid: str):
    with system_sync_session() as s:
        return s.execute(text(
            "SELECT status, job_id::text AS job_id, category, quote_id, quoted_count, "
            "       truncated, unit_price_cents, currency, pricing_version, quote_snapshot, "
            "       dispatched_at "
            "FROM contact_lookup_actions WHERE id = :a"), {"a": aid}).one()


def _actions_of(user_id: str) -> int:
    with system_sync_session() as s:
        return s.execute(text("SELECT count(*) FROM contact_lookup_actions "
                              "WHERE user_id = :u"), {"u": user_id}).scalar_one()


def _quoted_rows(aid: str) -> set[str]:
    with system_sync_session() as s:
        return {str(r) for r in s.execute(text(
            "SELECT result_id FROM contact_lookup_action_results "
            "WHERE action_id = :a AND disposition = 'quoted'"), {"a": aid}).scalars()}


def _events(aid: str) -> list[tuple]:
    with system_sync_session() as s:
        return [tuple(r) for r in s.execute(text(
            "SELECT from_status, to_status FROM contact_lookup_action_events "
            "WHERE action_id = :a ORDER BY at, id"), {"a": aid})]


def _set_quote(redis_client, user_id: str, job_id: str, **changes) -> None:
    key = jobs_routes._quote_key(user_id, job_id, "new")
    stored = json.loads(redis_client.get(key))
    stored.update(changes)
    redis_client.set(key, json.dumps(stored), ex=600)


# ── the purchase ─────────────────────────────────────────────────────────────


async def test_a_confirm_commits_the_quoted_set_publishes_and_the_worker_claims_it(
    db, client, business_user, business_token, redis_client, _lookups_on, published,
):
    job = _job(business_user.id)
    leads = _seed(business_user.id, job, [{}, {}, {"party_name": None}])
    qid = await _quoted(client, business_token, job)
    quote = _stored(redis_client, business_user.id, job)

    r = await _confirm(client, business_token, job, qid)

    assert r.status_code == 202, r.text
    body = r.json()
    aid = body["action_id"]
    assert body == {"action_id": aid, "status": "dispatching", "quoted_count": 3,
                    "truncated": False}
    for rid in leads:  # no lead id ever leaves in the response
        assert rid not in r.text
    a = _action(aid)
    assert (a.status, a.job_id, a.category, a.quote_id, a.quoted_count) == \
        ("dispatching", job, "new", qid, 3)
    assert (a.unit_price_cents, a.currency, a.pricing_version) == (8, "USD", "2026-06")
    assert a.quote_snapshot["policy"] == quote["policy"]
    assert a.quote_snapshot["counts"] == quote["counts"]
    assert a.quote_snapshot["access"] == "full"
    assert a.quote_snapshot["planner_version"] == quote["planner_version"]
    assert a.dispatched_at is not None
    assert _quoted_rows(aid) == set(leads)
    assert _events(aid) == [(None, "dispatching")]
    assert published == [aid]
    # The confirmed quote is gone: it cannot be confirmed into a second action.
    assert _stored(redis_client, business_user.id, job) is None

    out = cla.run_action(aid)  # the worker, for real

    assert out["outcome"] == "claimed"
    assert _action(aid).status == "claimed"
    with system_sync_session() as s:
        bought = {str(x) for x in s.execute(text(
            "SELECT result_id FROM pending_skip_trace_rows WHERE action_id = :a"),
            {"a": aid}).scalars()}
    assert bought == set(leads)


async def test_a_replay_returns_the_same_action_and_buys_nothing_more(
    db, client, business_user, business_token, _lookups_on, published,
):
    job = _job(business_user.id)
    _seed(business_user.id, job, [{}, {}])
    qid = await _quoted(client, business_token, job)
    first = (await _confirm(client, business_token, job, qid)).json()

    again = await _confirm(client, business_token, job, qid)  # the quote key is gone

    assert again.status_code == 202, again.text
    assert again.json() == first
    assert _actions_of(business_user.id) == 1
    assert published == [first["action_id"]]


async def test_a_replay_is_never_refused_by_what_changed_after_the_purchase(
    db, client, business_user, business_token, _lookups_on, published, monkeypatch,
):
    """AO1: a paid customer's own action is answered before the mutable gates."""
    job = _job(business_user.id)
    _seed(business_user.id, job, [{}])
    qid = await _quoted(client, business_token, job)
    first = (await _confirm(client, business_token, job, qid)).json()
    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", False)
    await db.execute(text("UPDATE users SET plan = 'starter' WHERE id = :u"),
                     {"u": business_user.id})
    await db.commit()

    again = await _confirm(client, business_token, job, qid)

    assert again.status_code == 202, again.text
    assert again.json() == first


async def test_a_replay_for_another_tab_is_a_conflict(
    db, client, business_user, business_token, _lookups_on, published,
):
    job = _job(business_user.id)
    _seed(business_user.id, job, [{}])
    qid = await _quoted(client, business_token, job)
    assert (await _confirm(client, business_token, job, qid)).status_code == 202

    r = await _confirm(client, business_token, job, qid, "already_delivered")

    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "quote_mismatch"


async def test_two_concurrent_confirms_make_one_action(
    db, client, business_user, business_token, _lookups_on, published,
):
    job = _job(business_user.id)
    _seed(business_user.id, job, [{}, {}])
    qid = await _quoted(client, business_token, job)

    a, b = await asyncio.gather(_confirm(client, business_token, job, qid),
                                _confirm(client, business_token, job, qid))

    assert (a.status_code, b.status_code) == (202, 202), (a.text, b.text)
    assert a.json()["action_id"] == b.json()["action_id"]
    assert _actions_of(business_user.id) == 1


# ── the quote must still be the one offered ──────────────────────────────────


async def test_a_superseded_quote_is_gone(
    db, client, business_user, business_token, _lookups_on, published,
):
    job = _job(business_user.id)
    _seed(business_user.id, job, [{}])
    old = await _quoted(client, business_token, job)
    await _quoted(client, business_token, job)  # a newer quote replaces it

    r = await _confirm(client, business_token, job, old)

    assert r.status_code == 410
    assert r.json()["detail"]["code"] == "quote_expired"
    assert _actions_of(business_user.id) == 0


async def test_a_missing_or_expired_quote_is_gone(
    db, client, business_user, business_token, redis_client, _lookups_on, published,
):
    job = _job(business_user.id)
    _seed(business_user.id, job, [{}])
    qid = await _quoted(client, business_token, job)
    _set_quote(redis_client, business_user.id, job, expires_at="2020-01-01T00:00:00+00:00")
    assert (await _confirm(client, business_token, job, qid)).status_code == 410
    redis_client.delete(jobs_routes._quote_key(business_user.id, job, "new"))
    assert (await _confirm(client, business_token, job, qid)).status_code == 410
    assert _actions_of(business_user.id) == 0


async def test_an_unsupported_quote_version_is_a_conflict(
    db, client, business_user, business_token, redis_client, _lookups_on, published,
):
    job = _job(business_user.id)
    _seed(business_user.id, job, [{}])
    qid = await _quoted(client, business_token, job)
    _set_quote(redis_client, business_user.id, job, v=1)

    r = await _confirm(client, business_token, job, qid)

    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "quote_unsupported"


async def test_a_quote_with_nothing_to_look_up_creates_nothing(
    db, client, business_user, business_token, _lookups_on, published,
):
    job = _job(business_user.id)
    _seed(business_user.id, job, [{"skip_trace_status": "hit"}])
    qid = await _quoted(client, business_token, job)

    r = await _confirm(client, business_token, job, qid)

    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "nothing_to_look_up"
    assert _actions_of(business_user.id) == 0


@pytest.mark.parametrize("how", ["deleted", "another_tenant", "another_run"])
async def test_a_quoted_lead_that_is_no_longer_this_tabs_buys_nothing(
    db, client, business_user, business_token, starter_user, redis_client, _lookups_on,
    published, how,
):
    """V1: every quoted id must still be one of THIS account's leads in THIS run, or
    the whole confirm is refused. A forged id in the stored quote is the worst case."""
    job = _job(business_user.id)
    leads = _seed(business_user.id, job, [{}, {}])
    qid = await _quoted(client, business_token, job)
    if how == "deleted":
        with system_sync_session() as s:
            s.execute(text("DELETE FROM results WHERE id = :r"), {"r": leads[1]})
            s.commit()
    else:
        owner = starter_user.id if how == "another_tenant" else business_user.id
        [foreign] = _seed(owner, _job(owner), [{}])
        _set_quote(redis_client, business_user.id, job, quoted_ids=[*leads, foreign])

    r = await _confirm(client, business_token, job, qid)

    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "quote_stale"
    assert _actions_of(business_user.id) == 0
    with system_sync_session() as s:
        assert s.execute(text("SELECT count(*) FROM contact_lookup_action_results "
                              "WHERE user_id = :u"), {"u": business_user.id}).scalar_one() == 0


# ── the gates ────────────────────────────────────────────────────────────────


async def test_another_accounts_job_is_404(
    db, client, business_token, starter_user, _lookups_on, published,
):
    job = _job(starter_user.id)
    r = await _confirm(client, business_token, job, "q" * 43)
    assert r.status_code == 404


async def test_a_run_that_has_not_finished_is_409(
    db, client, business_user, business_token, _lookups_on, published,
):
    job = _job(business_user.id, status="scraping")
    r = await _confirm(client, business_token, job, "q" * 43)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "run_not_finished"


async def test_a_plan_without_lookups_is_402_and_switched_off_is_503(
    db, client, business_user, business_token, _lookups_on, published, monkeypatch,
):
    job = _job(business_user.id)
    _seed(business_user.id, job, [{}])
    qid = await _quoted(client, business_token, job)
    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", False)
    r = await _confirm(client, business_token, job, qid)
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "contact_lookups_unavailable"
    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
    await db.execute(text("UPDATE users SET plan = 'starter' WHERE id = :u"),
                     {"u": business_user.id})
    await db.commit()
    assert (await _confirm(client, business_token, job, qid)).status_code == 402
    assert _actions_of(business_user.id) == 0


async def test_a_malformed_quote_id_is_422(
    db, client, business_user, business_token, _lookups_on, published,
):
    job = _job(business_user.id)
    for bad in ("", "x" * 65, "has spaces", "semi;colon"):
        r = await _confirm(client, business_token, job, bad)
        assert r.status_code == 422, bad


async def test_thirty_writes_a_minute_then_429(
    db, client, business_user, business_token, _lookups_on, published,
):
    """The `writes` zone (V7/W4): 30 per minute per account."""
    job = _job(business_user.id)
    for i in range(30):
        r = await _confirm(client, business_token, job, f"nosuchquote{i}")
        assert r.status_code == 410, f"confirm {i + 1}: {r.status_code}"
    assert (await _confirm(client, business_token, job, "nosuchquote30")).status_code == 429


async def test_a_stalled_rate_limiter_is_a_bounded_503(
    db, client, business_user, business_token, _lookups_on, published,
):
    """AO4: only an OUTER timeout of the limiter call is a 503."""
    job = _job(business_user.id)
    rate_limit_module._get_redis()
    good = rate_limit_module._redis_client
    with _blackhole() as port:
        rate_limit_module._redis_client = aioredis.from_url(f"redis://127.0.0.1:{port}/0")
        try:
            started = time.monotonic()
            r = await _confirm(client, business_token, job, "q" * 43)
            elapsed = time.monotonic() - started
        finally:
            stalled, rate_limit_module._redis_client = rate_limit_module._redis_client, good
    await stalled.aclose()
    assert r.status_code == 503
    assert elapsed < 3.0, elapsed


async def test_an_unreachable_quote_store_is_a_bounded_503(
    db, client, business_user, business_token, _lookups_on, published, monkeypatch,
):
    job = _job(business_user.id)
    _seed(business_user.id, job, [{}])
    qid = await _quoted(client, business_token, job)
    rate_limit_module._get_redis()  # the limiter keeps the good url
    with _blackhole() as port:
        monkeypatch.setattr(settings, "REDIS_URL", f"redis://127.0.0.1:{port}/0")
        jobs_routes._lookup_redis_client = None
        started = time.monotonic()
        r = await _confirm(client, business_token, job, qid)
        elapsed = time.monotonic() - started
    assert r.status_code == 503
    assert elapsed < 3.0, elapsed
    assert _actions_of(business_user.id) == 0


# ── a publish that does not land ─────────────────────────────────────────────


async def test_a_failed_publish_still_accepts_and_the_reconciler_publishes_it(
    db, client, business_user, business_token, _lookups_on, monkeypatch,
):
    """FAULT INJECTION (labelled): the broker refuses the publish. The purchase is
    committed, the customer gets 202, and the reconciler's P3 publishes it."""
    job = _job(business_user.id)
    _seed(business_user.id, job, [{}])
    qid = await _quoted(client, business_token, job)
    real = cla.lookup_contacts.apply_async
    attempts: list[str] = []

    def broken(*args, **kwargs):
        attempts.append("refused")
        raise ConnectionError("injected: broker refused the publish")

    monkeypatch.setattr(cla.lookup_contacts, "apply_async", broken)
    r = await _confirm(client, business_token, job, qid)

    assert r.status_code == 202, r.text
    aid = r.json()["action_id"]
    assert attempts == ["refused"]
    assert (_action(aid).status, _action(aid).dispatched_at) == ("dispatching", None)

    republished: list[str] = []

    def spy(*args, **kwargs):
        republished.append(kwargs.get("args", [None])[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(cla.lookup_contacts, "apply_async", spy)
    with system_sync_session() as s:  # past P3's first-publish delay
        s.execute(text("UPDATE contact_lookup_actions SET created_at = now() - "
                       "interval '2 minutes' WHERE id = :a"), {"a": aid})
        s.commit()
    rec._reconcile_contact_lookups_impl()

    assert republished == [aid]
    assert _action(aid).dispatched_at is not None


async def test_the_worker_winning_the_race_to_the_stamp_is_not_an_error(
    db, client, business_user, business_token, _lookups_on, monkeypatch, caplog,
):
    """AO2, FAULT INJECTION (labelled): the worker claims the action between the
    publish and the `dispatched_at` stamp. The stamp matches no row, and the confirm
    is still a 202."""
    job = _job(business_user.id)
    _seed(business_user.id, job, [{}])
    qid = await _quoted(client, business_token, job)
    ran: list[str] = []

    def worker_first(*args, **kwargs):
        aid = kwargs.get("args", [None])[0]
        ran.append(cla.run_action(aid)["outcome"])

    monkeypatch.setattr(cla.lookup_contacts, "apply_async", worker_first)
    r = await _confirm(client, business_token, job, qid)

    assert r.status_code == 202, r.text
    aid = r.json()["action_id"]
    assert ran == ["claimed"]
    a = _action(aid)
    assert (a.status, a.dispatched_at) == ("claimed", None)
    # A normal race, not a failure: the stamp matched no row, nothing raised.
    assert "dispatched_at not stamped" not in caplog.text


async def test_a_stamp_the_database_refuses_still_accepts_the_purchase(
    db, client, business_user, business_token, _lookups_on, published,
):
    """FAULT INJECTION (labelled): the `dispatched_at` stamp fails in the database
    after the purchase committed. The confirm must still be a 202. Found by a mutant:
    the stamp's rollback expired the ORM user, and the audit line read it, a 500."""
    job = _job(business_user.id)
    _seed(business_user.id, job, [{}])
    qid = await _quoted(client, business_token, job)
    fn, trig = f"t_refuse_stamp_{uuid.uuid4().hex[:8]}", f"trg_{uuid.uuid4().hex[:8]}"
    with system_sync_session() as s:
        s.execute(text(
            f"CREATE FUNCTION {fn}() RETURNS trigger AS $f$ BEGIN "
            f"IF NEW.quote_id = '{qid}' AND NEW.dispatched_at IS NOT NULL THEN "
            "RAISE EXCEPTION 'injected: the stamp is refused'; END IF; RETURN NEW; END "
            "$f$ LANGUAGE plpgsql"))
        s.execute(text(f"CREATE TRIGGER {trig} BEFORE UPDATE ON contact_lookup_actions "
                       f"FOR EACH ROW EXECUTE FUNCTION {fn}()"))
        s.commit()
    try:
        r = await _confirm(client, business_token, job, qid)
    finally:
        with system_sync_session() as s:
            s.execute(text(f"DROP TRIGGER {trig} ON contact_lookup_actions"))
            s.execute(text(f"DROP FUNCTION {fn}()"))
            s.commit()

    assert r.status_code == 202, r.text
    aid = r.json()["action_id"]
    assert published == [aid]
    a = _action(aid)
    assert (a.status, a.dispatched_at) == ("dispatching", None)


async def test_a_busy_publisher_is_skipped_and_never_waited_on(
    db, client, business_user, business_token, _lookups_on, published, caplog,
):
    """AO3: with both publish slots held (a broker stall in progress), the confirm
    does not wait: it accepts, and the reconciler publishes later."""
    job = _job(business_user.id)
    _seed(business_user.id, job, [{}])
    qid = await _quoted(client, business_token, job)
    assert jobs_routes._publish_slots.acquire(blocking=False)
    assert jobs_routes._publish_slots.acquire(blocking=False)
    try:
        started = time.monotonic()
        r = await _confirm(client, business_token, job, qid)
        elapsed = time.monotonic() - started
    finally:
        jobs_routes._publish_slots.release()
        jobs_routes._publish_slots.release()

    assert r.status_code == 202, r.text
    assert published == []
    assert "publish skipped (broker busy)" in caplog.text  # skipped, not bypassed
    assert _action(r.json()["action_id"]).dispatched_at is None
    assert elapsed < 3.0, elapsed


@pytest.mark.parametrize("corrupt", [
    {"expires_at": "2099-01-01T00:00:00"},          # naive: no timezone
    {"quoted_ids": "not-a-list"},
    {"quoted_ids": ["not-a-uuid"]},
    {"quoted_ids": {"not": "a list"}},
    {"unit_price_cents": None},
    {"unit_price_cents": 0},
    {"unit_price_cents": True},
    {"unit_price_cents": "500"},
    {"currency": "DOLLARS"},
    {"pricing_version": ""},
    {"pricing_version": "v" * 33},
    {"unit_price_cents": 2_147_483_648},             # overflows the INTEGER column
    {"stopped": "credit_cap", "remaining": "3"},
    {"remaining": -1},
    {"remaining": None},
    {"stopped": "not_a_planner_stop"},
])
async def test_a_corrupt_stored_quote_is_refused_never_a_500(
    db, client, business_user, business_token, redis_client, _lookups_on, published, corrupt,
):
    job = _job(business_user.id)
    _seed(business_user.id, job, [{}])
    qid = await _quoted(client, business_token, job)
    _set_quote(redis_client, business_user.id, job, **corrupt)

    r = await _confirm(client, business_token, job, qid)

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "quote_unsupported"
    assert _actions_of(business_user.id) == 0


async def test_quoted_ids_held_in_a_mapping_are_refused_not_bought(
    db, client, business_user, business_token, redis_client, _lookups_on, published,
):
    """A JSON object of real lead ids iterates like a list. It is still not a quote."""
    job = _job(business_user.id)
    _seed(business_user.id, job, [{}])
    qid = await _quoted(client, business_token, job)
    ids = _stored(redis_client, business_user.id, job)["quoted_ids"]
    _set_quote(redis_client, business_user.id, job, quoted_ids=dict.fromkeys(ids, 1))

    r = await _confirm(client, business_token, job, qid)

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "quote_unsupported"
    assert _actions_of(business_user.id) == 0


@pytest.mark.parametrize("raw", ["{not json", "[]", "null", '"a string"', "1"])
async def test_an_unreadable_stored_quote_is_refused_never_a_500(
    db, client, business_user, business_token, redis_client, _lookups_on, published, raw,
):
    job = _job(business_user.id)
    _seed(business_user.id, job, [{}])
    qid = await _quoted(client, business_token, job)
    redis_client.set(jobs_routes._quote_key(business_user.id, job, "new"), raw, ex=600)

    r = await _confirm(client, business_token, job, qid)

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "quote_unsupported"
    assert _actions_of(business_user.id) == 0


async def test_a_lead_quoted_twice_is_bought_once(
    db, client, business_user, business_token, redis_client, _lookups_on, published,
):
    job = _job(business_user.id)
    _seed(business_user.id, job, [{}])
    qid = await _quoted(client, business_token, job)
    ids = _stored(redis_client, business_user.id, job)["quoted_ids"]
    _set_quote(redis_client, business_user.id, job, quoted_ids=ids + ids)

    r = await _confirm(client, business_token, job, qid)

    assert r.status_code == 202, r.text
    assert r.json()["quoted_count"] == 1
    assert _quoted_rows(r.json()["action_id"]) == set(ids)


@pytest.mark.parametrize(("stopped", "remaining", "truncated"), [
    ("credit_cap", 3, True),
    ("credit_cap", 0, False),
    (None, 3, False),
])
async def test_truncated_means_the_quote_stopped_with_leads_left(
    db, client, business_user, business_token, redis_client, _lookups_on, published,
    stopped, remaining, truncated,
):
    job = _job(business_user.id)
    _seed(business_user.id, job, [{}])
    qid = await _quoted(client, business_token, job)
    _set_quote(redis_client, business_user.id, job, stopped=stopped, remaining=remaining)

    r = await _confirm(client, business_token, job, qid)

    assert r.status_code == 202, r.text
    assert r.json()["truncated"] is truncated
    assert _action(r.json()["action_id"]).truncated is truncated


async def test_the_publish_never_retries_inside_the_request(
    db, client, business_user, business_token, _lookups_on, monkeypatch,
):
    """AO3: kombu's publish-retry loop is off. PASS-THROUGH spy on the kwargs."""
    job = _job(business_user.id)
    _seed(business_user.id, job, [{}])
    qid = await _quoted(client, business_token, job)
    real = cla.lookup_contacts.apply_async
    seen: list[dict] = []

    def spy(*args, **kwargs):
        seen.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(cla.lookup_contacts, "apply_async", spy)
    r = await _confirm(client, business_token, job, qid)

    assert r.status_code == 202, r.text
    assert [k.get("retry") for k in seen] == [False]


async def test_a_worker_module_that_fails_to_import_still_accepts_the_purchase(
    db, client, business_user, business_token, _lookups_on, monkeypatch, caplog,
):
    """FAULT INJECTION (labelled): the worker module cannot be imported after the
    purchase committed (2d review r1, P1). 202, and `dispatched_at` stays NULL for P3."""
    import sys

    job = _job(business_user.id)
    _seed(business_user.id, job, [{}])
    qid = await _quoted(client, business_token, job)
    monkeypatch.setitem(sys.modules, "src.workers.contact_lookup_action", None)

    r = await _confirm(client, business_token, job, qid)

    assert r.status_code == 202, r.text
    a = _action(r.json()["action_id"])
    assert (a.status, a.dispatched_at) == ("dispatching", None)
    assert "publish not attempted" in caplog.text  # the import really failed


def _all_slots_free() -> bool:
    got = [jobs_routes._publish_slots.acquire(blocking=False) for _ in range(2)]
    for ok in got:
        if ok:
            jobs_routes._publish_slots.release()
    return all(got)


async def test_the_publish_helper_never_raises_and_frees_its_slot(monkeypatch):
    """FAULT INJECTION (labelled): the broker refuses. The helper itself answers False
    (its contract, not only the route's guard) and its slot comes back."""
    attempts: list[str] = []

    def refused(*args, **kwargs):
        attempts.append("refused")
        raise ConnectionError("injected: broker refused the publish")

    monkeypatch.setattr(cla.lookup_contacts, "apply_async", refused)

    assert await jobs_routes._publish_contact_lookup(cla.lookup_contacts,
                                                     str(uuid.uuid4())) is False
    assert attempts == ["refused"]
    for _ in range(50):
        if _all_slots_free():
            break
        time.sleep(0.05)
    else:
        pytest.fail("a refused publish kept its slot")


async def test_a_pool_that_will_not_start_the_publish_frees_the_slot(monkeypatch):
    """FAULT INJECTION (labelled): the executor refuses the job (e.g. shut down)."""
    attempts: list[str] = []

    def no_threads(*args, **kwargs):
        attempts.append("submit")
        raise RuntimeError("injected: cannot schedule new futures after shutdown")

    monkeypatch.setattr(jobs_routes._publish_pool, "submit", no_threads)

    assert await jobs_routes._publish_contact_lookup(cla.lookup_contacts,
                                                     str(uuid.uuid4())) is False
    assert attempts == ["submit"]
    assert _all_slots_free()


async def test_a_forced_race_takes_the_unique_constraint_path_and_returns_the_winner(
    db, client, business_user, business_token, _lookups_on, published, monkeypatch,
):
    """Both confirms pass the replay check BEFORE either inserts (a barrier on a
    PASS-THROUGH spy of the lookup), so the loser really hits
    `uq_contact_lookup_actions_quote`, rolls back and re-fetches the winner."""
    job = _job(business_user.id)
    _seed(business_user.id, job, [{}, {}])
    qid = await _quoted(client, business_token, job)
    real = jobs_routes._action_for_quote
    calls: list = []
    both_checked = asyncio.Event()

    async def barrier_spy(db_, quote_id, user_id):
        row = await real(db_, quote_id, user_id)
        calls.append(row)
        if len(calls) == 2:
            both_checked.set()
        if len(calls) <= 2:
            await asyncio.wait_for(both_checked.wait(), 10)
        return row

    monkeypatch.setattr(jobs_routes, "_action_for_quote", barrier_spy)
    a, b = await asyncio.gather(_confirm(client, business_token, job, qid),
                                _confirm(client, business_token, job, qid))

    assert (a.status_code, b.status_code) == (202, 202), (a.text, b.text)
    assert a.json()["action_id"] == b.json()["action_id"]
    assert _actions_of(business_user.id) == 1
    # Two pre-checks that both saw nothing, then the loser's re-fetch of the winner.
    assert len(calls) == 3 and calls[0] is None and calls[1] is None
    assert calls[2] is not None


async def test_a_publish_that_hangs_is_bounded_and_frees_its_slot(
    db, client, business_user, business_token, _lookups_on, monkeypatch,
):
    """FAULT INJECTION (labelled): the broker publish blocks. The confirm answers 202
    within the bound, and once the publish returns its slot is free again."""
    import threading

    job = _job(business_user.id)
    _seed(business_user.id, job, [{}])
    qid = await _quoted(client, business_token, job)
    release, entered = threading.Event(), threading.Event()

    def hangs(*args, **kwargs):
        entered.set()
        release.wait(20)

    monkeypatch.setattr(cla.lookup_contacts, "apply_async", hangs)
    started = time.monotonic()
    r = await _confirm(client, business_token, job, qid)
    elapsed = time.monotonic() - started
    release.set()

    assert r.status_code == 202, r.text
    assert entered.is_set()  # the publish really started and hung
    assert elapsed < 6.0, elapsed
    assert _action(r.json()["action_id"]).dispatched_at is None
    for _ in range(50):  # the publish thread returns and releases its slot
        if jobs_routes._publish_slots.acquire(blocking=False):
            break
        time.sleep(0.1)
    else:
        pytest.fail("the hung publish never released its slot")
    jobs_routes._publish_slots.release()


def test_the_confirm_accepts_every_stop_the_planner_can_record():
    """`_QUOTE_STOPS` mirrors `window.stopped`. A new stop reason in the planner that
    the confirm does not know would refuse every such quote as unsupported."""
    import ast
    import inspect

    from src.api import contact_lookup_planner as planner

    assigned = {
        n.value.value for n in ast.walk(ast.parse(inspect.getsource(planner)))
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
        and any(isinstance(t, ast.Attribute) and t.attr == "stopped" for t in n.targets)
    }
    assert assigned, "no `.stopped = ...` assignment found: the scan is stale"
    assert assigned <= set(jobs_routes._QUOTE_STOPS), assigned


def test_the_api_never_imports_the_worker_module_at_load():
    """#393: a module-level import of a worker module from the API router closes an
    import loop. The confirm imports `lookup_contacts` inside the handler only."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(jobs_routes))
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = {n.module for n in top if isinstance(n, ast.ImportFrom)} | {
        a.name for n in top if isinstance(n, ast.Import) for a in n.names}
    assert not any(m and m.startswith("src.workers") for m in names), names
