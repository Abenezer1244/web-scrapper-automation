"""P0-07 6b: the beat re-runs the skip-trace enqueue of a DONE job whose enqueue crashed.

Seeded by a real run_scrape_job (the 6a harness) whose enqueue is made to fail
once, so the marker, the job and its leads are exactly what production leaves.
The repair then runs the real enqueue against the real test DB. Only the county
portal and R2 (external) are replaced, plus the enqueue itself where a test needs
it to fail. Lease expiry is stood in for by moving next_attempt_at.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from src.config import settings
from src.db.session import SyncSessionLocal
from src.workers import degradation_repair as repair
from src.workers import tasks
from src.workers.tasks_helpers import enrich
from tests.test_job_degradations_wiring import _apply, _crash, make_job  # noqa: F401

_REAL_ENQUEUE = tasks._enqueue_skip_trace_rows


@pytest.fixture
def skip_trace_on(monkeypatch):
    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "p007-test-token-not-used")


@pytest.fixture
def degraded_job(make_job, redis_client, monkeypatch, skip_trace_on):  # noqa: F811
    """A DONE job whose enqueue crashed: marker pending, leads not_attempted."""
    job_id = make_job(skip_trace=True)
    monkeypatch.setattr(tasks, "_enqueue_skip_trace_rows", _crash)
    _apply(job_id)
    monkeypatch.setattr(tasks, "_enqueue_skip_trace_rows", _REAL_ENQUEUE)
    assert _marker(job_id).repair_status == "pending"
    return job_id


def _marker(job_id):
    with SyncSessionLocal() as s:
        return s.execute(text(
            "SELECT repair_status, attempts, last_error_type, next_attempt_at, "
            "next_attempt_at > now() + interval '50 minutes' AS far "
            "FROM job_degradations WHERE job_id = :j AND reason = 'skip_trace_enqueue'"),
            {"j": job_id}).one()


def _queued(job_id) -> int:
    with SyncSessionLocal() as s:
        return s.execute(text(
            "SELECT count(*) FROM pending_skip_trace_rows p JOIN results r ON r.id = p.result_id "
            "WHERE r.job_id = :j"), {"j": job_id}).scalar_one()


def _due_now(job_id):
    """Stand in for the lease or backoff running out."""
    with SyncSessionLocal() as s:
        s.execute(text(
            "UPDATE job_degradations SET next_attempt_at = now() - interval '1 second' "
            "WHERE job_id = :j"), {"j": job_id})
        s.commit()


def _alerts(job_id) -> int:
    with SyncSessionLocal() as s:
        return s.execute(text(
            "SELECT count(*) FROM audit_events WHERE event = 'ops_alert' AND path = :p"),
            {"p": f"job_degraded_repair:{job_id}:skip_trace_enqueue"}).scalar_one()


def test_the_repair_queues_the_leads_once(degraded_job, redis_client):
    assert _queued(degraded_job) == 0
    repair.repair_degraded_skip_trace()
    assert _marker(degraded_job).repair_status == "repaired"
    queued = _queued(degraded_job)
    assert queued > 0

    # Nothing is due any more, and the enqueue itself queues nothing twice.
    _due_now(degraded_job)
    repair.repair_degraded_skip_trace()
    assert _marker(degraded_job).repair_status == "repaired"
    assert _queued(degraded_job) == queued
    from src.db.models import Job, ScraperConfig
    from src.db.session import rls_sync_session

    with SyncSessionLocal() as s:
        uid = s.get(Job, degraded_job).user_id
    with rls_sync_session(str(uid)) as db:
        job = db.get(Job, degraded_job)
        enrich._enqueue_skip_trace_rows(db, job, redis_client, degraded_job,
                                        db.get(ScraperConfig, job.scraper_config_id))
    assert _queued(degraded_job) == queued


def test_skip_trace_switched_off_since_is_suppressed(degraded_job, redis_client):
    with SyncSessionLocal() as s:
        s.execute(text(
            "UPDATE scraper_configs SET skip_trace_enabled = false WHERE id = "
            "(SELECT scraper_config_id FROM jobs WHERE id = :j)"), {"j": degraded_job})
        s.commit()
    repair.repair_degraded_skip_trace()
    assert _marker(degraded_job).repair_status == "suppressed"
    assert _queued(degraded_job) == 0


def test_a_switch_off_after_the_config_was_read_is_honoured_under_the_lock(
        degraded_job, redis_client):
    """The enqueue re-reads the flag under its claim lock (Codex)."""
    from src.db.models import Job, ScraperConfig
    from src.db.session import rls_sync_session

    with SyncSessionLocal() as s:
        uid = s.get(Job, degraded_job).user_id
    with rls_sync_session(str(uid)) as db:
        job = db.get(Job, degraded_job)
        config = db.get(ScraperConfig, job.scraper_config_id)
        assert config.skip_trace_enabled  # read while still on
        with SyncSessionLocal() as other:
            other.execute(text(
                "UPDATE scraper_configs SET skip_trace_enabled = false WHERE id = :c"),
                {"c": config.id})
            other.commit()
        db.commit()
        outcome = enrich._enqueue_skip_trace_rows(db, job, redis_client, degraded_job, config)
    assert outcome == "config_off"
    assert _queued(degraded_job) == 0


def test_skip_trace_off_for_everyone_waits_without_using_an_attempt(
        degraded_job, redis_client, monkeypatch):
    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", False)
    repair.repair_degraded_skip_trace()
    m = _marker(degraded_job)
    assert (m.repair_status, m.attempts) == ("pending", 0)
    assert m.far  # back in about an hour, not hammered every tick
    assert _queued(degraded_job) == 0


def test_a_job_that_is_no_longer_done_is_suppressed(degraded_job, redis_client):
    with SyncSessionLocal() as s:
        s.execute(text("UPDATE jobs SET status = 'failed' WHERE id = :j"), {"j": degraded_job})
        s.commit()
    repair.repair_degraded_skip_trace()
    assert _marker(degraded_job).repair_status == "suppressed"
    assert _queued(degraded_job) == 0


def test_repeated_failures_back_off_then_give_up_with_one_alert(
        degraded_job, redis_client, monkeypatch):
    monkeypatch.setattr(enrich, "_enqueue_skip_trace_rows", _crash)
    for attempt in range(1, repair.MAX_ATTEMPTS + 1):
        repair.repair_degraded_skip_trace()
        m = _marker(degraded_job)
        assert m.attempts == attempt
        assert m.last_error_type == "_PhaseCrashError"
        if attempt < repair.MAX_ATTEMPTS:
            assert m.repair_status == "pending"
            assert _alerts(degraded_job) == 0
            _due_now(degraded_job)
    assert _marker(degraded_job).repair_status == "gave_up"
    assert _alerts(degraded_job) == 1
    _due_now(degraded_job)
    repair.repair_degraded_skip_trace()  # a given-up row is never picked again
    assert _marker(degraded_job).attempts == repair.MAX_ATTEMPTS
    assert _alerts(degraded_job) == 1


def test_a_claimed_row_is_not_claimed_again_and_a_lost_lease_writes_nothing(
        degraded_job, redis_client):
    first = repair._claim(10)
    assert [str(r.job_id) for r in first] == [degraded_job]
    assert _marker(degraded_job).far  # leased past the task's own time limit
    assert repair._claim(10) == []  # still leased: no second worker gets it

    _due_now(degraded_job)  # lease ran out; a newer run takes it
    second = repair._claim(10)
    assert len(second) == 1
    assert repair._settle(first[0], repair_status="repaired") is False  # stale run
    assert _marker(degraded_job).repair_status == "pending"
    assert repair._settle(second[0], repair_status="repaired") is True


def test_paused_claims_wait_quietly_across_retries(degraded_job, redis_client, monkeypatch):
    """Claims paused: wait, no attempt used, and no customer line per retry (Codex)."""
    from src.workers import skip_trace_claim

    monkeypatch.setattr(skip_trace_claim, "claim_enforcement_ok", lambda db: False)
    with SyncSessionLocal() as s:
        before = s.execute(text("SELECT count(*) FROM job_logs WHERE job_id = :j"),
                           {"j": degraded_job}).scalar_one()
    for _ in range(2):
        repair.repair_degraded_skip_trace()
        _due_now(degraded_job)
    m = _marker(degraded_job)
    assert (m.repair_status, m.attempts) == ("pending", 0)
    with SyncSessionLocal() as s:
        after = s.execute(text("SELECT count(*) FROM job_logs WHERE job_id = :j"),
                          {"j": degraded_job}).scalar_one()
    assert after == before
    assert _queued(degraded_job) == 0


def test_a_switch_off_cannot_land_while_the_claim_is_being_made(
        degraded_job, redis_client, monkeypatch):
    """FOR SHARE holds the scraper's row from the re-read to the claim's commit.

    The tick may also repair other due jobs left by earlier tests, so the probe
    targets the scraper of the job each claim is for, and every one must hold.
    """
    from sqlalchemy.exc import OperationalError

    from src.workers import skip_trace_claim

    real = skip_trace_claim.claim_skip_trace_rows
    probes: dict[str, bool] = {}

    def claim_while_probing(db, payloads, *a, **k):
        with SyncSessionLocal() as other:
            job_id = str(other.execute(text(
                "SELECT job_id FROM results WHERE id = :r"),
                {"r": str(payloads[0]["result_id"])}).scalar_one())
            other.execute(text("SET lock_timeout = '300ms'"))
            try:
                other.execute(text(
                    "UPDATE scraper_configs SET skip_trace_enabled = false WHERE id = "
                    "(SELECT scraper_config_id FROM jobs WHERE id = :j)"), {"j": job_id})
                probes[job_id] = False
            except OperationalError:
                probes[job_id] = True
            other.rollback()
        return real(db, payloads, *a, **k)

    monkeypatch.setattr(skip_trace_claim, "claim_skip_trace_rows", claim_while_probing)
    repair.repair_degraded_skip_trace()
    assert probes.get(degraded_job) is True
    assert all(probes.values()), probes
    assert _marker(degraded_job).repair_status == "repaired"
    assert _queued(degraded_job) > 0
