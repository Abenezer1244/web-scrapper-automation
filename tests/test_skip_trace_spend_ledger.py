"""The skip-trace spend ledger: what a claimed row's `submitted_at` means (1b-1b-i).

`pending_skip_trace_rows.submitted_at` is the instant a row was CLAIMED for a
Tracerfy POST. The daily spend cap counts it inside a rolling 24h window, and the
per-account cap in 1b-1b-ii will too, so it must stay the claim time for as long
as the row is spent, and must disappear only when a release proves nothing was
charged. These tests pin that ledger down on a real database. Only the two
Tracerfy HTTP seams (`submit_batch`, `fetch_queues`) are replaced: nothing leaves
the process and no credits are spent.

Covered here, not elsewhere:
  C1  an accepted batch, and one adopted days later, keep the claim time;
  C2  bookkeeping never lands on a row that has since been claimed again;
      a release only touches the leads it actually released;
  the dialer ages an adopted row by its batch, not by the claim;
  C12 every provider outcome leaves the ledger in the state the cap relies on.
Existing coverage deliberately not repeated: test_skip_trace_dispatcher_claim
(definite rejection, a claim another tick holds), test_skip_trace_reconciliation
(the pure queue-matching rules), test_tracerfy_ingest (ingest keeps the stamp).
"""
import random
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from src.config import settings
from src.db.models import Job
from src.db.session import system_sync_session
from src.scrapers.enrichment import skip_trace
from src.scrapers.enrichment.skip_trace import TracerfyError
from src.workers import skip_trace_dispatcher as dispatcher
from src.workers.scheduler_helpers.dialer import skip_trace_unsettled


def _seed(user_id: str, n: int = 1, *, status: str = "queued", submitted_at=None,
          trace_type: str = "advanced", queue_id: int | None = None) -> list[dict]:
    """One done, billed job holding `n` leads, each with one pending row.

    Distinct addresses, so the dispatcher's one-answer-per-address hold never
    folds them together; enqueued a second apart, so FIFO order is the seed order.
    """
    sc_id, job_id = str(uuid.uuid4()), str(uuid.uuid4())
    rows = []
    with system_sync_session() as db:
        db.execute(text("""
            INSERT INTO scraper_configs (id, user_id, name, county, state, record_type,
                fields, enrichment, schedule, deliver, skip_trace_enabled, active)
            VALUES (:sc, :u, 'ledger test', 'pierce', 'WA', 'probate', '[]'::json,
                    '[]'::json, '{"frequency":"manual"}'::json,
                    '{"format":"csv","emails":[]}'::json, true, true)
        """), {"sc": sc_id, "u": user_id})
        db.execute(text("""
            INSERT INTO jobs (id, user_id, scraper_config_id, status, trigger, page_current,
                              page_total, record_count, retry_count, billing_applied_at)
            VALUES (:j, :u, :sc, 'done', 'manual', 0, 0, 0, 0, now())
        """), {"j": job_id, "u": user_id, "sc": sc_id})
        for i in range(n):
            rid, pid = str(uuid.uuid4()), str(uuid.uuid4())
            addr = f"{100 + i} LEDGER ST"
            db.execute(text("""
                INSERT INTO results (id, job_id, user_id, is_duplicate, skip_trace_status,
                                     party_name, property_address, enrichment_data, created_at)
                VALUES (:r, :j, :u, false, 'queued', 'LEDGER TEST OWNER', :a, '{}'::json, now())
            """), {"r": rid, "j": job_id, "u": user_id, "a": addr})
            db.execute(text("""
                INSERT INTO pending_skip_trace_rows
                    (id, job_id, result_id, user_id, property_address, city, state,
                     trace_type, status, enqueued_at, submitted_at, tracerfy_queue_id)
                VALUES (:p, :j, :r, :u, :a, 'TACOMA', 'WA', :t, :s,
                        now() - make_interval(secs => :age), :sub, :q)
            """), {"p": pid, "j": job_id, "r": rid, "u": user_id, "a": addr, "t": trace_type,
                   "s": status, "age": n - i, "sub": submitted_at, "q": queue_id})
            rows.append({"pending": pid, "result": rid, "job": job_id, "user": user_id})
        db.commit()
    return rows


def _row(pending_id: str):
    with system_sync_session() as db:
        return db.execute(text(
            "SELECT status, submitted_at, tracerfy_queue_id FROM pending_skip_trace_rows "
            "WHERE id = :i"), {"i": pending_id}).one()


def _result_status(result_id: str) -> str:
    with system_sync_session() as db:
        return db.execute(text("SELECT skip_trace_status FROM results WHERE id = :i"),
                          {"i": result_id}).scalar_one()


def _claim(r: dict):
    return dispatcher._Claim(r["pending"], r["result"], r["job"], r["user"])


def _queue_id() -> int:
    return random.randint(10**8, 2 * 10**9)


@pytest.fixture
def tracerfy(monkeypatch):
    """Dispatcher on, alerts no-op; each test decides what Tracerfy answers."""
    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "test-token-not-real")
    monkeypatch.setattr(settings, "SKIP_TRACE_MAX_BATCHES_PER_TICK", 1)
    monkeypatch.setattr(settings, "OPS_ALERT_EMAIL", "")
    calls: list[list[dict]] = []

    def answer(*outcomes):
        """Each POST gets the next outcome: an exception to raise, or 'accept'."""
        queue = list(outcomes)

        def _submit(rows, trace_type="normal", api_token=None):
            calls.append(rows)
            outcome = queue.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return {"queue_id": _queue_id(), "rows_uploaded": len(rows), "credits_deducted": 0}

        monkeypatch.setattr(skip_trace, "submit_batch", _submit)
        return calls

    return answer


# ── C1: the claim time survives acceptance, and adoption days later ──────────


async def test_an_accepted_batch_keeps_its_claim_time(starter_user, tracerfy, monkeypatch):
    (r,) = _seed(starter_user.id)
    seen_at_post = {}

    def _submit(rows, trace_type="normal", api_token=None):
        seen_at_post["row"] = _row(r["pending"])  # the claim is committed before the POST
        return {"queue_id": _queue_id(), "rows_uploaded": 1, "credits_deducted": 2}

    tracerfy()
    monkeypatch.setattr(skip_trace, "submit_batch", _submit)

    dispatcher.dispatch_pending_skip_trace()

    status, submitted_at, queue_id = _row(r["pending"])
    assert seen_at_post["row"].status == "submitting"
    assert status == "submitted" and queue_id is not None
    # The bookkeeping clock used to overwrite this. It must be the CLAIM time.
    assert submitted_at == seen_at_post["row"].submitted_at


async def test_adoption_keeps_a_claim_time_from_days_ago(starter_user, tracerfy, monkeypatch):
    claimed_at = (datetime.now(UTC) - timedelta(days=3)).replace(microsecond=0)
    (r,) = _seed(starter_user.id, status="submitting", submitted_at=claimed_at)
    qid = _queue_id()
    monkeypatch.setattr(skip_trace, "fetch_queues", lambda *a, **k: [{
        "id": qid, "trace_type": "advanced", "queue_type": "api", "pending": False,
        "created_at": (claimed_at + timedelta(seconds=5)).isoformat(),
        "rows_uploaded": 1, "credits_deducted": 2, "download_url": None,
    }])

    with system_sync_session() as db:
        summary = dispatcher._reconcile_stale_claims(db)

    assert summary["adopted"] == 1
    status, submitted_at, queue_id = _row(r["pending"])
    assert (status, queue_id) == ("submitted", qid)
    # Restamped to "now", this old spend would count against TODAY's cap.
    assert submitted_at == claimed_at


async def test_the_bookkeeping_retry_keeps_the_claim_time(starter_user):
    claimed_at = datetime.now(UTC) - timedelta(minutes=2)
    (r,) = _seed(starter_user.id, status="submitting", submitted_at=claimed_at)
    qid = _queue_id()

    # The retry runs on its own fresh session, after the first commit failed.
    assert dispatcher._persist_submission_retry(
        qid, [_claim(r)], "advanced", {"queue_id": qid, "rows_uploaded": 1},
        claim_time=claimed_at,
    )

    status, submitted_at, queue_id = _row(r["pending"])
    assert (status, queue_id, submitted_at) == ("submitted", qid, claimed_at)


def _stale_claim_queue(qid: int, claimed_at: datetime) -> dict:
    return {"id": qid, "trace_type": "advanced", "queue_type": "api", "pending": False,
            "created_at": (claimed_at + timedelta(seconds=5)).isoformat(),
            "rows_uploaded": 1, "credits_deducted": 2, "download_url": None}


async def test_the_reconciler_releases_a_claim_tracerfy_never_saw(starter_user, monkeypatch):
    claimed_at = datetime.now(UTC) - timedelta(hours=2)
    (r,) = _seed(starter_user.id, status="submitting", submitted_at=claimed_at)
    monkeypatch.setattr(skip_trace, "fetch_queues", lambda *a, **k: [])

    with system_sync_session() as db:
        summary = dispatcher._reconcile_stale_claims(db)

    assert summary["released"] == 1
    # Never charged, so it stops counting and goes back in line.
    assert _row(r["pending"])[:2] == ("queued", None)


async def test_a_queue_two_claims_could_own_is_adopted_by_neither(starter_user, monkeypatch):
    claimed_at = datetime.now(UTC) - timedelta(hours=2)
    (first,) = _seed(starter_user.id, status="submitting", submitted_at=claimed_at)
    (second,) = _seed(starter_user.id, status="submitting",
                      submitted_at=claimed_at + timedelta(seconds=1))
    qid = _queue_id()
    monkeypatch.setattr(skip_trace, "fetch_queues",
                        lambda *a, **k: [_stale_claim_queue(qid, claimed_at)])

    with system_sync_session() as db:
        assert dispatcher.contested_queue_ids(db, [_stale_claim_queue(qid, claimed_at)],
                                              set()) == {qid}
        summary = dispatcher._reconcile_stale_claims(db)

    assert summary["adopted"] == 0
    for r in (first, second):
        assert _row(r["pending"])[0] == "submitting"


# ── C2: bookkeeping and releases stay on their own claim ─────────────────────


async def test_bookkeeping_never_lands_on_a_newer_claim(starter_user, caplog):
    old_claim = datetime.now(UTC) - timedelta(minutes=40)
    new_claim = datetime.now(UTC) - timedelta(minutes=1)
    # The row was released after the old POST and has since been claimed again.
    (r,) = _seed(starter_user.id, status="submitting", submitted_at=new_claim)
    qid = _queue_id()

    with system_sync_session() as db:
        dispatcher._persist_submission(
            db, qid, [_claim(r)], "advanced", {"queue_id": qid, "rows_uploaded": 1},
            claim_time=old_claim,
        )

    status, submitted_at, queue_id = _row(r["pending"])
    assert (status, queue_id) == ("submitting", None)
    assert submitted_at == new_claim
    assert _result_status(r["result"]) == "queued"
    with system_sync_session() as db:
        # Tracerfy charged for the old batch, so its queue is still recorded.
        assert db.execute(text("SELECT count(*) FROM skip_trace_queues "
                               "WHERE tracerfy_queue_id = :q"), {"q": qid}).scalar_one() == 1
    assert "possible double purchase" in caplog.text


async def test_a_release_only_touches_the_leads_it_released(starter_user):
    claim = datetime.now(UTC) - timedelta(minutes=40)
    mine, moved_on = _seed(starter_user.id, 2, status="submitting", submitted_at=claim)
    with system_sync_session() as db:
        db.execute(text("UPDATE pending_skip_trace_rows SET submitted_at = now() "
                        "WHERE id = :i"), {"i": moved_on["pending"]})
        db.commit()
        released = dispatcher._release_claim(
            db, [_claim(mine), _claim(moved_on)], "errored", claim_time=claim,
        )

    assert [str(x.id) for x in released] == [mine["pending"]]
    assert _result_status(mine["result"]) == "errored"
    # The re-claimed lead is in flight again; marking it "Error" was the old bug.
    assert _result_status(moved_on["result"]) == "queued"


# ── The dialer ages an adopted row by its batch ──────────────────────────────


def _queue_row(user_id: str, job_id: str, qid: int, submitted_at) -> None:
    with system_sync_session() as db:
        db.execute(text("""
            INSERT INTO skip_trace_queues (id, tracerfy_queue_id, job_id, user_id, trace_type,
                                           status, rows_uploaded, credits_deducted, submitted_at)
            VALUES (:id, :q, :j, :u, 'advanced', 'pending', 1, 2, :s)
        """), {"id": str(uuid.uuid4()), "q": qid, "j": job_id, "u": user_id, "s": submitted_at})
        db.commit()


def _unsettled(job_id: str) -> bool:
    with system_sync_session() as db:
        return db.execute(
            select(Job.id).where(Job.id == job_id, skip_trace_unsettled(datetime.now(UTC)))
        ).first() is not None


@pytest.mark.parametrize(("row_age_h", "queue_age_h", "has_queue_row", "expect_unsettled"), [
    (72, 1, True, True),     # adopted: claimed 3 days ago, batch recorded an hour ago
    (72, 13, True, False),   # the batch itself is past the 12h cutoff: wedged, let it go
    # Names a queue that has no row: may be paid and coming. 72h old, so only the
    # missing-queue rule (not the age) can keep it unsettled.
    (72, None, False, True),
])
async def test_the_dialer_ages_a_submitted_row_by_its_batch(
    starter_user, row_age_h, queue_age_h, has_queue_row, expect_unsettled,
):
    now = datetime.now(UTC)
    qid = _queue_id()
    (r,) = _seed(starter_user.id, status="submitted",
                 submitted_at=now - timedelta(hours=row_age_h), queue_id=qid)
    if has_queue_row:
        _queue_row(starter_user.id, r["job"], qid, now - timedelta(hours=queue_age_h))
    assert _unsettled(r["job"]) is expect_unsettled


async def test_a_job_held_by_a_missing_queue_is_not_held_silently(
    starter_user, caplog, monkeypatch,
):
    from src.workers import ops_alerts
    from src.workers.scheduler_helpers.dialer import _alert_rows_naming_missing_queues

    # send_ops_alert is the email-vendor boundary (Resend): capture, never send.
    sent: list[tuple] = []
    monkeypatch.setattr(ops_alerts, "send_ops_alert", lambda *a: sent.append(a) or True)
    _seed(starter_user.id, status="submitted",
          submitted_at=datetime.now(UTC) - timedelta(hours=72), queue_id=_queue_id())

    with system_sync_session() as db:
        _alert_rows_naming_missing_queues(db)

    assert "name a Tracerfy queue with no skip_trace_queues row" in caplog.text
    assert len(sent) == 1
    kind, key, subject, body = sent[0]
    assert (kind, key) == ("dialer", "missing_skip_trace_queue")
    assert "Dialer pushes blocked" in subject and "1 pending_skip_trace_rows" in body


async def test_a_failing_missing_queue_probe_does_not_stop_the_sweep(starter_user, monkeypatch):
    from src.workers.scheduler_helpers import dialer

    def _boom(db):
        db.execute(text("SELECT no_such_column FROM pending_skip_trace_rows"))

    from sqlalchemy import event

    from src.db.session import sync_engine

    monkeypatch.setattr(dialer, "_alert_rows_naming_missing_queues", _boom)
    # Observe the real SQL (no mock): the sweep's candidate query must run AFTER
    # the probe failed, on the rolled-back session. Without this a sweep that
    # caught the failure and then returned early would still pass.
    statements: list[str] = []

    def _record(conn, cursor, statement, *args):
        statements.append(statement)

    event.listen(sync_engine, "before_cursor_execute", _record)
    try:
        dialer._dialer_push_sweep_impl()
    finally:
        event.remove(sync_engine, "before_cursor_execute", _record)

    probe = next(i for i, s in enumerate(statements) if "no_such_column" in s)
    assert any("dialer_pushed_at" in s and "scraper_configs" in s
               for s in statements[probe + 1:]), "the sweep stopped at the probe"


@pytest.mark.parametrize(("row_age_h", "expect_unsettled"), [(1, True), (13, False)])
async def test_a_row_without_a_queue_id_ages_by_its_own_time(
    starter_user, row_age_h, expect_unsettled,
):
    (r,) = _seed(starter_user.id, status="submitted",
                 submitted_at=datetime.now(UTC) - timedelta(hours=row_age_h))
    assert _unsettled(r["job"]) is expect_unsettled


# ── C12: every provider outcome leaves the ledger as the cap needs it ────────


@pytest.mark.parametrize(("error", "expect_status", "expect_spent"), [
    # Tracerfy may have accepted and charged: the claim stays, and it counts.
    ("Network error: read timed out", "submitting", True),
    # Never delivered or refused before charging: released, and it does not count.
    ("Tracerfy returned 429: rate limit", "queued", False),
    ("Tracerfy returned 503: unavailable", "queued", False),
    ("Connection error: connection refused", "queued", False),
    ("Tracerfy returned 402: You need 999 more credits to complete this request",
     "queued", False),
    ("Tracerfy returned 400: bad batch", "errored", False),
])
async def test_a_provider_outcome_leaves_the_ledger_right(
    starter_user, tracerfy, error, expect_status, expect_spent,
):
    (r,) = _seed(starter_user.id)
    calls = tracerfy(TracerfyError(error))

    dispatcher.dispatch_pending_skip_trace()

    # The POST really happened: a 'queued' row after a no-op tick would pass too.
    assert len(calls) == 1
    status, submitted_at, _q = _row(r["pending"])
    assert status == expect_status
    assert (submitted_at is not None) is expect_spent


async def test_a_partial_402_submits_the_affordable_head_under_its_claim(starter_user, tracerfy):
    # Three advanced rows = 6 credits; "need 2 more" leaves 4 = two rows affordable.
    a, b, c = _seed(starter_user.id, 3)
    calls = tracerfy(TracerfyError("Tracerfy returned 402: You need 2 more credits"), "accept")

    dispatcher.dispatch_pending_skip_trace()

    assert [len(x) for x in calls] == [3, 2]
    sent = [_row(x["pending"]) for x in (a, b)]
    assert all(s.status == "submitted" and s.tracerfy_queue_id is not None for s in sent)
    # Both went out under the ONE claim they were taken in.
    assert sent[0].submitted_at == sent[1].submitted_at is not None
    assert _row(c["pending"])[:2] == ("queued", None)


async def test_an_unknown_partial_resubmit_keeps_its_claim(starter_user, tracerfy):
    a, b, c = _seed(starter_user.id, 3)
    tracerfy(TracerfyError("Tracerfy returned 402: You need 2 more credits"),
             TracerfyError("Network error: read timed out"))

    dispatcher.dispatch_pending_skip_trace()

    for x in (a, b):
        status, submitted_at, _q = _row(x["pending"])
        assert status == "submitting" and submitted_at is not None
    assert _row(c["pending"])[:2] == ("queued", None)
