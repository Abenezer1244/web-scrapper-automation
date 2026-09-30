"""The done-time breakdown as finalization freezes it.

Drives the production helper, `finalize_billing_and_done` (2c-bis moved billing and
the done-CAS there, fenced on the attempt token), on real rows: the bill and the
frozen breakdown come from one partition, only the attempt that owns a fresh bill
freezes, and a re-run billed earlier leaves an existing snapshot alone. T5b pins
the remaining wiring in run_scrape_job (the completion line), which needs Redis and
a browser to reach behaviourally.
"""
import inspect
import uuid

import pytest
from sqlalchemy import text

from src.api.run_breakdown import SNAPSHOT_COLUMNS, decide_snapshot, read_partition
from src.db.models import Job, Result, ScraperConfig, User
from src.db.session import SyncSessionLocal
from src.workers.scheduler_helpers.health import _Candidate, _recovery_cas
from src.workers.tasks_helpers.finalize import FinalizeKind, finalize_billing_and_done
from src.workers.tasks_helpers.status import AttemptToken, claim_attempt

ADDR = "5006 61ST STREET CT E"
FROZEN_3_NEW = {"breakdown_dropped_before_save": 0, "breakdown_no_address": 0,
                "breakdown_same_run_merged": 0, "breakdown_already_delivered": 0,
                "breakdown_over_quota": 0, "breakdown_new": 3}


def _live(source: str) -> str:
    return "\n".join(ln for ln in source.splitlines() if not ln.strip().startswith("#"))


def test_finalization_bills_and_freezes_from_one_partition():
    body = _live(inspect.getsource(finalize_billing_and_done))
    assert "_partition = read_partition(db, job_id, job.user_id)" in body
    assert "billable_count = _partition.new" in body
    assert "attempt_token=attempt_token, partition=_partition," in body
    assert "**(_snapshot or {})," in body, "the done-CAS no longer writes the breakdown"
    # The old standalone billing count must not come back beside the partition:
    # two statements are two readings that can disagree.
    assert "actionable_sql(" not in body
    # Order: the partition is read before the billing CAS, the decision after it.
    assert body.index("read_partition(db") < body.index("billing_applied_at.is_(None)")
    assert body.index("billing_applied_at.is_(None)") < body.index("decide_snapshot(")


def test_run_scrape_job_logs_completion_from_the_frozen_breakdown():
    from src.workers.tasks import run_scrape_job

    body = _live(inspect.getsource(run_scrape_job.__wrapped__))
    assert "completion_message(display_count, _outcome.frozen)" in body
    assert "duplicates filtered" not in body


# ── real rows through finalize_billing_and_done ──────────────────────────────

@pytest.fixture
def finalizing(starter_user, scraper_config):
    """Attempt A claimed a job and is finalizing: 3 new leads saved, 3 found."""
    job_id = str(uuid.uuid4())
    with SyncSessionLocal() as s:
        s.add(Job(id=job_id, user_id=starter_user.id, scraper_config_id=scraper_config.id,
                  trigger="manual", status="pending"))
        s.flush()
        for _ in range(3):
            s.add(Result(id=str(uuid.uuid4()), job_id=job_id, user_id=starter_user.id,
                         property_address=ADDR, is_duplicate=False))
        s.commit()
        a = claim_attempt(s, job_id)
        s.execute(text("UPDATE jobs SET status = 'enriching', records_found = 3 "
                       "WHERE id = :j"), {"j": job_id})
        s.commit()
    return {"job_id": job_id, "user_id": starter_user.id, "config_id": scraper_config.id,
            "a": a}


def _finalize(f, token, redis_client):
    with SyncSessionLocal() as db:
        job = db.get(Job, f["job_id"])
        return finalize_billing_and_done(
            db, redis_client, job=job, user=db.get(User, job.user_id),
            config=db.get(ScraperConfig, job.scraper_config_id), job_id=f["job_id"],
            attempt_token=token, object_key=f"exports/{job.user_id}/{f['job_id']}/leads.csv",
            boot_user_id=str(job.user_id),
        )


def _row(f) -> Job:
    with SyncSessionLocal() as s:
        return s.get(Job, f["job_id"])


def _snapshot(job: Job) -> dict:
    return {c: getattr(job, c) for c in SNAPSHOT_COLUMNS.values()}


def test_the_owner_bills_and_freezes_the_same_count(finalizing, redis_client):
    out = _finalize(finalizing, finalizing["a"], redis_client)

    assert out.kind is FinalizeKind.DONE and out.display_count == 3
    assert out.frozen == {f: FROZEN_3_NEW[c] for f, c in SNAPSHOT_COLUMNS.items()}
    job = _row(finalizing)
    assert (job.status, job.billed_count, job.record_count) == ("done", 3, 3)
    assert _snapshot(job) == FROZEN_3_NEW


def test_b_reclaims_first_so_stale_a_bills_and_freezes_nothing(finalizing, redis_client):
    a = finalizing["a"]
    with SyncSessionLocal() as b:
        seen = _Candidate(id=finalizing["job_id"], status="enriching",
                          retry_count=a.retry_count, started_at=a.started_at,
                          user_id=str(finalizing["user_id"]),
                          scraper_config_id=str(finalizing["config_id"]))
        assert _recovery_cas(b, seen, status="pending", started_at=None,
                             records_found=None, retry_count=a.retry_count + 1)
        assert claim_attempt(b, finalizing["job_id"]) is not None
        b.commit()

    out = _finalize(finalizing, a, redis_client)

    assert out.kind is FinalizeKind.LOST_OWNERSHIP and out.frozen is None
    job = _row(finalizing)
    assert job.billing_applied_at is None and job.status != "done"
    assert set(_snapshot(job).values()) == {None}


def test_a_retried_run_is_billed_but_freezes_nothing(finalizing, redis_client):
    """records_found is per attempt, the saved rows per job: a retried run's
    difference is not its drop count, so the six are written NULL."""
    with SyncSessionLocal() as s:
        s.execute(text("UPDATE jobs SET retry_count = 1 WHERE id = :j"),
                  {"j": finalizing["job_id"]})
        s.commit()
    token = AttemptToken(finalizing["a"].started_at, 1)

    out = _finalize(finalizing, token, redis_client)

    assert out.kind is FinalizeKind.DONE and out.frozen is None
    job = _row(finalizing)
    assert (job.status, job.billed_count) == ("done", 3)
    assert set(_snapshot(job).values()) == {None}


def test_a_rerun_billed_earlier_keeps_the_existing_snapshot(finalizing, redis_client):
    """The job was billed and frozen by an earlier pass, rows changed since (a
    backfill), and finalization runs again: the done-CAS must not name the columns,
    so the frozen values survive and the charge stays what was billed."""
    with SyncSessionLocal() as s:
        s.execute(text(
            "UPDATE jobs SET billing_applied_at = clock_timestamp(), billed_count = 3, "
            + ", ".join(f"{c} = {v}" for c, v in FROZEN_3_NEW.items())
            + " WHERE id = :jid"), {"jid": finalizing["job_id"]})
        s.add(Result(id=str(uuid.uuid4()), job_id=finalizing["job_id"],
                     user_id=finalizing["user_id"], property_address=ADDR,
                     is_duplicate=False))
        s.commit()
        assert read_partition(s, finalizing["job_id"], finalizing["user_id"]).new == 4

    out = _finalize(finalizing, finalizing["a"], redis_client)

    assert out.kind is FinalizeKind.DONE and out.display_count == 3 and out.frozen is None
    job = _row(finalizing)
    assert job.status == "done" and _snapshot(job) == FROZEN_3_NEW


def test_the_owner_read_is_scoped_to_the_tenant(finalizing, business_user):
    """Another account's id against this job reads no row: not owned, no columns."""
    with SyncSessionLocal() as a:
        partition = read_partition(a, finalizing["job_id"], finalizing["user_id"])
        cols, reason = decide_snapshot(a, job_id=finalizing["job_id"],
                                       user_id=business_user.id, billed_now=True,
                                       attempt_token=finalizing["a"], partition=partition)
        a.rollback()
    assert cols is None and "no longer owns" in reason


def test_a_replacement_with_the_same_started_at_is_not_the_owner(finalizing):
    """The token is the pair: B stamped with A's exact started_at but a higher
    retry_count is not A, so A may not describe B's run."""
    a = finalizing["a"]
    with SyncSessionLocal() as s:
        s.execute(text("UPDATE jobs SET retry_count = :n WHERE id = :j"),
                  {"n": a.retry_count + 1, "j": finalizing["job_id"]})
        s.commit()
    with SyncSessionLocal() as s:
        partition = read_partition(s, finalizing["job_id"], finalizing["user_id"])
        cols, reason = decide_snapshot(s, job_id=finalizing["job_id"],
                                       user_id=finalizing["user_id"], billed_now=True,
                                       attempt_token=a, partition=partition)
        s.rollback()
    assert cols is None and "no longer owns" in reason
