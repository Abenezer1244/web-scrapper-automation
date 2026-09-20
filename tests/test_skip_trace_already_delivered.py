"""Already delivered is not the same fact as already skip traced.

Owner report, 2026-09-18: a run with skip trace OFF delivered 38 leads. The same range
run again with skip trace ON said "38 already delivered" and traced none of them,
because the enqueue (and the dispatcher, three times over) dropped every duplicate.
Dedup decides whether a lead is delivered and billed again. It must not decide
whether a lead the account already owns may get its phone and email.

Rules pinned here (tasks/todo-enrich-already-delivered.md):
  * skip trace OFF: nothing is traced, new or already delivered
  * an already-delivered lead with no reusable trace is queued like a new one
  * a settled hit or miss of this account inside SKIP_TRACE_CACHE_DAYS is copied,
    never bought again; an older one is bought again (the freshness rule)
  * a transport failure is retried; a lookup Tracerfy charged for and we could not
    attribute ('unmatched') is not re-bought every run
  * the lead stays already delivered: is_duplicate / duplicate_reason never move
  * same-run siblings stay untraced; nothing is ever copied across accounts

Real DB (conftest). Tracerfy is never reached: these stop at the queue.
"""
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from src.config import settings
from src.db.models import DeliveredRecord, Job, PendingSkipTraceRow, Result, ScraperConfig
from src.db.session import system_sync_session
from src.workers.property_identity import legacy_strong_signature

_PARTY = "SAARENAS AVELINO G"


@pytest.fixture
def _skip_trace_on(monkeypatch):
    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "test-token-not-real")


def _address(n: int) -> str:
    return f"{1400 + n} MAIN ST"


def _parcel(n: int) -> str:
    return f"01234{n:05d}"


def _hash(n: int) -> str:
    return legacy_strong_signature(_parcel(n), _address(n))


def _run(user_id: str, *, skip_on: bool, status: str = "enriching") -> str:
    """One scraper run: its own config (so skip_trace_enabled is per run) and job."""
    sc_id, job_id = str(uuid.uuid4()), str(uuid.uuid4())
    with system_sync_session() as db:
        db.add(ScraperConfig(
            id=sc_id, user_id=user_id, name="already delivered", county="clark",
            state="WA", record_type="probate", fields=[], enrichment=[],
            schedule={"frequency": "manual"}, deliver={"formats": ["csv"], "emails": []},
            skip_trace_enabled=skip_on,
        ))
        db.flush()
        db.add(Job(id=job_id, user_id=user_id, scraper_config_id=sc_id,
                   status=status, trigger="manual",
                   # /download serves a finished run's file only.
                   export_key=f"exports/{job_id}.csv" if status == "done" else None))
        db.commit()
    return job_id


def _lead(
    user_id: str, job_id: str, n: int, *, dup: bool = False, reason: str | None = None,
    status: str = "not_attempted", traced_days_ago: float | None = None,
    phone: str | None = None, email: str | None = None, source: str | None = None,
    traced_first: str = "AVELINO", traced_last: str = "SAARENAS",
    subject_hash: str | None = None,
) -> str:
    """A lead row. A non-duplicate claims its property for the account, exactly as the
    worker's dedup claim does; a duplicate is flagged against an earlier claim.

    A SETTLED lead (hit/miss) also carries `skip_trace_subject_hash`, because since
    098 every settled row does: ingest, the known-answer sweep and the enqueue cache
    hit all record whose answer it is. Without it the row is a pre-098 row, which
    fails closed and donates nothing. `traced_first` / `traced_last` override the
    owner that answer was bought for, which is how the contamination cases seed a
    DIFFERENT owner's contacts at the same property.
    """
    rid = str(uuid.uuid4())
    attempted = (datetime.now(UTC) - timedelta(days=traced_days_ago)
                 if traced_days_ago is not None else None)
    if subject_hash is None and status in ("hit", "miss"):
        from src.scrapers.enrichment.skip_trace import lookup_subject_key
        subject_hash = lookup_subject_key(
            user_id, _address(n), "VANCOUVER", "WA", "normal", traced_first, traced_last,
        )
    with system_sync_session() as db:
        db.add(Result(
            id=rid, job_id=job_id, user_id=user_id, party_name=_PARTY,
            parcel_id=_parcel(n), property_address=_address(n),
            property_city="VANCOUVER", property_state="WA", property_zip="98661",
            dedup_hash=_hash(n), is_duplicate=dup,
            duplicate_reason=(reason or "prior_run") if dup else None,
            skip_trace_status=status, skip_trace_attempted_at=attempted,
            skip_trace_source=source, skip_trace_subject_hash=subject_hash,
            phone=phone, email=email,
            phones=[{"number": phone, "type": "Mobile"}] if phone else None,
            emails=[email] if email else None,
        ))
        db.flush()
        if not dup:
            db.execute(text(
                "INSERT INTO delivered_records (id, user_id, dedup_hash, first_result_id, "
                "first_job_id, parcel_id, property_address) VALUES (:id, :u, :h, :r, :j, :p, :a) "
                "ON CONFLICT (user_id, dedup_hash) DO NOTHING"),
                {"id": str(uuid.uuid4()), "u": user_id, "h": _hash(n), "r": rid,
                 "j": job_id, "p": _parcel(n), "a": _address(n)})
        db.commit()
    return rid


def _enqueue(job_id: str, redis_client) -> None:
    """What run_scrape_job does for skip trace: reuse first (start of ENRICHING),
    then the enqueue once delivery is decided."""
    from src.workers.tasks_helpers.enrich import (
        _enqueue_skip_trace_rows,
        _reuse_enrichment_for_duplicates,
    )

    with system_sync_session() as db:
        job = db.get(Job, job_id)
        cfg = db.get(ScraperConfig, job.scraper_config_id)
        _reuse_enrichment_for_duplicates(db, job, job_id)
        _enqueue_skip_trace_rows(db, job, redis_client, job_id, cfg)


def _pending(result_id: str) -> int:
    with system_sync_session() as db:
        return db.execute(
            select(func.count()).select_from(PendingSkipTraceRow)
            .where(PendingSkipTraceRow.result_id == result_id)
        ).scalar_one()


def _row(result_id: str) -> Result:
    """ORM read: phone/email are encrypted at rest and only the mapped type decrypts."""
    with system_sync_session() as db:
        row = db.get(Result, result_id)
        db.expunge(row)
        return row


# ── 1, 2, 7: the switch still decides ────────────────────────────────────────


async def test_new_lead_with_skip_trace_off_is_delivered_untraced(
    business_user, redis_client, _skip_trace_on,
):
    job = _run(business_user.id, skip_on=False)
    lead = _lead(business_user.id, job, 1)

    _enqueue(job, redis_client)

    assert _pending(lead) == 0
    assert _row(lead).skip_trace_status == "not_attempted"


async def test_already_delivered_lead_with_skip_trace_off_is_not_traced(
    business_user, redis_client, _skip_trace_on,
):
    first = _run(business_user.id, skip_on=False)
    _lead(business_user.id, first, 1)
    again = _run(business_user.id, skip_on=False)
    dup = _lead(business_user.id, again, 1, dup=True)

    _enqueue(again, redis_client)

    assert _pending(dup) == 0
    assert _row(dup).skip_trace_status == "not_attempted"


async def test_new_lead_with_skip_trace_on_is_queued(
    business_user, redis_client, _skip_trace_on,
):
    job = _run(business_user.id, skip_on=True)
    lead = _lead(business_user.id, job, 1)

    _enqueue(job, redis_client)

    assert _pending(lead) == 1
    assert _row(lead).skip_trace_status == "queued"


# ── 3: the owner's report ────────────────────────────────────────────────────


async def test_already_delivered_never_traced_lead_is_queued_when_skip_trace_is_on(
    business_user, redis_client, _skip_trace_on,
):
    """Run 1 delivered it with skip trace off; run 2 turns skip trace on. The lead is
    still already delivered, and it is now queued for its first lookup."""
    first = _run(business_user.id, skip_on=False)
    original = _lead(business_user.id, first, 1)
    _enqueue(first, redis_client)
    assert _row(original).skip_trace_status == "not_attempted"

    again = _run(business_user.id, skip_on=True)
    dup = _lead(business_user.id, again, 1, dup=True)
    _enqueue(again, redis_client)

    assert _pending(dup) == 1
    row = _row(dup)
    assert row.skip_trace_status == "queued"
    # Enrichment eligibility did not touch novelty: still already delivered.
    assert row.is_duplicate is True
    assert row.duplicate_reason == "prior_run"
    # One lead, not two: nothing was inserted to get it through the pipeline.
    with system_sync_session() as db:
        assert db.execute(select(func.count()).select_from(Result).where(
            Result.job_id == again)).scalar_one() == 1


# ── 4, 5: settled answers are reused, never bought twice ─────────────────────


async def test_a_prior_hit_is_copied_not_bought_again(
    business_user, redis_client, _skip_trace_on,
):
    first = _run(business_user.id, skip_on=True)
    _lead(business_user.id, first, 1, status="hit", traced_days_ago=3,
          phone="2065550100", email="owner@example.com")
    again = _run(business_user.id, skip_on=True)
    dup = _lead(business_user.id, again, 1, dup=True)

    _enqueue(again, redis_client)

    assert _pending(dup) == 0
    row = _row(dup)
    assert row.skip_trace_status == "hit"
    assert row.phone == "2065550100"
    assert row.email == "owner@example.com"
    assert row.skip_trace_source == "reused"  # reuse statement 1


async def test_a_trace_bought_on_a_later_run_is_reused_by_the_next_one(
    business_user, redis_client, _skip_trace_on,
):
    """Run 1 (off) delivered it; run 2 (on) traced ITS duplicate row; run 3 (on) must
    reuse run 2's answer. The original row is still untraced, so reuse that only reads
    the first delivery would buy the same lookup a second time."""
    first = _run(business_user.id, skip_on=False)
    _lead(business_user.id, first, 1)
    second = _run(business_user.id, skip_on=True, status="done")
    _lead(business_user.id, second, 1, dup=True, status="hit", traced_days_ago=1,
          phone="2065550111", email="later@example.com")
    third = _run(business_user.id, skip_on=True)
    dup = _lead(business_user.id, third, 1, dup=True)

    _enqueue(third, redis_client)

    assert _pending(dup) == 0
    row = _row(dup)
    assert row.skip_trace_status == "hit"
    assert row.phone == "2065550111"
    assert row.skip_trace_source == "reused"  # reuse statement 2


async def test_a_prior_no_contacts_answer_is_not_asked_again(
    business_user, redis_client, _skip_trace_on,
):
    """'miss' means Tracerfy answered and found nothing. It is a settled answer, so the
    lead is not re-bought on every run, and it stays distinct from never asked."""
    first = _run(business_user.id, skip_on=True)
    _lead(business_user.id, first, 1, status="miss", traced_days_ago=5)
    again = _run(business_user.id, skip_on=True)
    dup = _lead(business_user.id, again, 1, dup=True)

    _enqueue(again, redis_client)

    assert _pending(dup) == 0
    row = _row(dup)
    assert row.skip_trace_status == "miss"
    assert row.phone is None and row.email is None


async def test_a_trace_older_than_the_freshness_window_is_bought_again(
    business_user, redis_client, _skip_trace_on,
):
    first = _run(business_user.id, skip_on=True)
    _lead(business_user.id, first, 1, status="hit",
          traced_days_ago=settings.SKIP_TRACE_CACHE_DAYS + 5,
          phone="2065550122", email="stale@example.com")
    again = _run(business_user.id, skip_on=True)
    dup = _lead(business_user.id, again, 1, dup=True)

    _enqueue(again, redis_client)

    assert _pending(dup) == 1
    assert _row(dup).skip_trace_status == "queued"


async def test_the_tenant_cache_answers_an_already_delivered_lead(
    business_user, redis_client, _skip_trace_on,
):
    """The address cache ingest writes is the other reuse path; a fresh entry is copied
    onto the already-delivered row with no pending row."""
    from src.db.models import SkipTraceCache
    from src.scrapers.enrichment.skip_trace import lookup_subject_key

    first = _run(business_user.id, skip_on=False)
    _lead(business_user.id, first, 1)
    with system_sync_session() as db:
        db.add(SkipTraceCache(
            address_hash=lookup_subject_key(business_user.id, _address(1), "VANCOUVER", "WA", "normal", "AVELINO", "SAARENAS"),
            phone="2065550133", phone_type="Mobile", email="cache@example.com",
            phones=[{"number": "2065550133", "type": "Mobile"}], emails=["cache@example.com"],
            fetched_at=datetime.now(UTC) - timedelta(days=2),
        ))
        db.commit()
    again = _run(business_user.id, skip_on=True)
    dup = _lead(business_user.id, again, 1, dup=True)

    _enqueue(again, redis_client)

    assert _pending(dup) == 0
    row = _row(dup)
    assert row.skip_trace_status == "hit"
    assert row.phone == "2065550133"
    assert row.skip_trace_source == "reused"  # enqueue cache hit
    # The retention clock is when the data was obtained (2 days ago), not now.
    age = datetime.now(UTC) - row.skip_trace_attempted_at
    assert timedelta(days=1, hours=23) < age < timedelta(days=2, hours=1)


# ── 6: failures ──────────────────────────────────────────────────────────────


async def test_a_prior_transport_failure_is_retried(
    business_user, redis_client, _skip_trace_on,
):
    """'errored' with no charged lookup behind it (rejected before Tracerfy saw it) is
    not an answer. It is never copied, and the next run with skip trace on retries."""
    first = _run(business_user.id, skip_on=True)
    _lead(business_user.id, first, 1, status="errored", traced_days_ago=1)
    again = _run(business_user.id, skip_on=True)
    dup = _lead(business_user.id, again, 1, dup=True)

    _enqueue(again, redis_client)

    assert _pending(dup) == 1
    assert _row(dup).skip_trace_status == "queued"


async def test_a_charged_but_unattributable_lookup_is_not_rebought_every_run(
    business_user, redis_client, _skip_trace_on,
):
    """Tracerfy accepted and charged the lookup, and our address reconciliation could not
    attribute it ('unmatched', billed). Retrying on every run would buy the same failure
    again and again, so the lead is settled as errored instead of queued."""
    first = _run(business_user.id, skip_on=True, status="done")
    original = _lead(business_user.id, first, 1, status="errored", traced_days_ago=1)
    with system_sync_session() as db:
        db.add(PendingSkipTraceRow(
            job_id=first, result_id=original, user_id=business_user.id,
            property_address=_address(1), city="VANCOUVER", state="WA",
            trace_type="normal", status="unmatched",
            enqueued_at=datetime.now(UTC) - timedelta(days=1),
        ))
        db.commit()
    again = _run(business_user.id, skip_on=True)
    dup = _lead(business_user.id, again, 1, dup=True)

    _enqueue(again, redis_client)

    assert _pending(dup) == 0
    assert _row(dup).skip_trace_status == "errored"


# ── 8: a mixed run keeps its classification ──────────────────────────────────


async def test_mixed_run_traces_only_what_needs_a_lookup(
    business_user, redis_client, _skip_trace_on,
):
    """10 new, 20 already delivered never traced, 8 already delivered already traced."""
    earlier = _run(business_user.id, skip_on=False)
    for n in range(10, 30):
        _lead(business_user.id, earlier, n)
    for n in range(30, 38):
        _lead(business_user.id, earlier, n, status="hit", traced_days_ago=2,
              phone=f"20655502{n:02d}", email=f"o{n}@example.com")

    run = _run(business_user.id, skip_on=True)
    new = [_lead(business_user.id, run, n) for n in range(0, 10)]
    untraced = [_lead(business_user.id, run, n, dup=True) for n in range(10, 30)]
    traced = [_lead(business_user.id, run, n, dup=True) for n in range(30, 38)]

    _enqueue(run, redis_client)

    with system_sync_session() as db:
        new_count = db.execute(select(func.count()).select_from(Result).where(
            Result.job_id == run, Result.is_duplicate.is_(False))).scalar_one()
        dup_count = db.execute(select(func.count()).select_from(Result).where(
            Result.job_id == run, Result.is_duplicate.is_(True))).scalar_one()
    assert (new_count, dup_count) == (10, 28)
    assert all(_pending(r) == 1 for r in new + untraced)
    assert all(_pending(r) == 0 for r in traced)
    assert {_row(r).skip_trace_status for r in traced} == {"hit"}


# ── rows that are not already delivered stay out ─────────────────────────────


async def test_a_same_run_sibling_is_still_never_traced(
    business_user, redis_client, _skip_trace_on,
):
    """Another filing of a property this same run already traces: not already delivered,
    and tracing it would buy the same address twice in one run."""
    run = _run(business_user.id, skip_on=True)
    survivor = _lead(business_user.id, run, 1)
    sibling = _lead(business_user.id, run, 1, dup=True, reason="same_run")

    _enqueue(run, redis_client)

    assert _pending(survivor) == 1
    assert _pending(sibling) == 0
    assert _row(sibling).skip_trace_status == "not_attempted"


# ── 10: tenant isolation ─────────────────────────────────────────────────────


async def test_another_accounts_trace_is_never_copied(
    business_user, starter_user, redis_client, _skip_trace_on, db,
):
    """Account B paid for a hit on a parcel. Account A's already-delivered row on the
    same parcel must be looked up for A, never handed B's phone and email: reuse and
    cache are both keyed by the account."""
    from src.db.models import SkipTraceCache, User
    from src.scrapers.enrichment.skip_trace import lookup_subject_key

    other = await db.get(User, starter_user.id)
    other.plan = "business"
    await db.commit()

    b_run = _run(other.id, skip_on=True, status="done")
    _lead(other.id, b_run, 1, status="hit", traced_days_ago=1,
          phone="2065550199", email="b-only@example.com")
    with system_sync_session() as s:
        s.add(SkipTraceCache(
            address_hash=lookup_subject_key(other.id, _address(1), "VANCOUVER", "WA", "normal", "AVELINO", "SAARENAS"),
            phone="2065550199", email="b-only@example.com",
            fetched_at=datetime.now(UTC) - timedelta(days=1),
        ))
        s.commit()

    a_first = _run(business_user.id, skip_on=False)
    _lead(business_user.id, a_first, 1)
    a_again = _run(business_user.id, skip_on=True)
    a_dup = _lead(business_user.id, a_again, 1, dup=True)

    _enqueue(a_again, redis_client)

    row = _row(a_dup)
    assert row.phone is None and row.email is None
    assert row.skip_trace_status == "queued"
    assert _pending(a_dup) == 1
    # And the claim table never mixed the two accounts.
    with system_sync_session() as s:
        owners = set(s.execute(select(DeliveredRecord.user_id).where(
            DeliveredRecord.dedup_hash == _hash(1))).scalars())
    assert owners == {str(other.id), str(business_user.id)}


# ── the dispatcher buys an already-delivered lead, and buys one answer once ───
#
# No network: TRACERFY_API_BASE_URL is non-HTTPS, so submit_batch refuses before any
# socket opens (a definite rejection, released as 'errored'). A row the dispatcher
# CLAIMED therefore ends 'errored'; a row it held or settled never does.


@pytest.fixture
def _dispatcher_on(monkeypatch):
    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "test-token-not-real")
    monkeypatch.setattr(settings, "TRACERFY_API_BASE_URL", "http://tracerfy.invalid")
    monkeypatch.setattr(settings, "OPS_ALERT_EMAIL", "")


def _queue(user_id: str, job_id: str, result_id: str, n: int, *, status: str = "queued",
           submitted_days_ago: float | None = None) -> str:
    """A pending row as the enqueue writes it (and its Result's status to match)."""
    pid = str(uuid.uuid4())
    at = (datetime.now(UTC) - timedelta(days=submitted_days_ago)
          if submitted_days_ago is not None else None)
    with system_sync_session() as db:
        db.add(PendingSkipTraceRow(
            id=pid, job_id=job_id, result_id=result_id, user_id=user_id,
            property_address=_address(n), city="VANCOUVER", state="WA", zip="98661",
            first_name="AVELINO", last_name="SAARENAS", trace_type="normal",
            status=status, submitted_at=at,
        ))
        if status == "queued":
            db.execute(text("UPDATE results SET skip_trace_status = 'queued' WHERE id = :r"),
                       {"r": result_id})
        db.commit()
    return pid


def _pending_status(pending_id: str) -> str:
    with system_sync_session() as db:
        return db.execute(text("SELECT status FROM pending_skip_trace_rows WHERE id = :i"),
                          {"i": pending_id}).scalar_one()


def _dispatch() -> dict:
    from src.workers.skip_trace_dispatcher import dispatch_pending_skip_trace

    return dispatch_pending_skip_trace()


async def test_the_dispatcher_submits_an_already_delivered_lead(business_user, _dispatcher_on):
    """It used to withdraw every duplicate before the POST. An already-delivered lead of
    a delivered run now goes through the claim like any other."""
    first = _run(business_user.id, skip_on=False, status="done")
    _lead(business_user.id, first, 1)
    again = _run(business_user.id, skip_on=True, status="done")
    dup = _lead(business_user.id, again, 1, dup=True)
    pid = _queue(business_user.id, again, dup, 1)

    out = _dispatch()

    assert any("HTTPS" in e for e in out["errors"]), "the row never reached the claim"
    assert _pending_status(pid) == "errored"


async def test_a_run_that_did_not_deliver_does_not_buy_its_already_delivered_lookups(
    business_user, _dispatcher_on,
):
    """The contract made explicit (Codex): lookups are bought when the CURRENT run
    delivers. A failed run withdraws them unpaid and the lead can be traced next time."""
    first = _run(business_user.id, skip_on=False, status="done")
    _lead(business_user.id, first, 1)
    failed = _run(business_user.id, skip_on=True, status="failed")
    dup = _lead(business_user.id, failed, 1, dup=True)
    pid = _queue(business_user.id, failed, dup, 1)

    out = _dispatch()

    assert out["submitted_rows"] == 0 and not out["errors"]
    assert _pending_status(pid) == "cancelled"
    assert _row(dup).skip_trace_status == "not_attempted"


async def test_a_lookup_already_at_tracerfy_is_not_bought_again_for_its_twin(
    business_user, _dispatcher_on,
):
    """Run 1's lookup for lead X is at Tracerfy; run 2 queued X's already-delivered row.
    The twin is held, not submitted."""
    first = _run(business_user.id, skip_on=True, status="done")
    original = _lead(business_user.id, first, 1, status="submitted")
    in_flight = _queue(business_user.id, first, original, 1, status="submitted",
                       submitted_days_ago=0.01)
    again = _run(business_user.id, skip_on=True, status="done")
    dup = _lead(business_user.id, again, 1, dup=True)
    twin = _queue(business_user.id, again, dup, 1)

    out = _dispatch()

    assert out["submitted_rows"] == 0 and not out["errors"]
    assert _pending_status(twin) == "queued"
    assert _pending_status(in_flight) == "submitted"


async def test_two_runs_queuing_one_lead_submit_it_once(
    business_user, _dispatcher_on, monkeypatch,
):
    """Scenario 9: two runs found the same never-traced lead at once and both queued it.
    One batch carries one of them; the other waits for that answer."""
    monkeypatch.setattr(settings, "SKIP_TRACE_MAX_BATCHES_PER_TICK", 1)
    first = _run(business_user.id, skip_on=False, status="done")
    _lead(business_user.id, first, 1)
    a = _run(business_user.id, skip_on=True, status="done")
    b = _run(business_user.id, skip_on=True, status="done")
    row_a = _queue(business_user.id, a, _lead(business_user.id, a, 1, dup=True), 1)
    row_b = _queue(business_user.id, b, _lead(business_user.id, b, 1, dup=True), 1)

    _dispatch()

    assert sorted([_pending_status(row_a), _pending_status(row_b)]) == ["errored", "queued"]


async def test_a_held_twin_takes_the_answer_when_it_lands_and_is_not_billed(
    business_user, _dispatcher_on,
):
    """The twin's lookup landed (ingest wrote the tenant cache). The held row is settled
    from it on the next tick: contacts copied, pending row 'reused', nothing submitted."""
    from src.db.models import SkipTraceCache
    from src.scrapers.enrichment.skip_trace import lookup_subject_key

    first = _run(business_user.id, skip_on=False, status="done")
    _lead(business_user.id, first, 1)
    again = _run(business_user.id, skip_on=True, status="done")
    dup = _lead(business_user.id, again, 1, dup=True)
    held = _queue(business_user.id, again, dup, 1)
    with system_sync_session() as db:
        db.add(SkipTraceCache(
            address_hash=lookup_subject_key(business_user.id, _address(1), "VANCOUVER", "WA", "normal", "AVELINO", "SAARENAS"),
            phone="2065550144", phone_type="Mobile", email="landed@example.com",
            phones=[{"number": "2065550144", "type": "Mobile"}], emails=["landed@example.com"],
            fetched_at=datetime.now(UTC) - timedelta(days=3),
        ))
        db.commit()

    out = _dispatch()

    assert out["submitted_rows"] == 0 and not out["errors"]
    assert _pending_status(held) == "reused"
    row = _row(dup)
    assert (row.skip_trace_status, row.phone, row.email) == (
        "hit", "2065550144", "landed@example.com")
    assert row.is_duplicate is True
    assert row.skip_trace_source == "reused"  # dispatcher known-answer sweep
    age = datetime.now(UTC) - row.skip_trace_attempted_at  # retention clock kept
    assert timedelta(days=2, hours=23) < age < timedelta(days=3, hours=1)


async def test_a_twin_whose_lookup_was_charged_without_a_match_is_not_bought_again(
    business_user, _dispatcher_on,
):
    first = _run(business_user.id, skip_on=True, status="done")
    original = _lead(business_user.id, first, 1, status="errored", traced_days_ago=0)
    _queue(business_user.id, first, original, 1, status="unmatched", submitted_days_ago=0.1)
    again = _run(business_user.id, skip_on=True, status="done")
    dup = _lead(business_user.id, again, 1, dup=True)
    held = _queue(business_user.id, again, dup, 1)

    out = _dispatch()

    assert out["submitted_rows"] == 0 and not out["errors"]
    assert _pending_status(held) == "cancelled"
    assert _row(dup).skip_trace_status == "errored"


async def test_another_accounts_lookup_never_holds_or_answers_mine(
    business_user, starter_user, db, _dispatcher_on,
):
    """Account B's lookup for the same address is at Tracerfy and B has a fresh cache
    entry for it. Account A's row is neither held behind B's nor answered from B's."""
    from src.db.models import SkipTraceCache, User
    from src.scrapers.enrichment.skip_trace import lookup_subject_key

    other = await db.get(User, starter_user.id)
    other.plan = "business"
    await db.commit()
    b_run = _run(other.id, skip_on=True, status="done")
    b_lead = _lead(other.id, b_run, 1, status="submitted")
    _queue(other.id, b_run, b_lead, 1, status="submitted", submitted_days_ago=0.01)
    with system_sync_session() as s:
        s.add(SkipTraceCache(
            address_hash=lookup_subject_key(other.id, _address(1), "VANCOUVER", "WA", "normal", "AVELINO", "SAARENAS"),
            phone="2065550155", email="b-only@example.com", fetched_at=datetime.now(UTC),
        ))
        s.commit()

    a_first = _run(business_user.id, skip_on=False, status="done")
    _lead(business_user.id, a_first, 1)
    a_again = _run(business_user.id, skip_on=True, status="done")
    a_dup = _lead(business_user.id, a_again, 1, dup=True)
    mine = _queue(business_user.id, a_again, a_dup, 1)

    _dispatch()

    assert _pending_status(mine) == "errored", "A's row was held or settled by B's data"
    row = _row(a_dup)
    assert row.phone is None and row.email is None


async def test_a_tick_that_cannot_take_the_claim_lock_buys_nothing(
    business_user, _dispatcher_on,
):
    from src.workers.skip_trace_dispatcher import _CLAIM_LOCK_KEY

    first = _run(business_user.id, skip_on=False, status="done")
    _lead(business_user.id, first, 1)
    again = _run(business_user.id, skip_on=True, status="done")
    pid = _queue(business_user.id, again, _lead(business_user.id, again, 1, dup=True), 1)

    with system_sync_session() as other_tick:
        other_tick.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _CLAIM_LOCK_KEY})
        out = _dispatch()
        other_tick.rollback()

    assert out.get("deferred") == "claim_locked"
    assert _pending_status(pid) == "queued"


# ── 11, 12: after the answer lands: quota, the CSV, and run 3 ────────────────


async def test_an_answer_for_an_already_delivered_lead_bills_one_lookup_and_no_record(
    business_user, client, business_token, redis_client, monkeypatch,
):
    """The second half of the owner's scenario through the real ingest and the real
    download endpoint. Tracerfy answers for the already-delivered row; the Already
    delivered CSV carries its phones and emails in their own columns; skip-trace usage
    moves by exactly the one lookup bought and record usage does not move; the lead is
    still already delivered. A third run then reuses the answer and buys nothing.
    Only the provider's CSV download is stubbed (external paid API, no network)."""
    import csv
    import io

    from src.workers.tasks_helpers.enrich import (
        _enqueue_skip_trace_rows,
        _reuse_enrichment_for_duplicates,
    )
    from src.workers.tracerfy_ingest import ingest_tracerfy_batch

    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "test-token-not-real")
    monkeypatch.setattr(
        "src.scrapers.enrichment.skip_trace.download_tracerfy_csv",
        lambda url: (
            "address,city,state,first_name,last_name,primary_phone,primary_phone_type,"
            "Mobile-1,Mobile-2,Landline-1,Email-1,Email-2\n"
            f"{_address(1)},VANCOUVER,WA,AVELINO,SAARENAS,2065550166,Mobile,2065550166,"
            "2065550177,,owner@example.com,second@example.com\n"
        ),
    )

    def usage() -> tuple[int, int]:
        with system_sync_session() as s:
            return tuple(s.execute(text(
                "SELECT skip_trace_used_this_month, records_used FROM users WHERE id = :u"),
                {"u": business_user.id}).one())

    first = _run(business_user.id, skip_on=False, status="done")
    _lead(business_user.id, first, 1)
    again = _run(business_user.id, skip_on=True, status="done")
    dup = _lead(business_user.id, again, 1, dup=True)
    queue_id = int(uuid.uuid4().int % 10_000_000) + 910_000_000
    with system_sync_session() as s:
        s.add(PendingSkipTraceRow(
            job_id=again, result_id=dup, user_id=business_user.id,
            property_address=_address(1), city="VANCOUVER", state="WA",
            # The names the row was actually submitted with, matching the stubbed
            # CSV below and what build_pending_row_payload derives from _PARTY. A
            # 'normal' trace never has NULL names in production (that combination
            # is routed to 'advanced'), and since 098 the names are part of the
            # cache key, so omitting them here would key the answer to nobody.
            first_name="AVELINO", last_name="SAARENAS",
            trace_type="normal", status="submitted", submitted_at=datetime.now(UTC),
            tracerfy_queue_id=queue_id,
        ))
        s.execute(text("UPDATE results SET skip_trace_status = 'submitted' WHERE id = :r"),
                  {"r": dup})
        s.execute(text(
            "INSERT INTO skip_trace_queues (id, tracerfy_queue_id, job_id, user_id, "
            "trace_type, status, rows_uploaded, credits_deducted, submitted_at) "
            "VALUES (:id, :q, :j, :u, 'normal', 'pending', 1, 0, now())"),
            {"id": str(uuid.uuid4()), "q": queue_id, "j": again, "u": business_user.id})
        s.commit()
    lookups_before, records_before = usage()

    out = ingest_tracerfy_batch(
        queue_id=queue_id,
        download_url="https://tracerfy.nyc3.cdn.digitaloceanspaces.com/tracerfy/x.csv",
        rows_uploaded=1, credits_deducted=1,
    )

    assert out["hits"] == 1
    row = _row(dup)
    assert (row.skip_trace_status, row.phone, row.email) == (
        "hit", "2065550166", "owner@example.com")
    assert (row.is_duplicate, row.duplicate_reason) == (True, "prior_run")
    assert row.skip_trace_source == "lookup"  # Tracerfy answered this row
    lookups_after, records_after = usage()
    assert lookups_after - lookups_before == 1, "skip-trace usage must move by the one lookup"
    assert records_after == records_before, "an already-delivered lead was billed as a record"

    resp = await client.get(f"/jobs/{again}/download", params={"category": "already_delivered"},
                            headers={"Authorization": f"Bearer {business_token}"})
    assert resp.status_code == 200
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    assert len(rows) == 1
    # The default (legacy_v1) layout: every number and address in its own column.
    assert (rows[0]["phone"], rows[0]["phone_2"]) == ("2065550166", "2065550177")
    assert (rows[0]["email"], rows[0]["email_2"]) == ("owner@example.com", "second@example.com")

    third = _run(business_user.id, skip_on=True)
    third_dup = _lead(business_user.id, third, 1, dup=True)
    with system_sync_session() as s:
        job = s.get(Job, third)
        cfg = s.get(ScraperConfig, job.scraper_config_id)
        _reuse_enrichment_for_duplicates(s, job, third)
        _enqueue_skip_trace_rows(s, job, redis_client, third, cfg)
    assert _pending(third_dup) == 0
    assert _row(third_dup).phone == "2065550166"
    assert _row(third_dup).skip_trace_source == "reused"
    assert usage() == (lookups_after, records_after)


# ── the Already delivered tab says where the lookups stand ───────────────────


async def test_the_results_page_reports_the_lookup_state_of_already_delivered_leads(
    business_user, client, business_token, starter_token,
):
    """The tab's number is partitioned into found / none found / looking / failed / not
    looked up, from the same statement and predicate, so the parts always add up. A
    same-run sibling is in neither, and another account gets nothing."""
    earlier = _run(business_user.id, skip_on=False, status="done")
    for n in range(1, 7):
        _lead(business_user.id, earlier, n)
    run = _run(business_user.id, skip_on=True, status="done")
    _lead(business_user.id, run, 0)  # new
    _lead(business_user.id, run, 0, dup=True, reason="same_run")  # combined, not counted
    _lead(business_user.id, run, 1, dup=True, status="hit", traced_days_ago=0,
          phone="2065550188", email="found@example.com", source="lookup")
    _lead(business_user.id, run, 2, dup=True, status="hit", traced_days_ago=0,
          phone="2065550199", source="reused")
    _lead(business_user.id, run, 3, dup=True, status="miss", traced_days_ago=0,
          source="reused")
    _lead(business_user.id, run, 4, dup=True, status="queued")
    _lead(business_user.id, run, 5, dup=True, status="errored", traced_days_ago=0)
    _lead(business_user.id, run, 6, dup=True)

    resp = await client.get(f"/jobs/{run}/results", params={"category": "already_delivered"},
                            headers={"Authorization": f"Bearer {business_token}"})

    assert resp.status_code == 200
    page = resp.json()
    assert page["already_delivered_count"] == 6
    contacts = page["already_delivered_contacts"]
    assert contacts == {
        "found": 2, "none_found": 1, "looking": 1, "failed": 1, "not_looked_up": 1,
        # Of the 3 answered, 2 were copied from an earlier answer (one hit, one miss).
        "reused": 2,
    }
    buckets = ("found", "none_found", "looking", "failed", "not_looked_up")
    assert sum(contacts[b] for b in buckets) == page["already_delivered_count"]
    assert contacts["reused"] <= contacts["found"] + contacts["none_found"]
    # The rows the tab lists carry the contacts themselves.
    phones = {i["phone"] for i in page["items"] if i["phone"]}
    assert phones == {"2065550188", "2065550199"}

    other = await client.get(f"/jobs/{run}/results", params={"category": "already_delivered"},
                             headers={"Authorization": f"Bearer {starter_token}"})
    assert other.status_code == 404


async def test_a_twin_stuck_at_an_unknown_outcome_holds_for_as_long_as_it_takes(
    business_user, _dispatcher_on,
):
    """Codex P1: a lookup left 'submitting' by an unknown outcome may already be charged,
    and the system never buys past one. However old it is, its twin waits for the
    reconciler instead of being bought a second time."""
    first = _run(business_user.id, skip_on=True, status="done")
    original = _lead(business_user.id, first, 1, status="submitted")
    stuck = _queue(business_user.id, first, original, 1, status="submitting",
                   submitted_days_ago=3)
    again = _run(business_user.id, skip_on=True, status="done")
    twin = _queue(business_user.id, again, _lead(business_user.id, again, 1, dup=True), 1)

    from src.workers.skip_trace_dispatcher import _hold_answers_in_flight

    with system_sync_session() as db:
        head = db.execute(select(PendingSkipTraceRow).where(
            PendingSkipTraceRow.id == twin)).scalars().all()
        kept, held = _hold_answers_in_flight(db, head)
    assert (kept, held) == ([], 1)
    assert _pending_status(twin) == "queued"
    assert _pending_status(stuck) == "submitting"


def _cache(user_id: str, n: int, phone: str, *, first="AVELINO", last="SAARENAS") -> None:
    """An answer this account already paid for, under the v2 subject key (098).

    The key carries the OWNER, not just the address, so the default names match
    what `_queue` writes and what `_PARTY` splits to. Pass different names to seed
    a DIFFERENT owner's answer at the same address, which must not be reused.
    """
    from src.db.models import SkipTraceCache
    from src.scrapers.enrichment.skip_trace import lookup_subject_key

    with system_sync_session() as db:
        db.add(SkipTraceCache(
            address_hash=lookup_subject_key(
                user_id, _address(n), "VANCOUVER", "WA", "normal", first, last,
            ),
            phone=phone, phone_type="Mobile", email=None,
            phones=[{"number": phone, "type": "Mobile"}], emails=None,
            fetched_at=datetime.now(UTC),
        ))
        db.commit()


def _legacy_cache(user_id: str, n: int, phone: str) -> None:
    """An answer cached under the PRE-098 address-only key.

    Such a row cannot say whose answer it holds, which is the whole reason for the
    cutover. Seeded only to prove that nothing reads it any more.
    """
    from src.db.models import SkipTraceCache
    from src.scrapers.enrichment.skip_trace import address_cache_key

    with system_sync_session() as db:
        db.add(SkipTraceCache(
            address_hash=address_cache_key(user_id, _address(n), "VANCOUVER", "WA"),
            phone=phone, phone_type="Mobile", email=None,
            phones=[{"number": phone, "type": "Mobile"}], emails=None,
            fetched_at=datetime.now(UTC),
        ))
        db.commit()


async def test_an_answer_that_lands_after_the_sweep_is_still_not_bought_again(
    business_user, _dispatcher_on,
):
    """Codex round 2 P1: the twin's answer lands after this tick's known-answer sweep ran,
    so its original is 'completed' (no longer in flight) and the sweep never saw the
    cache. The claim path itself must refuse to buy it."""
    from src.workers.skip_trace_dispatcher import _hold_answers_in_flight

    first = _run(business_user.id, skip_on=True, status="done")
    original = _lead(business_user.id, first, 1, status="hit", traced_days_ago=0,
                     phone="2065550201")
    _queue(business_user.id, first, original, 1, status="completed", submitted_days_ago=0.01)
    _cache(business_user.id, 1, "2065550201")
    again = _run(business_user.id, skip_on=True, status="done")
    twin = _queue(business_user.id, again, _lead(business_user.id, again, 1, dup=True), 1)

    with system_sync_session() as db:
        head = db.execute(select(PendingSkipTraceRow).where(
            PendingSkipTraceRow.id == twin)).scalars().all()
        kept, held = _hold_answers_in_flight(db, head)
    assert (kept, held) == ([], 1)


async def test_the_sweep_never_hands_contacts_to_a_lead_it_may_not_contact(
    business_user, _dispatcher_on, monkeypatch,
):
    """Codex round 2 P2: an ATIP-named Tacoma lead may be named, not contacted, while the
    paid switch is off. A fresh cache answer for its address is NOT copied onto it; the
    row is left for the cancel sweep."""
    from src.scrapers.enrichment.pierce_atip_owner import OWNER_SOURCE
    from src.workers.skip_trace_dispatcher import _settle_queued_from_known_answers

    monkeypatch.setattr(settings, "PIERCE_CV_OWNER_SKIP_TRACE_ENABLED", False)
    first = _run(business_user.id, skip_on=False, status="done")
    _lead(business_user.id, first, 1)
    again = _run(business_user.id, skip_on=True, status="done")
    dup = _lead(business_user.id, again, 1, dup=True)
    with system_sync_session() as db:
        db.execute(text("UPDATE results SET enrichment_data = CAST(:ed AS json) WHERE id = :r"),
                   {"ed": json.dumps({"source": "tacoma_code_violations",
                                      "owner_source": OWNER_SOURCE}), "r": dup})
        db.commit()
    pid = _queue(business_user.id, again, dup, 1)
    _cache(business_user.id, 1, "2065550202")

    with system_sync_session() as db:
        _settle_queued_from_known_answers(db)

    assert _pending_status(pid) == "queued"
    row = _row(dup)
    assert row.phone is None and row.skip_trace_status == "queued"


async def test_the_sweep_stands_aside_while_another_tick_is_claiming(
    business_user, _dispatcher_on,
):
    from src.workers.skip_trace_dispatcher import (
        _CLAIM_LOCK_KEY,
        _settle_queued_from_known_answers,
    )

    first = _run(business_user.id, skip_on=False, status="done")
    _lead(business_user.id, first, 1)
    again = _run(business_user.id, skip_on=True, status="done")
    pid = _queue(business_user.id, again, _lead(business_user.id, again, 1, dup=True), 1)
    _cache(business_user.id, 1, "2065550203")

    with system_sync_session() as other_tick:
        other_tick.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _CLAIM_LOCK_KEY})
        with system_sync_session() as db:
            assert _settle_queued_from_known_answers(db) is None
        other_tick.rollback()

    assert _pending_status(pid) == "queued"


# ── provenance is written only where an answer is actually copied ────────────


async def test_no_answer_copied_means_no_provenance(business_user, redis_client, _skip_trace_on):
    """The first reuse statement also fills addresses for rows it copies no answer onto.
    Those rows must not be marked 'reused'."""
    first = _run(business_user.id, skip_on=False)
    _lead(business_user.id, first, 1)  # original never traced: nothing to copy
    again = _run(business_user.id, skip_on=False)
    dup = _lead(business_user.id, again, 1, dup=True)

    _enqueue(again, redis_client)

    row = _row(dup)
    assert (row.skip_trace_status, row.skip_trace_source) == ("not_attempted", None)


async def test_a_lead_that_may_not_be_contacted_gets_no_answer_and_no_provenance(
    business_user, redis_client, _skip_trace_on, monkeypatch,
):
    from src.scrapers.enrichment.pierce_atip_owner import OWNER_SOURCE

    monkeypatch.setattr(settings, "PIERCE_CV_OWNER_SKIP_TRACE_ENABLED", False)
    first = _run(business_user.id, skip_on=True)
    _lead(business_user.id, first, 1, status="hit", traced_days_ago=1, phone="2065550210")
    again = _run(business_user.id, skip_on=False)
    dup = _lead(business_user.id, again, 1, dup=True)
    with system_sync_session() as db:
        db.execute(text("UPDATE results SET enrichment_data = CAST(:ed AS json) WHERE id = :r"),
                   {"ed": json.dumps({"source": "tacoma_code_violations",
                                      "owner_source": OWNER_SOURCE}), "r": dup})
        db.commit()

    _enqueue(again, redis_client)

    row = _row(dup)
    assert (row.phone, row.skip_trace_status, row.skip_trace_source) == (
        None, "not_attempted", None)


async def test_the_database_refuses_an_unknown_provenance(business_user):
    import sqlalchemy.exc

    job = _run(business_user.id, skip_on=False)
    rid = _lead(business_user.id, job, 1)
    with system_sync_session() as db:
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            db.execute(text("UPDATE results SET skip_trace_source = 'free' WHERE id = :r"),
                       {"r": rid})
            db.commit()
