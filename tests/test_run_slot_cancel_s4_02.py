"""Audit #4 S4-02: a cancelled run holds its scraper's run slot until its worker stops.

A cancel only flips ``jobs.status``. The worker keeps scraping until it reaches a
cancel check, and the dedup claims it writes meanwhile make any new run of the
same scraper drop those leads as "already delivered". The slot used to be held
for a FIXED 300 s after the cancel, which nothing bounds the worker by.

Now the worker acknowledges its own exit (``HeartbeatThread.__exit__``), and an
unacknowledged cancelled attempt holds the slot until its Celery hard time limit
has provably killed it.

Every step here is the production code: the real claim, the real cancel route,
the real ``HeartbeatThread`` and the real ``POST /jobs`` gate. The only direct
writes move timestamps into the past to stand in for elapsed time, and one test
reproduces the watchdog's re-queue columns.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from src.api.auth import create_secure_token, hash_password
from src.db.models import Job, ScraperConfig, User
from src.db.session import SyncSessionLocal
from src.workers.tasks_helpers.status import HeartbeatThread, claim_job_for_attempt

STOPPING = "This scraper is still stopping. Try again in a few minutes."


async def _user(db) -> User:
    start = datetime.now(UTC) - timedelta(days=10)
    user = User(
        id=str(uuid.uuid4()),
        email=f"test_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
        password_hash=hash_password("TestPass123!"),
        plan="business", records_limit=5000, records_used=0,
        subscription_status="active", quota_anchor_at=start,
        quota_period_start=start, quota_period_end=start + timedelta(days=30),
        records_period_start=start,
    )
    db.add(user)
    await db.commit()
    return user


async def _config(db, user, county) -> ScraperConfig:
    config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name=f"S402 {county}",
        county=county, state="WA", record_type="probate",
        fields=["party_name", "parcel_id"], enrichment=[],
        schedule={"frequency": "manual"}, deliver={"format": "csv", "emails": []},
    )
    db.add(config)
    await db.commit()
    return config


async def _pending_job(db, user, config) -> str:
    job = Job(
        id=str(uuid.uuid4()), user_id=user.id, scraper_config_id=config.id,
        status="pending", trigger="manual",
    )
    db.add(job)
    await db.commit()
    return job.id


def _auth(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_secure_token(user.id)}"}


def _claim(job_id):
    """The worker's claim, exactly as run_scrape_job makes it."""
    with SyncSessionLocal() as s:
        started = claim_job_for_attempt(s, job_id)
    assert started is not None
    return started


async def _cancel(client, user, job_id) -> None:
    r = await client.delete(f"/jobs/{job_id}", headers=_auth(user))
    assert r.status_code == 204, r.text


async def _start(client, user, config):
    return await client.post(
        "/jobs", json={"scraper_config_id": config.id, "trigger": "manual"},
        headers=_auth(user),
    )


def _age(job_id, **seconds_back) -> None:
    """Move the named timestamp columns into the past by the DATABASE clock."""
    sets = ", ".join(f"{col} = now() - make_interval(secs => :{col})" for col in seconds_back)
    with SyncSessionLocal() as s:
        s.execute(text(f"UPDATE jobs SET {sets} WHERE id = :j"), {"j": job_id, **seconds_back})  # noqa: S608 — column names are this file's literals
        s.commit()


def _row(job_id):
    with SyncSessionLocal() as s:
        return s.execute(
            text("SELECT status, started_at, last_heartbeat_at FROM jobs WHERE id = :j"),
            {"j": job_id},
        ).one()


def _assert_refused_as_stopping(r, job_id) -> None:
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == {"code": "run_in_flight", "job_id": job_id, "message": STOPPING}


@pytest.fixture
async def scraper(db, connectors):
    county = f"s402{uuid.uuid4().hex[:8]}"
    await connectors(county, ["probate"], "manual")
    user = await _user(db)
    return user, await _config(db, user, county)


# ─── The finding ──────────────────────────────────────────────────────────────

async def test_a_cancelled_worker_that_has_not_stopped_holds_the_slot_past_five_minutes(
    db, client: AsyncClient, scraper,
):
    """The regression. The worker claimed the run and is still scraping when the
    customer cancels. Six minutes on it has not reached a cancel check, so it has
    not acknowledged. A second run must still be refused: it would lose every
    lead the old worker claims from here on."""
    user, config = scraper
    job_id = await _pending_job(db, user, config)
    _claim(job_id)
    await _cancel(client, user, job_id)
    _age(job_id, started_at=400, finished_at=360)

    _assert_refused_as_stopping(await _start(client, user, config), job_id)


# ─── Release ──────────────────────────────────────────────────────────────────

async def test_the_worker_acknowledging_its_exit_frees_the_slot_at_once(
    db, client: AsyncClient, scraper,
):
    """The worker noticed the cancel and returned. Its exit is positive evidence
    that nothing more will be claimed, so the customer need not wait at all."""
    user, config = scraper
    job_id = await _pending_job(db, user, config)
    started = _claim(job_id)
    with HeartbeatThread(job_id, interval_s=0.05) as hb:
        hb.start(started)
        await _cancel(client, user, job_id)
        _assert_refused_as_stopping(await _start(client, user, config), job_id)

    assert _row(job_id).last_heartbeat_at is None
    r = await _start(client, user, config)
    assert r.status_code == 201, r.text


async def test_a_worker_that_died_without_acknowledging_is_released_after_the_hard_limit(
    db, client: AsyncClient, scraper,
):
    """A deploy or an OOM kill never reaches the acknowledgement. Celery's hard
    time limit kills every attempt RUN_SCRAPE_TIME_LIMIT_S after it started, so an
    attempt claimed longer ago than that (plus the clock slack) cannot be alive."""
    from src.config.constants import RUN_SCRAPE_TIME_LIMIT_S

    user, config = scraper
    job_id = await _pending_job(db, user, config)
    _claim(job_id)
    await _cancel(client, user, job_id)

    _age(job_id, started_at=RUN_SCRAPE_TIME_LIMIT_S)
    _assert_refused_as_stopping(await _start(client, user, config), job_id)

    _age(job_id, started_at=Job.RUN_SLOT_RELEASE_AFTER_S + 5)
    r = await _start(client, user, config)
    assert r.status_code == 201, r.text


async def test_a_run_cancelled_before_any_worker_claimed_it_holds_nothing(
    db, client: AsyncClient, scraper,
):
    user, config = scraper
    job_id = await _pending_job(db, user, config)
    await _cancel(client, user, job_id)

    r = await _start(client, user, config)
    assert r.status_code == 201, r.text


# ─── The acknowledgement is narrow ────────────────────────────────────────────

async def test_the_acknowledgement_leaves_a_finished_job_alone(db, scraper):
    """Only a cancelled attempt is acknowledged. A worker that exits after its job
    finished must not rewrite that job's liveness."""
    user, config = scraper
    job_id = await _pending_job(db, user, config)
    started = _claim(job_id)
    with HeartbeatThread(job_id, interval_s=0.05) as hb:
        hb.start(started)
        with SyncSessionLocal() as s:
            s.execute(text("UPDATE jobs SET status = 'done', finished_at = now() WHERE id = :j"),
                      {"j": job_id})
            s.commit()

    assert _row(job_id).last_heartbeat_at is not None


async def test_a_superseded_attempt_cannot_acknowledge_for_the_live_one(
    db, client: AsyncClient, scraper,
):
    """The watchdog re-queued a silent attempt and a new worker claimed it. When
    the OLD worker finally exits, its acknowledgement names the old started_at
    and must not release the slot the live attempt still holds."""
    user, config = scraper
    job_id = await _pending_job(db, user, config)
    old_started = _claim(job_id)
    with HeartbeatThread(job_id, interval_s=0.05) as old_hb:
        old_hb.start(old_started)
        # The watchdog's re-queue (health.py _recovery_cas): back to pending,
        # attempt token and liveness cleared.
        with SyncSessionLocal() as s:
            s.execute(text(
                "UPDATE jobs SET status = 'pending', started_at = NULL, "
                "last_heartbeat_at = NULL WHERE id = :j"
            ), {"j": job_id})
            s.commit()
        new_started = _claim(job_id)
        assert new_started != old_started
        await _cancel(client, user, job_id)

    assert _row(job_id).last_heartbeat_at is not None
    _assert_refused_as_stopping(await _start(client, user, config), job_id)


def test_a_failed_acknowledgement_never_masks_the_tasks_own_exception():
    """The acknowledgement runs while the task may already be unwinding an
    exception (a crash, SoftTimeLimitExceeded). A database error inside it must
    be logged and swallowed, so the task's real error is what Celery sees.

    'not-a-uuid' makes Postgres reject the UPDATE for real."""
    hb = HeartbeatThread("not-a-uuid", interval_s=3600)
    with pytest.raises(RuntimeError, match="the task's own error"):
        with hb:
            hb.start(datetime.now(UTC))
            raise RuntimeError("the task's own error")


# ─── The claim is stamped by the database clock ───────────────────────────────

async def test_the_claim_stamps_the_attempt_with_the_database_clock(db, scraper):
    """The release ceiling compares started_at with the database's now(). Both
    must come from one clock, or a worker clock running slow would release a
    live attempt early.

    The claim runs in a transaction opened 1.5 s earlier. The database's now() is
    the transaction start, so a database-clock stamp equals it; a stamp taken
    from the worker's own clock would be 1.5 s later."""
    user, config = scraper
    job_id = await _pending_job(db, user, config)
    with SyncSessionLocal() as s:
        opened = s.execute(text("SELECT now()")).scalar_one()
        time.sleep(1.5)
        started = claim_job_for_attempt(s, job_id)

    assert started == opened
    row = _row(job_id)
    assert (row.started_at, row.last_heartbeat_at) == (started, started)
