"""One active run per scraper config (UX audit F-003, migration 104).

Real Postgres, no mocks. POST /jobs is rate limited to 5 creations a minute per
user, so every case that posts more than a couple of times gets its own test (the
autouse Redis flush and a fresh user reset the limit between tests).
"""
import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.constants import ACTIVE_STATUSES
from src.db.models import BatchRun, Job, ScraperConfig, User
from src.db.session import SyncSessionLocal
from src.workers.batch_tasks import _is_one_active_job_violation
from tests.test_batch_dispatch import _batch_with_pending_run, _children, _dispatch, _user

TERMINAL = ("done", "failed")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _config(db: AsyncSession, user: User) -> str:
    cfg = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name="run guard",
        county="pierce", state="WA", record_type="probate",
        fields=[], enrichment=[], schedule={}, deliver={},
    )
    db.add(cfg)
    await db.commit()
    return cfg.id


async def _job(db: AsyncSession, user: User, config_id: str, status: str, **extra) -> str:
    job = Job(
        id=str(uuid.uuid4()), user_id=user.id, scraper_config_id=config_id,
        status=status, trigger="manual", **extra,
    )
    db.add(job)
    await db.commit()
    return job.id


def test_index_predicate_matches_active_statuses():
    """Migration 104 duplicates the status list rather than importing it. If
    ACTIVE_STATUSES ever changes, this fails until the index is migrated too."""
    with SyncSessionLocal() as db:
        pred = db.execute(text(
            "SELECT pg_get_expr(i.indpred, i.indrelid) FROM pg_class c "
            "JOIN pg_index i ON i.indexrelid = c.oid "
            "WHERE c.relname = 'uq_jobs_one_active_per_config'"
        )).scalar()
    assert pred is not None, "migration 104's index is missing"
    in_index = set(__import__("re").findall(r"'([a-z_]+)'::character varying", pred))
    assert in_index == set(ACTIVE_STATUSES)


async def test_second_run_now_is_409_naming_the_running_job(
    client: AsyncClient, business_token: str, business_user: User, db: AsyncSession
):
    config_id = await _config(db, business_user)
    first = await client.post("/jobs", json={"scraper_config_id": config_id, "trigger": "manual"},
                              headers=_auth(business_token))
    assert first.status_code == 201, first.text
    second = await client.post("/jobs", json={"scraper_config_id": config_id, "trigger": "manual"},
                               headers=_auth(business_token))
    assert second.status_code == 409
    assert second.json()["detail"] == {
        "code": "run_in_flight", "job_id": first.json()["id"],
        "message": "This scraper is already running.",
    }
    # The session recovered: the next request on the same account works.
    assert (await client.get("/jobs", headers=_auth(business_token))).status_code == 200


@pytest.mark.parametrize("status", sorted(ACTIVE_STATUSES))
async def test_every_active_status_blocks(
    status, client: AsyncClient, business_token: str, business_user: User, db: AsyncSession
):
    config_id = await _config(db, business_user)
    job_id = await _job(db, business_user, config_id, status)
    resp = await client.post("/jobs", json={"scraper_config_id": config_id, "trigger": "manual"},
                             headers=_auth(business_token))
    assert resp.status_code == 409
    assert resp.json()["detail"]["job_id"] == job_id


@pytest.mark.parametrize("status", TERMINAL)
async def test_a_finished_run_allows_a_new_one(
    status, client: AsyncClient, business_token: str, business_user: User, db: AsyncSession
):
    config_id = await _config(db, business_user)
    await _job(db, business_user, config_id, status, finished_at=datetime.now(UTC))
    resp = await client.post("/jobs", json={"scraper_config_id": config_id, "trigger": "manual"},
                             headers=_auth(business_token))
    assert resp.status_code == 201, resp.text


async def test_a_run_cancelled_mid_scrape_blocks_until_its_worker_stops(
    client: AsyncClient, business_token: str, business_user: User, db: AsyncSession
):
    """Claimed (the claim stamps last_heartbeat_at) and not yet acknowledged."""
    config_id = await _config(db, business_user)
    now = datetime.now(UTC)
    job_id = await _job(db, business_user, config_id, "cancelled",
                        started_at=now - timedelta(minutes=2),
                        last_heartbeat_at=now - timedelta(minutes=2), finished_at=now)
    resp = await client.post("/jobs", json={"scraper_config_id": config_id, "trigger": "manual"},
                             headers=_auth(business_token))
    assert resp.status_code == 409
    assert resp.json()["detail"] == {
        "code": "run_in_flight", "job_id": job_id,
        "message": "This scraper is still stopping. Try again in a few minutes.",
    }


async def test_a_run_cancelled_long_ago_or_before_it_started_does_not_block(
    client: AsyncClient, business_token: str, business_user: User, db: AsyncSession
):
    now = datetime.now(UTC)
    # Never acknowledged, but claimed longer ago than the hard time limit allows
    # any worker to live (Job.RUN_SLOT_RELEASE_AFTER_S).
    long_ago = now - timedelta(seconds=Job.RUN_SLOT_RELEASE_AFTER_S + 60)
    old = await _config(db, business_user)
    await _job(db, business_user, old, "cancelled",
               started_at=long_ago, last_heartbeat_at=long_ago, finished_at=long_ago)
    never_started = await _config(db, business_user)
    await _job(db, business_user, never_started, "cancelled", finished_at=now)
    for config_id in (old, never_started):
        resp = await client.post("/jobs", json={"scraper_config_id": config_id, "trigger": "manual"},
                                 headers=_auth(business_token))
        assert resp.status_code == 201, resp.text


async def test_two_simultaneous_clicks_start_exactly_one_run(
    client: AsyncClient, business_token: str, business_user: User, db: AsyncSession
):
    config_id = await _config(db, business_user)
    body = {"scraper_config_id": config_id, "trigger": "manual"}
    a, b = await asyncio.gather(
        client.post("/jobs", json=body, headers=_auth(business_token)),
        client.post("/jobs", json=body, headers=_auth(business_token)),
    )
    assert sorted([a.status_code, b.status_code]) == [201, 409], (a.text, b.text)
    count = (await db.execute(
        select(Job.id).where(Job.scraper_config_id == config_id)
    )).all()
    assert len(count) == 1


async def test_losing_the_race_at_the_index_is_a_clean_409_every_time(
    client: AsyncClient, business_token: str, business_user: User, db: AsyncSession
):
    """Forces the race deterministically. Another connection inserts an active
    job and holds its transaction open, so POST /jobs passes its pre-check (it
    cannot see uncommitted rows), then blocks on the unique index until that
    transaction commits, and fails there. That path must answer 409 naming the
    winner, never 500 (it once did: the rollback expired config.id and reading it
    lazy-loaded outside the async context)."""
    import threading

    config_id = await _config(db, business_user)
    user_id = business_user.id
    winner_id = str(uuid.uuid4())
    inserted = threading.Event()

    def _hold_then_commit():
        with SyncSessionLocal() as s:
            s.add(Job(id=winner_id, user_id=user_id, scraper_config_id=config_id,
                      status="queued", trigger="scheduled"))
            s.flush()
            inserted.set()
            threading.Event().wait(2.0)  # POST /jobs runs and blocks meanwhile
            s.commit()

    holder = threading.Thread(target=_hold_then_commit)
    holder.start()
    assert inserted.wait(10)
    resp = await client.post("/jobs", json={"scraper_config_id": config_id, "trigger": "manual"},
                             headers=_auth(business_token))
    holder.join(10)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["job_id"] == winner_id
    assert (await client.get("/jobs", headers=_auth(business_token))).status_code == 200


def test_the_index_rejects_a_second_active_job_on_any_path():
    with SyncSessionLocal() as db:
        user = _user(db)
        batch_id = _batch_with_pending_run(db, user.id, n_children=1)
        (cfg,) = _children(db, batch_id)
        db.add(Job(id=str(uuid.uuid4()), user_id=user.id, scraper_config_id=cfg.id,
                   status="scraping", trigger="manual"))
        db.commit()
        db.add(Job(id=str(uuid.uuid4()), user_id=user.id, scraper_config_id=cfg.id,
                   status="pending", trigger="scheduled"))
        with pytest.raises(IntegrityError) as caught:
            db.flush()
        assert _is_one_active_job_violation(caught.value)
        db.rollback()


def test_other_unique_violations_are_not_read_as_already_running():
    with SyncSessionLocal() as db:
        user = _user(db)
        batch_id = _batch_with_pending_run(db, user.id, n_children=1)
        (cfg,) = _children(db, batch_id)
        job_id = str(uuid.uuid4())
        db.add(Job(id=job_id, user_id=user.id, scraper_config_id=cfg.id,
                   status="done", trigger="manual"))
        db.commit()
        db.add(Job(id=job_id, user_id=user.id, scraper_config_id=cfg.id,
                   status="done", trigger="manual"))  # same primary key
        with pytest.raises(IntegrityError) as caught:
            db.flush()
        assert not _is_one_active_job_violation(caught.value)
        db.rollback()


def test_watchdog_requeue_of_the_same_row_still_works():
    """The watchdog moves a stuck job back to 'pending' in place; that must not
    collide with its own index entry."""
    with SyncSessionLocal() as db:
        user = _user(db)
        batch_id = _batch_with_pending_run(db, user.id, n_children=1)
        (cfg,) = _children(db, batch_id)
        job = Job(id=str(uuid.uuid4()), user_id=user.id, scraper_config_id=cfg.id,
                  status="scraping", trigger="manual")
        db.add(job)
        db.commit()
        job.status = "pending"
        db.commit()
        assert db.get(Job, job.id).status == "pending"


def test_batch_skips_a_child_that_is_already_running_and_runs_its_siblings():
    with SyncSessionLocal() as db:
        user = _user(db)
        batch_id = _batch_with_pending_run(db, user.id, n_children=3)
        busy, free_a, free_b = _children(db, batch_id)
        running = Job(id=str(uuid.uuid4()), user_id=user.id, scraper_config_id=busy.id,
                      status="scraping", trigger="manual")
        db.add(running)
        db.commit()
        busy_id, free_ids, running_id = busy.id, {free_a.id, free_b.id}, running.id

    _dispatch(batch_id)

    with SyncSessionLocal() as db:
        run = db.query(BatchRun).filter(BatchRun.batch_id == batch_id).one()
        assert run.status == "running"
        created = db.query(Job).filter(Job.id.in_(run.child_job_ids)).all()
        assert {j.scraper_config_id for j in created} == free_ids
        assert run.failed_children == [{
            "config_id": busy_id, "county": "pierce", "record_type": "probate",
            "reason": "already running", "job_id": running_id,
        }]


def test_scheduler_waits_for_a_cancelled_run_to_stop_too():
    """The run-slot rule is shared: the scheduler must not start a scraper whose
    last run was cancelled mid-scrape and has not stopped, and may once the hard
    time limit has passed."""
    from src.workers.scheduler_helpers.dispatch import _scheduled_dispatch_blocker_exists

    now = datetime.now(UTC)
    with SyncSessionLocal() as db:
        user = _user(db)
        batch_id = _batch_with_pending_run(db, user.id, n_children=2)
        recent, old = _children(db, batch_id)
        long_ago = now - timedelta(seconds=Job.RUN_SLOT_RELEASE_AFTER_S + 60)
        db.add(Job(id=str(uuid.uuid4()), user_id=user.id, scraper_config_id=recent.id,
                   status="cancelled", trigger="manual",
                   started_at=now - timedelta(minutes=2),
                   last_heartbeat_at=now - timedelta(minutes=2), finished_at=now))
        db.add(Job(id=str(uuid.uuid4()), user_id=user.id, scraper_config_id=old.id,
                   status="cancelled", trigger="manual",
                   started_at=long_ago, last_heartbeat_at=long_ago, finished_at=long_ago))
        db.commit()
        assert _scheduled_dispatch_blocker_exists(db, recent.id, now) is True
        assert _scheduled_dispatch_blocker_exists(db, old.id, now) is False


def test_batch_reports_a_child_that_is_still_stopping():
    now = datetime.now(UTC)
    with SyncSessionLocal() as db:
        user = _user(db)
        batch_id = _batch_with_pending_run(db, user.id, n_children=2)
        stopping, free = _children(db, batch_id)
        cancelled = Job(id=str(uuid.uuid4()), user_id=user.id, scraper_config_id=stopping.id,
                        status="cancelled", trigger="manual",
                        started_at=now - timedelta(minutes=1),
                        last_heartbeat_at=now - timedelta(minutes=1), finished_at=now)
        db.add(cancelled)
        db.commit()
        stopping_id, free_id, cancelled_id = stopping.id, free.id, cancelled.id

    _dispatch(batch_id)

    with SyncSessionLocal() as db:
        run = db.query(BatchRun).filter(BatchRun.batch_id == batch_id).one()
        created = db.query(Job).filter(Job.id.in_(run.child_job_ids)).all()
        assert [j.scraper_config_id for j in created] == [free_id]
        assert run.failed_children == [{
            "config_id": stopping_id, "county": "pierce", "record_type": "probate",
            "reason": "still stopping", "job_id": cancelled_id,
        }]


def test_batch_whose_children_are_all_running_fails_with_the_reasons():
    with SyncSessionLocal() as db:
        user = _user(db)
        batch_id = _batch_with_pending_run(db, user.id, n_children=2)
        for cfg in _children(db, batch_id):
            db.add(Job(id=str(uuid.uuid4()), user_id=user.id, scraper_config_id=cfg.id,
                       status="queued", trigger="manual"))
        db.commit()

    _dispatch(batch_id)

    with SyncSessionLocal() as db:
        run = db.query(BatchRun).filter(BatchRun.batch_id == batch_id).one()
        assert run.status == "failed"
        assert run.child_job_ids == []
        assert [c["reason"] for c in run.failed_children] == ["already running", "already running"]
