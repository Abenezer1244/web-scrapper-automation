"""The done-time breakdown as run_scrape_job freezes it.

T5b pins the wiring inside run_scrape_job (a behavioural test cannot reach that
code: it sits deep in a task that needs Redis and a browser). T8 drives the
production decision (`decide_snapshot`) under real lock timing with two database
sessions: a stale attempt A finalizing while attempt B re-claims the job.
"""
import inspect
import threading
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from src.api.run_breakdown import SNAPSHOT_COLUMNS, decide_snapshot, read_partition
from src.db.models import Job, Result
from src.db.session import SyncSessionLocal
from src.workers.scheduler_helpers.health import _Candidate, _recovery_cas
from src.workers.tasks_helpers.status import _set_status, claim_job_for_attempt

ADDR = "5006 61ST STREET CT E"


def _live_lines(source: str) -> list[str]:
    return [ln.strip() for ln in source.splitlines() if not ln.strip().startswith("#")]


def test_run_scrape_job_bills_and_freezes_from_one_partition():
    from src.workers.tasks import run_scrape_job

    lines = _live_lines(inspect.getsource(run_scrape_job.__wrapped__))
    body = "\n".join(lines)
    assert "_partition = read_partition(db, job_id, job.user_id)" in lines
    assert "billable_count = _partition.new" in lines
    assert "_snapshot, _snapshot_refused = decide_snapshot(" in lines
    assert "**(_snapshot or {})," in lines, "the done-CAS no longer writes the breakdown"
    # The old standalone billing count must not come back beside the partition:
    # two statements are two readings that can disagree.
    assert "f\"AND {actionable_sql('results')}\"" not in lines
    assert "duplicates filtered" not in body
    # Order: the partition is read before the billing CAS, the decision after it.
    assert body.index("read_partition(db") < body.index("billing_applied_at.is_(None)")
    assert body.index("billing_applied_at.is_(None)") < body.index("decide_snapshot(")


# ── T8: stale attempt A vs replacement attempt B ─────────────────────────────

A_TOKEN = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


@pytest.fixture
def finalizing_job(db, starter_user, scraper_config):
    """A job attempt A is finalizing: 3 new leads saved, 3 found, never retried."""
    job_id = str(uuid.uuid4())
    with SyncSessionLocal() as s:
        s.add(Job(id=job_id, user_id=starter_user.id, scraper_config_id=scraper_config.id,
                  trigger="manual", status="enriching", started_at=A_TOKEN,
                  records_found=3, retry_count=0))
        s.flush()
        for _ in range(3):
            s.add(Result(id=str(uuid.uuid4()), job_id=job_id, user_id=starter_user.id,
                         property_address=ADDR, is_duplicate=False))
        s.commit()
    return job_id, starter_user.id, scraper_config.id


def _requeue_then_claim(job_id, user_id, config_id) -> datetime | None:
    """B: the watchdog's own guarded re-queue (_recovery_cas, on the row as it
    observed it: attempt A, enriching), then the production claim. Its own session."""
    with SyncSessionLocal() as b:
        seen = _Candidate(id=job_id, status="enriching", retry_count=0,
                          started_at=A_TOKEN, user_id=str(user_id),
                          scraper_config_id=str(config_id))
        _recovery_cas(b, seen, status="pending", started_at=None, records_found=None,
                      retry_count=1)
        return claim_job_for_attempt(b, job_id)


def _bill(a, job_id, n) -> int:
    """A's billing CAS: today's statement in run_scrape_job, which has no helper of
    its own yet. 2c-bis moves it into finalize_billing_and_done; on the rebase onto
    2c-bis this test drives that production helper instead."""
    return a.execute(text(
        "UPDATE jobs SET billed_count = :n, billing_applied_at = clock_timestamp() "
        "WHERE id = :jid AND billing_applied_at IS NULL"
    ), {"n": n, "jid": job_id}).rowcount


def test_b_reclaims_first_so_stale_a_freezes_nothing(finalizing_job):
    job_id, user_id, config_id = finalizing_job
    b_token = _requeue_then_claim(job_id, user_id, config_id)
    assert b_token is not None

    with SyncSessionLocal() as a:
        partition = read_partition(a, job_id, user_id)
        billed_now = _bill(a, job_id, partition.new)
        cols, reason = decide_snapshot(a, job_id=job_id, user_id=user_id,
                                       billed_now=bool(billed_now),
                                       attempt_started_at=A_TOKEN, partition=partition)
        a.rollback()

    assert cols is None, "a stale attempt described the replacement attempt's run"
    assert "no longer owns" in reason


def test_a_holds_the_lock_first_so_a_finishes_and_b_does_nothing(finalizing_job):
    job_id, user_id, config_id = finalizing_job
    with SyncSessionLocal() as a:
        partition = read_partition(a, job_id, user_id)
        assert _bill(a, job_id, partition.new) == 1   # A now holds the jobs row lock

        b_result: list = []
        b = threading.Thread(target=lambda: b_result.append(
            _requeue_then_claim(job_id, user_id, config_id)))
        b.start()
        b.join(timeout=2)
        assert b.is_alive(), "B's re-queue should be waiting on A's row lock"

        cols, reason = decide_snapshot(a, job_id=job_id, user_id=user_id, billed_now=True,
                                       attempt_started_at=A_TOKEN, partition=partition)
        assert reason is None
        job = a.get(Job, job_id)
        assert _set_status(a, job, "done", record_count=partition.new, commit=False,
                           **cols)
        a.commit()

    b.join(timeout=15)
    assert not b.is_alive()
    assert b_result == [None], "B claimed a job A had already finished"

    with SyncSessionLocal() as s:
        row = s.get(Job, job_id)
        assert (row.status, row.started_at, row.retry_count) == ("done", A_TOKEN, 0)
        assert {c: getattr(row, c) for c in SNAPSHOT_COLUMNS.values()} == {
            "breakdown_dropped_before_save": 0, "breakdown_no_address": 0,
            "breakdown_same_run_merged": 0, "breakdown_already_delivered": 0,
            "breakdown_over_quota": 0, "breakdown_new": 3,
        }


def test_the_owner_read_is_scoped_to_the_tenant(finalizing_job, business_user):
    """Another account's id against this job reads no row: not owned, no columns."""
    job_id, user_id, _ = finalizing_job
    with SyncSessionLocal() as a:
        partition = read_partition(a, job_id, user_id)
        cols, reason = decide_snapshot(a, job_id=job_id, user_id=business_user.id,
                                       billed_now=True, attempt_started_at=A_TOKEN,
                                       partition=partition)
        a.rollback()
    assert cols is None and "no longer owns" in reason


def test_a_rerun_billed_earlier_keeps_the_existing_snapshot(finalizing_job):
    """Branch 1 on real rows: the job was billed and frozen by an earlier attempt,
    rows changed since (a backfill), and a re-run reaches finalization again. The
    done-CAS must not name the columns, so the frozen values survive."""
    job_id, user_id, _ = finalizing_job
    frozen = {"breakdown_dropped_before_save": 0, "breakdown_no_address": 0,
              "breakdown_same_run_merged": 0, "breakdown_already_delivered": 0,
              "breakdown_over_quota": 0, "breakdown_new": 3}
    with SyncSessionLocal() as s:
        s.execute(text(
            "UPDATE jobs SET billing_applied_at = clock_timestamp(), billed_count = 3, "
            + ", ".join(f"{c} = {v}" for c, v in frozen.items())
            + " WHERE id = :jid"), {"jid": job_id})
        s.add(Result(id=str(uuid.uuid4()), job_id=job_id, user_id=user_id,
                     property_address=ADDR, is_duplicate=False))
        s.commit()

    with SyncSessionLocal() as a:
        partition = read_partition(a, job_id, user_id)
        assert partition.new == 4                      # the live view moved
        billed_now = _bill(a, job_id, partition.new)
        assert billed_now == 0
        cols, reason = decide_snapshot(a, job_id=job_id, user_id=user_id,
                                       billed_now=False, attempt_started_at=A_TOKEN,
                                       partition=partition)
        assert cols is None and "earlier attempt" in reason
        job = a.get(Job, job_id)
        assert _set_status(a, job, "done", record_count=3, commit=False, **(cols or {}))
        a.commit()

    with SyncSessionLocal() as s:
        row = s.get(Job, job_id)
        assert row.status == "done"
        assert {c: getattr(row, c) for c in frozen} == frozen
