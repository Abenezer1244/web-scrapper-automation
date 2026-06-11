"""Track A phases 4-5: lease, delivery idempotency, force-finalize, recovery sweep.

DB-backed (real Postgres). Enqueue side effects (.delay -> broker) are tolerated
hermetically — the assertions are all on committed DB state.
"""
import uuid
from datetime import UTC, datetime, timedelta

from kombu.exceptions import OperationalError
from sqlalchemy import update
from sqlalchemy.orm import Session

from src.api.auth import hash_password
from src.db.models import BatchRun, Job, ScraperBatch, ScraperConfig, User
from src.db.session import SyncSessionLocal


def _user(db: Session) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=f"batch_rec_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
        password_hash=hash_password("TestPass123!"),
        plan="pro",
        records_used=0,
        records_limit=-1,
    )
    db.add(user)
    db.flush()
    return user


def _config(db: Session, user_id: str, batch_id: str) -> ScraperConfig:
    cfg = ScraperConfig(
        id=str(uuid.uuid4()),
        user_id=user_id,
        batch_id=batch_id,
        name="child",
        county="pierce",
        state="WA",
        record_type="probate",
        fields=[],
        enrichment=[],
        schedule={},
        deliver={},
    )
    db.add(cfg)
    db.flush()
    return cfg


def _batch(db: Session, user_id: str) -> ScraperBatch:
    b = ScraperBatch(
        id=str(uuid.uuid4()),
        user_id=user_id,
        name="Rec Test",
        state="WA",
        fields=[],
        enrichment=[],
        schedule={},
        deliver={},
        status="active",
    )
    db.add(b)
    db.flush()
    return b


# ── Phase 4: force-finalize ────────────────────────────────────────────────────

def test_finalize_forced_marks_nonterminal_children_failed():
    """A forced finalize treats a still-non-terminal child as failed and finalizes
    the run as 'partial' (one done, one timed out) instead of stranding it."""
    from src.workers.batch_export import finalize_batch_run

    with SyncSessionLocal() as db:
        user = _user(db)
        batch = _batch(db, user.id)
        c_done = _config(db, user.id, batch.id)
        c_stuck = _config(db, user.id, batch.id)
        j_done = Job(id=str(uuid.uuid4()), user_id=user.id, scraper_config_id=c_done.id,
                     status="done", trigger="batch")
        j_stuck = Job(id=str(uuid.uuid4()), user_id=user.id, scraper_config_id=c_stuck.id,
                      status="scraping", trigger="batch")
        db.add_all([j_done, j_stuck])
        run = BatchRun(id=str(uuid.uuid4()), batch_id=batch.id, user_id=user.id,
                       status="running", child_job_ids=[j_done.id, j_stuck.id])
        db.add(run)
        db.commit()
        run_id = run.id

        run = db.get(BatchRun, run_id)
        finalize_batch_run(db, run, forced=True)

    with SyncSessionLocal() as db:
        run = db.get(BatchRun, run_id)
        assert run.status == "partial"  # 1 done + 1 timed-out
        reasons = {f["reason"] for f in (run.failed_children or [])}
        assert "timed out" in reasons
        assert run.completed_at is not None


def test_force_finalize_uses_running_time_not_created_time():
    """A run created long ago but that JUST started running (running_at recent)
    must NOT be force-finalized — else children that just started get marked timed
    out (Codex P1). Completion sweep keys force-finalize off running_at."""
    from src.workers.scheduler import (
        BATCH_FORCE_MINUTES,
        batch_completion_sweep,
    )

    with SyncSessionLocal() as db:
        user = _user(db)
        batch = _batch(db, user.id)
        cfg = _config(db, user.id, batch.id)
        # An active child that just started — finalizing now would wrongly fail it.
        job = Job(id=str(uuid.uuid4()), user_id=user.id, scraper_config_id=cfg.id,
                  status="scraping", trigger="batch")
        db.add(job)
        old_created = datetime.now(UTC) - timedelta(minutes=BATCH_FORCE_MINUTES + 30)
        just_running = datetime.now(UTC) - timedelta(minutes=1)
        run = BatchRun(id=str(uuid.uuid4()), batch_id=batch.id, user_id=user.id,
                       status="running", child_job_ids=[job.id],
                       created_at=old_created, running_at=just_running)
        db.add(run)
        db.commit()
        run_id = run.id

    batch_completion_sweep()

    with SyncSessionLocal() as db:
        run = db.get(BatchRun, run_id)
        # Still running — NOT force-finalized despite old created_at.
        assert run.status == "running"


# ── Phase 4: delivery idempotency CAS ──────────────────────────────────────────

def test_delivery_started_cas_is_at_most_once():
    """The delivery email is gated on a CAS over delivery_started_at: a re-finalize
    (lease steal / retry) claims nothing the second time, so no duplicate email.
    This is the exact statement _deliver runs."""
    with SyncSessionLocal() as db:
        user = _user(db)
        batch = _batch(db, user.id)
        run = BatchRun(id=str(uuid.uuid4()), batch_id=batch.id, user_id=user.id,
                       status="done", child_job_ids=[])
        db.add(run)
        db.commit()
        run_id = run.id

    def _claim_delivery() -> int:
        with SyncSessionLocal() as db:
            rc = db.execute(
                update(BatchRun)
                .where(BatchRun.id == run_id, BatchRun.delivery_started_at.is_(None))
                .values(delivery_started_at=datetime.now(UTC))
            ).rowcount
            db.commit()
            return rc

    assert _claim_delivery() == 1  # first finalize delivers
    assert _claim_delivery() == 0  # re-finalize won't re-send


# ── Phase 4: lease reclaim ─────────────────────────────────────────────────────

def test_completion_sweep_reclaims_expired_lease():
    """A run left 'running' with an expired lease (worker hard-killed mid-finalize)
    is reclaimed and finalized by a later sweep — not stranded forever (Gap 2)."""
    from src.workers.scheduler import BATCH_LEASE_MINUTES, batch_completion_sweep

    with SyncSessionLocal() as db:
        user = _user(db)
        batch = _batch(db, user.id)
        cfg = _config(db, user.id, batch.id)
        job = Job(id=str(uuid.uuid4()), user_id=user.id, scraper_config_id=cfg.id,
                  status="done", trigger="batch")
        db.add(job)
        # claimed_at well past the lease TTL => a dead lease.
        stale = datetime.now(UTC) - timedelta(minutes=BATCH_LEASE_MINUTES + 5)
        run = BatchRun(id=str(uuid.uuid4()), batch_id=batch.id, user_id=user.id,
                       status="running", child_job_ids=[job.id],
                       claimed_at=stale, claim_token="dead-owner")
        db.add(run)
        db.commit()
        run_id = run.id

    batch_completion_sweep()  # no .delay here; finalize with no results => no R2

    with SyncSessionLocal() as db:
        run = db.get(BatchRun, run_id)
        # All-done child, no results => 'done'; the key point is it left 'running'.
        assert run.status == "done"
        assert run.claim_token != "dead-owner"  # lease was taken over


# ── Phase 5: recovery sweep ────────────────────────────────────────────────────

def test_recovery_sweep_gives_up_on_old_pending_run():
    """A 'pending' run whose dispatch never materialized past the force deadline is
    marked 'failed' (terminal) — it can't sit 'pending' forever (Gap 1 backstop)."""
    from src.workers.scheduler import BATCH_FORCE_MINUTES, batch_recovery_sweep

    with SyncSessionLocal() as db:
        user = _user(db)
        batch = _batch(db, user.id)
        old = datetime.now(UTC) - timedelta(minutes=BATCH_FORCE_MINUTES + 10)
        run = BatchRun(id=str(uuid.uuid4()), batch_id=batch.id, user_id=user.id,
                       status="pending", child_job_ids=[], created_at=old)
        db.add(run)
        db.commit()
        run_id = run.id

    try:
        batch_recovery_sweep()
    except OperationalError:
        pass

    with SyncSessionLocal() as db:
        run = db.get(BatchRun, run_id)
        assert run.status == "failed"
        assert run.failed_children and run.failed_children[0]["reason"]


def test_recovery_sweep_reenqueues_pending_children():
    """A 'running' run with a still-'pending' child (lost child .delay) gets the
    child re-enqueued and dispatch_attempts bumped (bounded, Gap 3a)."""
    from src.workers.scheduler import batch_recovery_sweep

    with SyncSessionLocal() as db:
        user = _user(db)
        batch = _batch(db, user.id)
        cfg = _config(db, user.id, batch.id)
        job = Job(id=str(uuid.uuid4()), user_id=user.id, scraper_config_id=cfg.id,
                  status="pending", trigger="batch")
        db.add(job)
        run = BatchRun(id=str(uuid.uuid4()), batch_id=batch.id, user_id=user.id,
                       status="running", child_job_ids=[job.id], dispatch_attempts=0)
        db.add(run)
        db.commit()
        run_id = run.id

    try:
        batch_recovery_sweep()
    except OperationalError:
        pass

    with SyncSessionLocal() as db:
        run = db.get(BatchRun, run_id)
        assert run.dispatch_attempts == 1  # bumped exactly once
