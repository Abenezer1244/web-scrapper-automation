"""Durable claim handoff in the skip-trace dispatcher (Codex High, 2026-09-02).

DB-backed, no network: the Tracerfy endpoint is pointed at a non-HTTPS URL so
`submit_batch` raises a DEFINITE configuration rejection before any socket is
opened. That exercises the whole claim path — rows are claimed ('submitting',
committed) before the POST, the definite rejection releases them, the Result
rows are untouched, and rows another tick already claimed are never picked up.
Seeding uses system_sync_session (the worker write path), like test_analytics.
"""
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from src.config import settings
from src.db.session import system_sync_session
from src.workers.skip_trace_dispatcher import dispatch_pending_skip_trace


def _seed_pending(
    user_id: str, *, status: str = "queued", submitted_at=None,
    is_duplicate: bool = False, duplicate_reason: str | None = None,
    enrichment_data: str = "{}", job_status: str = "done",
    billed: bool = False, created_at=None,
) -> tuple[str, str]:
    """scraper_config → job → result (skip_trace_status='queued') → pending row."""
    sc_id, job_id, result_id, pending_id = (str(uuid.uuid4()) for _ in range(4))
    with system_sync_session() as db:
        db.execute(
            text("""
                INSERT INTO scraper_configs
                    (id, user_id, name, county, state, record_type, fields, enrichment,
                     schedule, deliver, skip_trace_enabled, active)
                VALUES (:sc_id, :user_id, 'claim test', 'pierce', 'WA', 'probate',
                        '[]'::json, '[]'::json, '{"frequency":"manual"}'::json,
                        '{"format":"csv","emails":[]}'::json, true, true)
            """),
            {"sc_id": sc_id, "user_id": user_id},
        )
        db.execute(
            text("""
                INSERT INTO jobs (id, user_id, scraper_config_id, status, trigger,
                                  page_current, page_total, record_count, retry_count,
                                  billing_applied_at, created_at)
                VALUES (:job_id, :user_id, :sc_id, :job_status, 'manual', 0, 0, 0, 0,
                        CASE WHEN :billed THEN now() END,
                        COALESCE(CAST(:created_at AS timestamptz), now()))
            """),
            {"job_id": job_id, "user_id": user_id, "sc_id": sc_id, "job_status": job_status,
             "billed": billed, "created_at": created_at},
        )
        db.execute(
            text("""
                INSERT INTO results (id, job_id, user_id, is_duplicate, duplicate_reason,
                                     skip_trace_status, party_name, property_address,
                                     enrichment_data, created_at)
                VALUES (:rid, :job_id, :user_id, :dup, :reason, 'queued',
                        'SAARENAS AVELINO G', '5128 BEVERLY AVE NE',
                        CAST(:ed AS json), now())
            """),
            {"rid": result_id, "job_id": job_id, "user_id": user_id,
             "dup": is_duplicate, "reason": duplicate_reason, "ed": enrichment_data},
        )
        db.execute(
            text("""
                INSERT INTO pending_skip_trace_rows
                    (id, job_id, result_id, user_id, property_address, city, state,
                     trace_type, status, enqueued_at, submitted_at)
                VALUES (:pid, :job_id, :rid, :user_id, '5128 BEVERLY AVE NE', 'TACOMA', 'WA',
                        'advanced', :status, now(), :submitted_at)
            """),
            {"pid": pending_id, "job_id": job_id, "rid": result_id, "user_id": user_id,
             "status": status, "submitted_at": submitted_at},
        )
        db.commit()
    return pending_id, result_id


def _pending_state(pending_id: str) -> tuple[str, object]:
    with system_sync_session() as db:
        return db.execute(
            text("SELECT status, submitted_at FROM pending_skip_trace_rows WHERE id = :id"),
            {"id": pending_id},
        ).one()


def _result_status(result_id: str) -> str:
    with system_sync_session() as db:
        return db.execute(
            text("SELECT skip_trace_status FROM results WHERE id = :id"), {"id": result_id}
        ).scalar_one()


@pytest.fixture
def _dispatcher_enabled(monkeypatch):
    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "test-token-not-real")
    # Non-HTTPS → submit_batch raises "must use HTTPS" BEFORE opening a socket:
    # a definite provider_error, no network, no credits.
    monkeypatch.setattr(settings, "TRACERFY_API_BASE_URL", "http://tracerfy.invalid")
    monkeypatch.setattr(settings, "OPS_ALERT_EMAIL", "")  # alerts stay no-op


@pytest.mark.asyncio
async def test_definite_rejection_releases_claim_to_errored(business_user, _dispatcher_enabled):
    pending_id, result_id = _seed_pending(business_user.id)

    out = dispatch_pending_skip_trace()

    assert out["submitted_batches"] == 0
    assert any("HTTPS" in e for e in out["errors"])
    status, submitted_at = _pending_state(pending_id)
    # Claimed ('submitting', committed) and then RELEASED by the definite failure —
    # never left mid-claim, never re-queued for a definite rejection.
    assert status == "errored"
    assert submitted_at is None
    # A definite rejection surfaces on the lead too — "Error", not "Processing"
    # forever (Codex round 3).
    assert _result_status(result_id) == "errored"


@pytest.mark.asyncio
async def test_row_claimed_by_another_tick_is_not_resubmitted(business_user, _dispatcher_enabled):
    stale = datetime.now(UTC) - timedelta(hours=2)
    pending_id, result_id = _seed_pending(business_user.id, status="submitting", submitted_at=stale)

    out = dispatch_pending_skip_trace()

    # Nothing to submit: 'submitting' rows belong to another tick / a crashed
    # handoff and must never be paid for again. The stale-claim check ran
    # (OPS_ALERT_EMAIL empty → no-op) without touching the row.
    assert out == {"submitted_batches": 0, "submitted_rows": 0, "errors": []}
    status, submitted_at = _pending_state(pending_id)
    assert status == "submitting"
    assert submitted_at is not None
    assert _result_status(result_id) == "queued"


@pytest.mark.asyncio
async def test_release_cannot_clobber_a_newer_claim_on_the_same_rows(
    business_user, _dispatcher_enabled
):
    """The double-pay race Codex found (2026-09-07).

    The reconciler reads stale claims WITHOUT a lock. Between its read and its
    write, another tick can release those rows, the dispatcher can re-claim them
    under a NEW submitted_at, and POST them. They are legitimately 'submitting'
    again at that instant, so a release guarded only on status would free a batch
    that is already in flight -- and the next tick would submit and pay for it a
    second time.

    Releasing therefore pins the exact claim it read via submitted_at.
    """
    from src.workers.skip_trace_dispatcher import _Claim, _release_claim

    stale = datetime.now(UTC) - timedelta(hours=2)
    pending_id, result_id = _seed_pending(
        business_user.id, status="submitting", submitted_at=stale)

    # A newer claim lands on the same row while the reconciler holds its snapshot.
    newer = datetime.now(UTC)
    with system_sync_session() as db:
        db.execute(
            text("UPDATE pending_skip_trace_rows SET submitted_at = :t WHERE id = :i"),
            {"t": newer, "i": pending_id},
        )
        db.commit()

    # The reconciler now tries to release using the OLD claim time it read.
    with system_sync_session() as db:
        _release_claim(
            db,
            [_Claim(pending_id, result_id, "j", business_user.id)],
            "queued",
            claim_time=stale,
        )
        db.commit()

    status, submitted_at = _pending_state(pending_id)
    assert status == "submitting", "the in-flight claim was released — double-pay"
    assert submitted_at is not None

    # ...and pinning the CURRENT claim time still releases normally.
    with system_sync_session() as db:
        _release_claim(
            db,
            [_Claim(pending_id, result_id, "j", business_user.id)],
            "queued",
            claim_time=newer,
        )
        db.commit()
    assert _pending_state(pending_id)[0] == "queued"


# ── A lead that stopped being delivered is withdrawn, never bought ───────────


@pytest.mark.asyncio
async def test_a_row_whose_lead_became_a_duplicate_is_withdrawn_not_submitted(
    business_user, _dispatcher_enabled
):
    """Queued while it was the survivor, flagged duplicate before the tick (a
    watchdog re-run repeating the survivor election). Nothing is claimed or
    POSTed: the non-HTTPS endpoint would have produced an error if it had been."""
    pending_id, result_id = _seed_pending(business_user.id, is_duplicate=True, duplicate_reason="same_run")

    out = dispatch_pending_skip_trace()

    assert out == {"submitted_batches": 0, "submitted_rows": 0, "errors": []}
    status, submitted_at = _pending_state(pending_id)
    assert status == "cancelled"
    assert submitted_at is None
    # Back to not_attempted, not errored: nothing failed, and a lead that becomes
    # deliverable again must be enqueueable again.
    assert _result_status(result_id) == "not_attempted"


@pytest.mark.asyncio
async def test_a_row_the_plan_cap_excluded_is_withdrawn_not_submitted(
    business_user, _dispatcher_enabled
):
    pending_id, result_id = _seed_pending(
        business_user.id, enrichment_data='{"delivery_excluded_reason": "over_quota"}')

    out = dispatch_pending_skip_trace()

    assert out == {"submitted_batches": 0, "submitted_rows": 0, "errors": []}
    assert _pending_state(pending_id)[0] == "cancelled"
    assert _result_status(result_id) == "not_attempted"


@pytest.mark.asyncio
async def test_withdrawal_does_not_hold_back_the_deliverable_rows_beside_it(
    business_user, _dispatcher_enabled
):
    """One FIFO head, one withdrawn row and one live row. The live row still goes
    through the claim path (and is released as errored by the fake endpoint's
    definite rejection); the withdrawn one is cancelled in the same tick."""
    dup_pending, dup_result = _seed_pending(business_user.id, is_duplicate=True, duplicate_reason="same_run")
    live_pending, live_result = _seed_pending(business_user.id)

    out = dispatch_pending_skip_trace()

    assert any("HTTPS" in e for e in out["errors"])
    assert _pending_state(dup_pending)[0] == "cancelled"
    assert _result_status(dup_result) == "not_attempted"
    assert _pending_state(live_pending)[0] == "errored"
    assert _result_status(live_result) == "errored"


@pytest.mark.asyncio
async def test_a_lead_being_updated_right_now_is_left_for_the_next_tick(
    business_user, _dispatcher_enabled
):
    """The race Codex found in the diff review. A re-election is flagging the row
    duplicate in a transaction still open when the dispatcher reads it. The old
    committed value says "deliverable", so reading it would buy the lookup. The
    dispatcher skips the locked row instead (never waiting on it, which would
    invert lock order against a purge cascade) and decides on the next tick."""
    pending_id, result_id = _seed_pending(business_user.id)

    with system_sync_session() as writer:
        writer.execute(
            text("UPDATE results SET is_duplicate = true, duplicate_reason = 'same_run' "
                 "WHERE id = :id"), {"id": result_id}
        )  # uncommitted: holds the row lock

        out = dispatch_pending_skip_trace()

        assert out == {"submitted_batches": 0, "submitted_rows": 0, "errors": []}
        assert _pending_state(pending_id)[0] == "queued"
        writer.commit()

    out = dispatch_pending_skip_trace()

    assert out == {"submitted_batches": 0, "submitted_rows": 0, "errors": []}
    assert _pending_state(pending_id)[0] == "cancelled"
    assert _result_status(result_id) == "not_attempted"


@pytest.mark.asyncio
@pytest.mark.parametrize("job_status", ["failed", "cancelled"])
async def test_a_job_that_delivered_nothing_never_buys_its_queued_lookups(
    business_user, _dispatcher_enabled, job_status
):
    """Rows are queued just before the enriched re-export and billing. If the job
    then fails (an upload that never lands) the file was never delivered, so its
    queued lookups are withdrawn, not paid for."""
    pending_id, result_id = _seed_pending(business_user.id, job_status=job_status)

    out = dispatch_pending_skip_trace()

    assert out == {"submitted_batches": 0, "submitted_rows": 0, "errors": []}
    assert _pending_state(pending_id)[0] == "cancelled"
    assert _result_status(result_id) == "not_attempted"


@pytest.mark.asyncio
@pytest.mark.parametrize("job_status", ["failed", "cancelled"])
async def test_a_job_that_billed_before_it_was_marked_terminal_still_buys_its_lookups(
    business_user, _dispatcher_enabled, job_status
):
    """Billing and the done-CAS commit together, so a billed job delivered its file
    (the download is gated on export_key, not status) and the customer paid. A
    status written over 'done' afterwards (a cancel racing completion) must not
    withdraw what they bought (Codex review round 6). The row goes through the
    claim path; the fake endpoint's definite rejection marks it errored."""
    pending_id, result_id = _seed_pending(business_user.id, job_status=job_status, billed=True)

    out = dispatch_pending_skip_trace()

    assert any("HTTPS" in e for e in out["errors"])
    assert _pending_state(pending_id)[0] == "errored"
    assert _result_status(result_id) == "errored"


@pytest.mark.asyncio
@pytest.mark.parametrize("job_status", ["failed", "cancelled"])
async def test_a_job_from_before_billing_was_stamped_still_buys_its_lookups(
    business_user, _dispatcher_enabled, job_status
):
    """Before migration 063 a job could charge and deliver and still end failed or
    cancelled with no stamp, so NULL proves nothing: its lookups are bought, as
    they were before the withdrawal existed (Codex review round 8)."""
    from src.workers.tasks_helpers.dedup import BILLING_STAMP_RELIABLE_SINCE

    pending_id, result_id = _seed_pending(
        business_user.id, job_status=job_status,
        created_at=BILLING_STAMP_RELIABLE_SINCE - timedelta(days=1))

    out = dispatch_pending_skip_trace()

    assert any("HTTPS" in e for e in out["errors"])
    assert _pending_state(pending_id)[0] == "errored"
    assert _result_status(result_id) == "errored"


@pytest.mark.asyncio
async def test_a_job_still_running_keeps_its_rows_queued_until_it_finishes(
    business_user, _dispatcher_enabled
):
    pending_id, result_id = _seed_pending(business_user.id, job_status="enriching")

    out = dispatch_pending_skip_trace()

    assert out == {"submitted_batches": 0, "submitted_rows": 0, "errors": []}
    assert _pending_state(pending_id)[0] == "queued"
    assert _result_status(result_id) == "queued"
