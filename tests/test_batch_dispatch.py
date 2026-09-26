"""Track A: dispatch_batch_run materializes the API-created 'pending' run.

DB-backed (real Postgres, like test_workers). The API now creates the BatchRun
'pending' (durable intent) in the same txn as the batch; dispatch_batch_run
transitions it pending->running and creates the child jobs. These tests assert
that transition + its idempotency (a duplicate dispatch must not create a second
set of jobs).
"""
import uuid

from httpx import AsyncClient
from kombu.exceptions import OperationalError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.api.auth import hash_password
from src.api.entitlements import PAUSED_REASON_ENTITLEMENT, ConfigRow, plan_reconciliation
from src.db.models import BatchRun, Job, ScraperBatch, ScraperConfig, User
from src.db.session import SyncSessionLocal
from src.workers.batch_tasks import dispatch_batch_run


def _dispatch(batch_id: str) -> None:
    """Run dispatch_batch_run, tolerating a post-commit broker enqueue failure.

    dispatch_batch_run commits the run transition + child jobs BEFORE it calls
    run_scrape_job.delay() in a loop. These tests assert that committed DB state;
    the actual enqueue is a separate side effect whose failure is recoverable by
    batch_recovery_sweep (Track A phase 5). Swallowing a broker OperationalError
    keeps the test hermetic (independent of broker availability) without mocking."""
    try:
        dispatch_batch_run(batch_id)
    except OperationalError:
        pass


def _user(db: Session, records_used: int = 0, records_limit: int = 50) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=f"batch_dispatch_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
        password_hash=hash_password("TestPass123!"),
        plan="pro",
        records_used=records_used,
        records_limit=records_limit,
    )
    db.add(user)
    db.flush()
    return user


def _batch_with_pending_run(db: Session, user_id: str, n_children: int = 2) -> str:
    """Mirror what POST /batches now persists: a ScraperBatch + N child configs +
    a 'pending' BatchRun, all committed together."""
    batch = ScraperBatch(
        id=str(uuid.uuid4()),
        user_id=user_id,
        name="Dispatch Test",
        state="WA",
        fields=[],
        enrichment=[],
        schedule={},
        deliver={},
        status="active",
    )
    db.add(batch)
    db.flush()
    for i in range(n_children):
        db.add(
            ScraperConfig(
                id=str(uuid.uuid4()),
                user_id=user_id,
                batch_id=batch.id,
                name=f"child {i}",
                county="pierce",
                state="WA",
                record_type="probate",
                fields=[],
                enrichment=[],
                schedule={},
                deliver={},
            )
        )
    db.add(
        BatchRun(
            id=str(uuid.uuid4()),
            batch_id=batch.id,
            user_id=user_id,
            status="pending",
            child_job_ids=[],
        )
    )
    db.commit()
    return batch.id


def test_dispatch_materializes_pending_run():
    """pending -> running, child jobs created, child_job_ids populated."""

    with SyncSessionLocal() as db:
        user = _user(db)
        batch_id = _batch_with_pending_run(db, user.id, n_children=2)

    _dispatch(batch_id)

    with SyncSessionLocal() as db:
        run = db.query(BatchRun).filter(BatchRun.batch_id == batch_id).one()
        assert run.status == "running"
        assert len(run.child_job_ids) == 2
        # dispatch_attempts is owned by the recovery sweep, not the normal dispatch.
        assert run.dispatch_attempts == 0
        assert run.running_at is not None  # stuck-time baseline set on materialize
        jobs = db.query(Job).filter(Job.id.in_(run.child_job_ids)).all()
        assert len(jobs) == 2
        assert all(j.status == "pending" and j.trigger == "batch" for j in jobs)


def test_dispatch_is_idempotent_no_duplicate_jobs():
    """A duplicate dispatch of an already-materialized run must NOT create a
    second set of jobs — the run stays 'running' with the same children."""

    with SyncSessionLocal() as db:
        user = _user(db)
        batch_id = _batch_with_pending_run(db, user.id, n_children=3)

    _dispatch(batch_id)
    with SyncSessionLocal() as db:
        first_ids = set(
            db.query(BatchRun).filter(BatchRun.batch_id == batch_id).one().child_job_ids
        )

    _dispatch(batch_id)  # duplicate / recovery re-dispatch
    with SyncSessionLocal() as db:
        run = db.query(BatchRun).filter(BatchRun.batch_id == batch_id).one()
        assert run.status == "running"
        assert set(run.child_job_ids) == first_ids  # same jobs, not a new set
        total_batch_jobs = db.query(Job).filter(Job.user_id == user.id).count()
        assert total_batch_jobs == 3  # exactly the original children


def test_dispatch_over_limit_run_fails_with_no_jobs():
    """If the user is over their monthly record limit at dispatch, the pending
    run becomes 'failed' and no child jobs are created."""

    with SyncSessionLocal() as db:
        user = _user(db, records_used=50, records_limit=50)  # at the cap
        batch_id = _batch_with_pending_run(db, user.id, n_children=2)

    _dispatch(batch_id)

    with SyncSessionLocal() as db:
        run = db.query(BatchRun).filter(BatchRun.batch_id == batch_id).one()
        assert run.status == "failed"
        assert run.child_job_ids == []
        assert db.query(Job).filter(Job.user_id == user.id).count() == 0


# ── F-043: children the user deleted must not be scraped or counted ───────────
# DELETE /scrapers/{id} is a soft delete (active=False) that keeps batch_id. A
# downgrade pause is active=False with paused_reason='entitlement'.


def _children(db: Session, batch_id: str) -> list[ScraperConfig]:
    return (
        db.query(ScraperConfig)
        .filter(ScraperConfig.batch_id == batch_id)
        .order_by(ScraperConfig.name)
        .all()
    )


def _delete(cfg: ScraperConfig) -> None:
    cfg.active = False
    cfg.paused_reason = None


def _pause(cfg: ScraperConfig) -> None:
    cfg.active = False
    cfg.paused_reason = PAUSED_REASON_ENTITLEMENT


def test_dispatch_skips_deleted_child_and_reports_paused_child():
    """One active, one deleted, one downgrade-paused child: only the active one
    gets a Job; the paused one is reported as a plan limit; the deleted one is
    neither run nor reported."""
    with SyncSessionLocal() as db:
        user = _user(db)
        batch_id = _batch_with_pending_run(db, user.id, n_children=3)
        active, deleted, paused = _children(db, batch_id)
        _delete(deleted)
        _pause(paused)
        db.commit()
        active_id, deleted_id, paused_id = active.id, deleted.id, paused.id

    _dispatch(batch_id)

    with SyncSessionLocal() as db:
        run = db.query(BatchRun).filter(BatchRun.batch_id == batch_id).one()
        assert run.status == "running"
        jobs = db.query(Job).filter(Job.user_id == user.id).all()
        assert [j.scraper_config_id for j in jobs] == [active_id]
        reported = {c["config_id"]: c["reason"] for c in run.failed_children}
        assert reported == {paused_id: "plan limit"}
        assert deleted_id not in reported


def test_dispatch_paused_child_gets_no_job_even_without_a_violation():
    """config_run_violation only counts ACTIVE configs, so for a lone paused
    child it finds nothing wrong. The pause itself must still block the Job."""
    with SyncSessionLocal() as db:
        user = _user(db)
        batch_id = _batch_with_pending_run(db, user.id, n_children=1)
        (paused,) = _children(db, batch_id)
        _pause(paused)
        db.commit()
        paused_id = paused.id

    _dispatch(batch_id)

    with SyncSessionLocal() as db:
        run = db.query(BatchRun).filter(BatchRun.batch_id == batch_id).one()
        assert run.status == "failed"
        assert run.failed_children == [{
            "config_id": paused_id, "county": "pierce",
            "record_type": "probate", "reason": "plan limit",
        }]
        assert db.query(Job).filter(Job.user_id == user.id).count() == 0


def test_dispatch_blocks_a_child_that_is_active_but_still_marked_paused():
    """active=True with paused_reason='entitlement' should never exist (the
    reconcile clears both together), but if it does, the pause wins: blocked
    and reported, never scraped and billed."""
    with SyncSessionLocal() as db:
        user = _user(db)
        batch_id = _batch_with_pending_run(db, user.id, n_children=1)
        (odd,) = _children(db, batch_id)
        odd.active = True
        odd.paused_reason = PAUSED_REASON_ENTITLEMENT
        db.commit()
        odd_id = odd.id

    _dispatch(batch_id)

    with SyncSessionLocal() as db:
        run = db.query(BatchRun).filter(BatchRun.batch_id == batch_id).one()
        assert run.status == "failed"
        assert [c["config_id"] for c in run.failed_children] == [odd_id]
        assert db.query(Job).filter(Job.user_id == user.id).count() == 0


def test_dispatch_all_deleted_batch_creates_no_job():
    """Every child deleted: no Job, and the zero-children path (not the
    all-blocked "failed" path), since nothing is left to run or block."""
    with SyncSessionLocal() as db:
        user = _user(db)
        batch_id = _batch_with_pending_run(db, user.id, n_children=2)
        for cfg in _children(db, batch_id):
            _delete(cfg)
        db.commit()

    _dispatch(batch_id)

    with SyncSessionLocal() as db:
        run = db.query(BatchRun).filter(BatchRun.batch_id == batch_id).one()
        assert run.status == "done"
        assert run.child_job_ids == []
        assert db.query(Job).filter(Job.user_id == user.id).count() == 0


async def test_batch_list_and_detail_count_the_same_current_children(
    client: AsyncClient, business_token: str, business_user: User, db: AsyncSession
):
    """List child_count, detail child_count and the detail children list all
    exclude the deleted child and keep the paused one."""
    batch = ScraperBatch(
        id=str(uuid.uuid4()), user_id=business_user.id, name="F-043 counts",
        state="WA", fields=[], enrichment=[], schedule={}, deliver={}, status="active",
    )
    db.add(batch)
    await db.flush()
    ids = []
    for i, (active, reason) in enumerate(
        [(True, None), (False, None), (False, PAUSED_REASON_ENTITLEMENT)]
    ):
        cfg = ScraperConfig(
            id=str(uuid.uuid4()), user_id=business_user.id, batch_id=batch.id,
            name=f"child {i}", county="pierce", state="WA", record_type="probate",
            fields=[], enrichment=[], schedule={}, deliver={},
            active=active, paused_reason=reason,
        )
        db.add(cfg)
        ids.append(cfg.id)
    await db.commit()
    active_id, deleted_id, paused_id = ids
    headers = {"Authorization": f"Bearer {business_token}"}

    listed = await client.get("/batches", headers=headers)
    assert listed.status_code == 200
    (summary,) = [b for b in listed.json() if b["id"] == batch.id]

    detail = await client.get(f"/batches/{batch.id}", headers=headers)
    assert detail.status_code == 200
    body = detail.json()

    assert summary["child_count"] == 2
    assert body["child_count"] == 2
    assert {c["config_id"] for c in body["children"]} == {active_id, paused_id}
    assert deleted_id not in {c["config_id"] for c in body["children"]}


async def test_deleting_a_paused_scraper_clears_the_pause_so_upgrade_cannot_revive_it(
    client: AsyncClient, business_token: str, business_user: User, db: AsyncSession
):
    """Before the fix a scraper deleted while downgrade-paused kept
    paused_reason='entitlement', and plan_reconciliation revives exactly those
    rows once the plan permits them: the deleted scraper came back and ran."""
    cfg = ScraperConfig(
        id=str(uuid.uuid4()), user_id=business_user.id, name="paused then deleted",
        county="pierce", state="WA", record_type="probate",
        fields=[], enrichment=[], schedule={}, deliver={},
        active=False, paused_reason=PAUSED_REASON_ENTITLEMENT,
    )
    db.add(cfg)
    await db.commit()
    # Read before expire_all(): an expired attribute lazy-loads, which an async
    # session cannot do implicitly.
    cfg_id, plan = cfg.id, business_user.plan

    resp = await client.delete(
        f"/scrapers/{cfg_id}", headers={"Authorization": f"Bearer {business_token}"}
    )
    assert resp.status_code == 204

    db.expire_all()
    row = (
        await db.execute(select(ScraperConfig).where(ScraperConfig.id == cfg_id))
    ).scalar_one()
    assert row.active is False
    assert row.paused_reason is None
    _, revive_ids = plan_reconciliation(
        [ConfigRow(row.id, row.state, row.county, row.record_type, row.created_at,
                   row.active, row.paused_reason)],
        plan,
    )
    assert row.id not in revive_ids
