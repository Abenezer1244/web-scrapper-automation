"""POST /jobs/{job_id}/contact-lookups/quote (Phase 1b-1c-ii).

Contract: tasks/todo-lookup-contacts.md, "FINAL 1b-1c contract and build list" and its
r3 amendments (R1 entitlement gate, R3 `remaining`, R4 Redis before the scan, R5 the
bounded limiter).

Real PG and real Redis. Redis FAILURE is real too, never stubbed: a closed local port,
a socket that accepts and never answers (blackholed), and a Redis that answers PING and
reads but refuses every write: an ACL user without `@write` on Redis >= 6 (CI), else a
private redis-server run with `maxmemory 1` + `noeviction` (Redis 5 has no ACLs).
Tracerfy is never reached: a quote spends nothing.
"""
from __future__ import annotations

import importlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

import pytest
import redis as sync_redis
import redis.asyncio as aioredis
from sqlalchemy import event, text

from src.api import contact_lookup_planner as planner
from src.api.middleware import auth_hardening
from src.api.quota import run_eligibility
from src.api.routes import jobs as jobs_routes
from src.config import settings
from src.db import session as db_session
from src.db.models import Job, Result, ScraperConfig
from src.db.session import system_sync_session
from src.utils import skip_trace_pause_state as pause_state
from src.utils.skip_trace_pause_state import NEVER, ScopeResume, fence_str

# By module path: `src.api.middleware` re-exports the `rate_limit` FUNCTION, which
# shadows the submodule of the same name.
rate_limit_module = importlib.import_module("src.api.middleware.rate_limit")

_PARTY = "SAARENAS AVELINO G"


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def _lookups_on(monkeypatch):
    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "test-token-not-real")


@pytest.fixture(autouse=True)
def _fresh_lookup_client():
    """The endpoint's sync client is module-level: rebuild it per test, so a test
    that points REDIS_URL elsewhere gets a client built from THAT url."""
    jobs_routes._lookup_redis_client = None
    yield
    client, jobs_routes._lookup_redis_client = jobs_routes._lookup_redis_client, None
    if client is not None:
        client.close()


@pytest.fixture
def _other_clients_built():
    """Build auth's and the limiter's async clients on the GOOD url first, so a test
    that then breaks REDIS_URL breaks only the quote's own client."""
    auth_hardening._get_redis()
    rate_limit_module._get_redis()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _quote(client, token: str, job_id: str, category: str = "new"):
    return await client.post(f"/jobs/{job_id}/contact-lookups/quote",
                             json={"category": category}, headers=_auth(token))


def _job(user_id: str, status: str = "done") -> str:
    sc_id, job_id = str(uuid.uuid4()), str(uuid.uuid4())
    with system_sync_session() as db:
        db.add(ScraperConfig(
            id=sc_id, user_id=user_id, name="quote", county="clark", state="WA",
            record_type="probate", fields=[], enrichment=[],
            schedule={"frequency": "manual"}, deliver={"formats": ["csv"], "emails": []},
            skip_trace_enabled=False,
        ))
        db.flush()
        db.add(Job(id=job_id, user_id=user_id, scraper_config_id=sc_id,
                   status=status, trigger="manual"))
        db.commit()
    return job_id


def _seed(user_id: str, job_id: str, specs: list[dict]) -> list[str]:
    ids = []
    with system_sync_session() as db:
        for n, spec in enumerate(specs):
            rid = str(uuid.uuid4())
            fields = {
                "id": rid, "job_id": job_id, "user_id": user_id, "party_name": _PARTY,
                "property_address": f"{2000 + n} MAIN ST", "property_city": "VANCOUVER",
                "property_state": "WA", "property_zip": "98661",
                "skip_trace_status": "not_attempted", "is_duplicate": False,
                "enrichment_data": {},
            }
            db.add(Result(**{**fields, **spec}))
            ids.append(rid)
        db.commit()
    return ids


def _stored(redis_client, user_id: str, job_id: str, category: str = "new") -> dict | None:
    raw = redis_client.get(jobs_routes._quote_key(user_id, job_id, category))
    return None if raw is None else json.loads(raw)


def _quote_keys(redis_client) -> list[str]:
    return list(redis_client.scan_iter("bridgeleads:contact_lookup:quote:*"))


async def _set_user(db, user_id: str, **cols) -> None:
    sets = ", ".join(f"{k} = :{k}" for k in cols)
    await db.execute(text(f"UPDATE users SET {sets} WHERE id = :uid"), {**cols, "uid": user_id})
    await db.commit()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextmanager
def _blackhole():
    """A port that completes the TCP handshake (kernel backlog) and never answers."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(8)
    try:
        yield s.getsockname()[1]
    finally:
        s.close()


def _assert_refuses_writes(url: str) -> None:
    """PING and a read succeed and a write is REFUSED by the server (a ResponseError:
    a connection error would also be a RedisError and prove nothing)."""
    probe = sync_redis.from_url(url, socket_timeout=0.5)
    try:
        assert probe.ping()
        probe.get("probe")
        with pytest.raises(sync_redis.ResponseError):
            probe.set("probe", "x")
    finally:
        probe.close()


@contextmanager
def _write_refusing_redis():
    """A real Redis URL that serves PING and reads, and refuses writes.

    Redis >= 6 (CI's service container): a throwaway ACL user on the test Redis with
    every command but `@write`. Redis 5 has no ACLs (the local rig): a private
    redis-server with `maxmemory 1` + `noeviction`, found via BL_TEST_REDIS_SERVER or
    PATH. Neither -> the test FAILS; it never skips."""
    base = settings.REDIS_URL
    admin = sync_redis.from_url(base, **settings.redis_kwargs())
    try:
        admin.execute_command("ACL", "WHOAMI")
        has_acl = True
    except sync_redis.ResponseError:
        has_acl = False
    if has_acl:
        user, password = f"bl_quote_ro_{uuid.uuid4().hex[:12]}", secrets.token_hex(16)
        admin.execute_command("ACL", "SETUSER", user, "on", f">{password}", "~*",
                              "+@all", "-@write")
        try:
            parts = urlsplit(base)
            netloc = f"{user}:{password}@{parts.hostname}:{parts.port or 6379}"
            url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))
            _assert_refuses_writes(url)
            yield url
        finally:
            admin.execute_command("ACL", "DELUSER", user)
            admin.close()
        return
    admin.close()
    binary = os.environ.get("BL_TEST_REDIS_SERVER") or shutil.which("redis-server")
    if not binary:
        pytest.fail("the test Redis has no ACLs and no redis-server binary was found: "
                    "set BL_TEST_REDIS_SERVER to one")
    port = _free_port()
    proc = subprocess.Popen(
        [binary, "--port", str(port), "--bind", "127.0.0.1",
         "--maxmemory", "1", "--maxmemory-policy", "noeviction",
         "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        url = f"redis://127.0.0.1:{port}/0"
        probe = sync_redis.from_url(url, socket_timeout=0.5)
        for _ in range(50):
            try:
                probe.ping()
                break
            # Not listening yet: refused, or (Windows) a connect timeout.
            except (sync_redis.ConnectionError, sync_redis.TimeoutError):
                time.sleep(0.1)
        probe.close()
        _assert_refuses_writes(url)
        yield url
    finally:
        proc.terminate()
        proc.wait(10)


@contextmanager
def _results_queries():
    """Count statements that read `results`, on the engine the request uses."""
    engine = db_session.async_engine.sync_engine
    seen: list[str] = []

    def _before(conn, cursor, statement, params, context, executemany):
        if "FROM results" in statement:
            seen.append(statement)

    event.listen(engine, "before_cursor_execute", _before)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", _before)


# ── the gates ────────────────────────────────────────────────────────────────


async def test_another_accounts_job_is_404_and_stores_nothing(
    db, client, business_user, business_token, starter_user, redis_client, _lookups_on,
):
    theirs = _job(starter_user.id)
    _seed(starter_user.id, theirs, [{}])
    r = await _quote(client, business_token, theirs)
    assert r.status_code == 404
    assert _quote_keys(redis_client) == []


async def test_a_run_that_has_not_finished_is_409(
    db, client, business_user, business_token, _lookups_on,
):
    job = _job(business_user.id, status="enriching")
    r = await _quote(client, business_token, job)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "run_not_finished"


async def test_a_plan_without_lookups_gets_the_structured_402(
    db, client, starter_user, starter_token, _lookups_on,
):
    job = _job(starter_user.id)
    r = await _quote(client, starter_token, job)
    assert r.status_code == 402
    assert r.json()["detail"]["code"] == "skip_trace"


@pytest.mark.parametrize(("cols", "code"), [
    ({"subscription_status": "unpaid"}, "frozen"),
    ({"subscription_status": "past_due",
      "entitlement_grace_ends_at": datetime.now(UTC) - timedelta(days=1)}, "frozen"),
    ({"entitlement_ends_at": datetime.now(UTC) - timedelta(days=1)}, "ended"),
])
async def test_a_frozen_or_ended_account_is_refused(
    db, client, business_user, business_token, _lookups_on, cols, code,
):
    job = _job(business_user.id)
    await _set_user(db, business_user.id, **cols)
    r = await _quote(client, business_token, job)
    assert r.status_code == 402
    assert r.json()["code"] == code


async def test_an_account_over_its_record_limit_can_still_quote_lookups(
    db, client, business_user, business_token, _lookups_on,
):
    """A lookup never counts as a record, so the record allowance does not refuse."""
    job = _job(business_user.id)
    _seed(business_user.id, job, [{}])
    await _set_user(db, business_user.id, records_used=5000, records_limit=5000)
    await db.refresh(business_user)
    assert run_eligibility(business_user).code == "over_limit"  # the case is real
    r = await _quote(client, business_token, job)
    assert r.status_code == 200, r.text
    assert r.json()["max_new_lookups"] == 1


@pytest.mark.parametrize(("flag", "value"), [("SKIP_TRACE_ENABLED", False),
                                             ("TRACERFY_API_TOKEN", "")])
async def test_lookups_switched_off_are_a_503(
    db, client, business_user, business_token, redis_client, _lookups_on, monkeypatch,
    flag, value,
):
    job = _job(business_user.id)
    monkeypatch.setattr(settings, flag, value)
    r = await _quote(client, business_token, job)
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "contact_lookups_unavailable"
    assert _quote_keys(redis_client) == []


async def test_an_unknown_category_or_field_is_422(
    db, client, business_user, business_token, _lookups_on,
):
    job = _job(business_user.id)
    for body in ({"category": "everything"}, {"category": "new", "q": "x"}):
        r = await client.post(f"/jobs/{job}/contact-lookups/quote", json=body,
                              headers=_auth(business_token))
        assert r.status_code == 422, body


# ── the quote ────────────────────────────────────────────────────────────────


async def test_the_quote_counts_the_tab_and_stores_the_quoted_set(
    db, client, business_user, business_token, redis_client, _lookups_on,
):
    job = _job(business_user.id)
    normal = _seed(business_user.id, job, [{}, {}, {}])
    [advanced] = _seed(business_user.id, job, [{"party_name": None}])
    others = _seed(business_user.id, job, [
        {"property_address": "  ", "mailing_address": "9 ELM ST, SEATTLE, WA 98101"},
        {"property_address": "UNKNOWN UNKNOWN, VANCOUVER WA 98661"},
        {"skip_trace_status": "queued"},
        {"skip_trace_status": "hit"},
        {"skip_trace_status": "errored"},
    ])
    # The other tab of the same job is not in this quote.
    _seed(business_user.id, job, [{"is_duplicate": True, "duplicate_reason": "prior_run"}])

    r = await _quote(client, business_token, job)
    assert r.status_code == 200, r.text
    q = r.json()
    assert q["max_new_lookups"] == 4
    assert q["advanced_count"] == 1
    assert q["examined"] == 6
    assert q["excluded"] == {"no_address": 1, "placeholder": 1, "settled_code_violation": 0,
                             "atip": 0, "not_traceable": 0}
    assert (q["in_progress"], q["already_answered"], q["previously_attempted"]) == (1, 1, 1)
    assert (q["truncated"], q["truncated_reason"], q["remaining"]) == (False, None, 0)
    assert (q["access"], q["trial_credit_allowance"], q["over_trial_allowance"]) == \
        ("full", None, 0)
    assert q["included_lookups_remaining"] == 1000
    assert (q["unit_price_cents"], q["currency"]) == (8, "USD")
    assert q["pause"]["status"] == "unknown"  # nothing published in this test
    # No lead id ever leaves in the response.
    for rid in [*normal, advanced, *others]:
        assert rid not in r.text

    stored = _stored(redis_client, business_user.id, job)
    assert stored["v"] == 2
    assert stored["quote_id"] == q["quote_id"]
    assert (stored["access"], stored["trial_credit_allowance"], stored["quoted_credits"]) == \
        ("full", None, 5)
    assert (stored["user_id"], stored["job_id"], stored["category"]) == \
        (business_user.id, job, "new")
    assert set(stored["quoted_ids"]) == {*normal, advanced}
    assert stored["planner_version"] == planner.PLANNER_VERSION
    assert stored["policy"] == {"pierce_cv_owner_skip_trace_enabled": False}
    assert (stored["unit_price_cents"], stored["currency"], stored["pricing_version"]) == \
        (8, "USD", "2026-06")
    assert stored["included_remaining_at_quote"] == 1000
    expires = datetime.fromisoformat(q["expires_at"].replace("Z", "+00:00"))
    assert datetime.fromisoformat(stored["expires_at"]) == expires
    assert timedelta(seconds=590) < expires - datetime.now(UTC) <= timedelta(seconds=600)
    ttl = redis_client.ttl(jobs_routes._quote_key(business_user.id, job, "new"))
    assert 0 < ttl <= 600


async def test_a_new_quote_replaces_the_tabs_previous_one(
    db, client, business_user, business_token, redis_client, _lookups_on,
):
    job = _job(business_user.id)
    _seed(business_user.id, job, [{}])
    first = (await _quote(client, business_token, job)).json()["quote_id"]
    second = (await _quote(client, business_token, job)).json()["quote_id"]
    assert first != second
    assert _stored(redis_client, business_user.id, job)["quote_id"] == second
    assert len(_quote_keys(redis_client)) == 1
    # The other tab keeps its own key.
    await _quote(client, business_token, job, "already_delivered")
    assert len(_quote_keys(redis_client)) == 2


async def test_each_tab_quotes_only_its_own_leads(
    db, client, business_user, business_token, redis_client, _lookups_on,
):
    job = _job(business_user.id)
    [new] = _seed(business_user.id, job, [{}])
    delivered = _seed(business_user.id, job, [
        {"is_duplicate": True, "duplicate_reason": "prior_run"},
        {"is_duplicate": True, "duplicate_reason": "prior_run", "party_name": None},
    ])
    q_new = (await _quote(client, business_token, job, "new")).json()
    q_dup = (await _quote(client, business_token, job, "already_delivered")).json()
    assert (q_new["max_new_lookups"], q_new["advanced_count"]) == (1, 0)
    assert (q_dup["max_new_lookups"], q_dup["advanced_count"]) == (2, 1)
    assert q_dup["category"] == "already_delivered"
    assert _stored(redis_client, business_user.id, job, "new")["quoted_ids"] == [new]
    assert set(_stored(redis_client, business_user.id, job,
                       "already_delivered")["quoted_ids"]) == set(delivered)


async def test_a_free_trial_is_quoted_up_to_its_lifetime_allowance(
    db, client, business_user, business_token, redis_client, _lookups_on, monkeypatch,
):
    """A trial (a paid plan name, no subscription) may buy at most its lifetime
    allowance (audit S3-03/S4-01); the quote says so instead of offering the tab."""
    monkeypatch.setattr(settings, "SKIP_TRACE_TRIAL_CREDIT_ALLOWANCE", 3)
    await _set_user(db, business_user.id, plan="pro", subscription_status=None,
                    trial_ends_at=datetime.now(UTC) + timedelta(days=7))
    job = _job(business_user.id)
    t0 = datetime(2026, 9, 1, tzinfo=UTC)
    ids = _seed(business_user.id, job,
                [{"created_at": t0 + timedelta(seconds=i)} for i in range(5)])
    q = (await _quote(client, business_token, job)).json()
    assert (q["access"], q["trial_credit_allowance"]) == ("trial", 3)
    assert q["max_new_lookups"] == 3
    assert (q["truncated"], q["truncated_reason"], q["remaining"]) == (True, "credit_cap", 2)
    assert q["unit_price_cents"] == 8
    stored = _stored(redis_client, business_user.id, job)
    assert stored["quoted_ids"] == ids[:3]
    assert (stored["access"], stored["trial_credit_allowance"], stored["quoted_credits"]) == \
        ("trial", 3, 3)


async def test_ten_quotes_a_minute_then_429(
    db, client, business_user, business_token, _lookups_on,
):
    """The quote spends its OWN bucket (`lookup_quote`), never the download budget."""
    job = _job(business_user.id)
    for i in range(10):
        r = await _quote(client, business_token, job)
        assert r.status_code == 200, f"quote {i + 1}: {r.status_code}"
    assert (await _quote(client, business_token, job)).status_code == 429
    # The download budget (`export`) is untouched: any answer but 429.
    r = await client.get(f"/jobs/{job}/export-url", headers=_auth(business_token))
    assert r.status_code != 429, r.text


async def test_a_tab_past_the_cap_is_truncated_with_what_is_left(
    db, client, business_user, business_token, redis_client, _lookups_on,
):
    job = _job(business_user.id)
    _seed(business_user.id, job, [{} for _ in range(planner.QUOTE_CAP + 3)])
    q = (await _quote(client, business_token, job)).json()
    assert q["max_new_lookups"] == planner.QUOTE_CAP
    assert (q["truncated"], q["truncated_reason"], q["remaining"]) == (True, "cap", 3)
    assert len(_stored(redis_client, business_user.id, job)["quoted_ids"]) == planner.QUOTE_CAP


# ── the pause state, through the real publisher ──────────────────────────────


def _publish(redis_client, *, glob=ScopeResume(None, None), default=ScopeResume(None, None),
             accounts=None, now=None) -> None:
    pause_state.publish(
        redis_client, fence=fence_str(time.time_ns() // 1000), now=now or datetime.now(UTC),
        interval_s=300, global_scope=glob, account_default=default, accounts=accounts or {},
    )


async def test_an_account_paused_by_its_cap_is_told_when_it_resumes(
    db, client, business_user, business_token, redis_client, _lookups_on,
):
    job = _job(business_user.id)
    _seed(business_user.id, job, [{}])
    normal_at = datetime.now(UTC) + timedelta(hours=3)
    advanced_at = datetime.now(UTC) + timedelta(hours=5)
    _publish(redis_client, accounts={business_user.id: ScopeResume(normal_at, advanced_at)})
    p = (await _quote(client, business_token, job)).json()["pause"]
    assert p["status"] == "paused"
    assert datetime.fromisoformat(p["normal_resume_at"]) == normal_at
    assert datetime.fromisoformat(p["advanced_resume_at"]) == advanced_at


async def test_the_global_cap_pauses_every_account(
    db, client, business_user, business_token, redis_client, _lookups_on,
):
    job = _job(business_user.id)
    at = datetime.now(UTC) + timedelta(hours=2)
    _publish(redis_client, glob=ScopeResume(at, at))
    p = (await _quote(client, business_token, job)).json()["pause"]
    assert (p["status"], datetime.fromisoformat(p["normal_resume_at"])) == ("paused", at)


async def test_advanced_lookups_that_can_never_run_say_never(
    db, client, business_user, business_token, redis_client, _lookups_on,
):
    job = _job(business_user.id)
    _publish(redis_client, default=ScopeResume(None, NEVER))
    p = (await _quote(client, business_token, job)).json()["pause"]
    assert p == {"status": "paused", "normal_resume_at": None, "advanced_resume_at": "never"}


async def test_nothing_binding_reads_not_paused(
    db, client, business_user, business_token, redis_client, _lookups_on,
):
    job = _job(business_user.id)
    _publish(redis_client)
    p = (await _quote(client, business_token, job)).json()["pause"]
    assert p == {"status": "not_paused", "normal_resume_at": None, "advanced_resume_at": None}


async def test_a_stale_or_switched_off_publisher_reads_unknown(
    db, client, business_user, business_token, redis_client, _lookups_on,
):
    job = _job(business_user.id)
    _publish(redis_client, now=datetime.now(UTC) - timedelta(hours=1))  # heartbeat stale
    assert (await _quote(client, business_token, job)).json()["pause"]["status"] == "unknown"
    pause_state.publish_tombstone(redis_client, fence=fence_str(time.time_ns()), interval_s=300)
    assert (await _quote(client, business_token, job)).json()["pause"]["status"] == "unknown"


# ── Redis failing: the quote is refused, and does no DB work first ───────────


async def test_redis_on_a_closed_port_is_a_503_before_any_scan(
    db, client, business_user, business_token, _lookups_on, _other_clients_built, monkeypatch,
):
    job = _job(business_user.id)
    _seed(business_user.id, job, [{}])
    monkeypatch.setattr(settings, "REDIS_URL", f"redis://127.0.0.1:{_free_port()}/0")
    with _results_queries() as seen:
        r = await _quote(client, business_token, job)
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "contact_lookups_unavailable"
    assert seen == []


async def test_a_blackholed_redis_is_a_bounded_503_before_any_scan(
    db, client, business_user, business_token, _lookups_on, _other_clients_built, monkeypatch,
):
    job = _job(business_user.id)
    _seed(business_user.id, job, [{}])
    with _blackhole() as port:
        monkeypatch.setattr(settings, "REDIS_URL", f"redis://127.0.0.1:{port}/0")
        started = time.monotonic()
        with _results_queries() as seen:
            r = await _quote(client, business_token, job)
        elapsed = time.monotonic() - started
    assert r.status_code == 503
    assert seen == []
    assert elapsed < 3.0, elapsed


async def test_a_stalled_rate_limiter_is_a_bounded_503(
    db, client, business_user, business_token, _lookups_on, _other_clients_built,
):
    """R5: the limiter's own async client has no socket timeout."""
    job = _job(business_user.id)
    _seed(business_user.id, job, [{}])
    good = rate_limit_module._redis_client
    with _blackhole() as port:
        rate_limit_module._redis_client = aioredis.from_url(f"redis://127.0.0.1:{port}/0")
        try:
            started = time.monotonic()
            with _results_queries() as seen:
                r = await _quote(client, business_token, job)
            elapsed = time.monotonic() - started
        finally:
            stalled, rate_limit_module._redis_client = rate_limit_module._redis_client, good
    await stalled.aclose()
    assert r.status_code == 503
    assert seen == []
    assert elapsed < 3.0, elapsed


async def test_a_quote_redis_cannot_store_is_never_shown(
    db, client, business_user, business_token, _lookups_on, _other_clients_built, monkeypatch,
):
    """PING and reads work, the write is refused: no quote in the body."""
    job = _job(business_user.id)
    _seed(business_user.id, job, [{}])
    with _write_refusing_redis() as url:
        monkeypatch.setattr(settings, "REDIS_URL", url)
        r = await _quote(client, business_token, job)
    assert r.status_code == 503
    assert "quote_id" not in r.text


# ── the tab queries as the real API role ─────────────────────────────────────


async def test_the_tab_queries_run_as_the_api_role(db, business_user):
    """The quote runs as `bridgeleads_app`, which has NO grant on the queue tables
    (memory: worker-only tables unreadable by the API role). Run every tab query as
    that role, under RLS with the tenant set, and prove the role switch is real."""
    job = _job(business_user.id)
    rid = _seed(business_user.id, job, [{}])[0]
    await db.execute(text(
        "DO $r$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = "
        "'bridgeleads_app') THEN CREATE ROLE bridgeleads_app NOLOGIN NOBYPASSRLS; "
        "END IF; END $r$"
    ))
    await db.execute(text("GRANT USAGE ON SCHEMA public TO bridgeleads_app"))
    await db.execute(text("GRANT SELECT ON public.results TO bridgeleads_app"))
    await db.execute(text("SET LOCAL ROLE bridgeleads_app"))
    await db.execute(text("SELECT set_config('app.current_user_id', :u, true)"),
                     {"u": business_user.id})
    today = datetime.now(UTC).date()
    policy = planner.policy_from_settings()
    counts = await planner.tab_status_counts(db, job, business_user.id, "new", today)
    window = await planner.plan_tab_window(db, job, business_user.id, "new", today, policy)
    remaining = await planner.count_remaining(db, job, business_user.id, "new", today, window)
    assert counts == {"not_attempted": 1}
    assert window.quoted_ids == [rid]
    assert remaining == 0
    with pytest.raises(Exception, match="permission denied"):
        await db.execute(text("SELECT 1 FROM pending_skip_trace_rows LIMIT 1"))
    await db.rollback()
