"""P0-07: a DONE job whose phase failed carries a durable, visible marker.

record_degraded() against the real test DB (migration 117), the real job-log
table and the real ops-alert path (its audit_events row is written whether or
not an email goes out). No mocks.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from src.db.models import Job
from src.db.session import SyncSessionLocal
from src.workers.tasks_helpers.degradation import (
    REASONS,
    DegradationNotRecordedError,
    record_degraded,
)

_SECRET = "Jane Q Marker, 1234 Marker Elm St"


@pytest.fixture
def job_id(starter_user, scraper_config):
    jid = str(uuid.uuid4())
    with SyncSessionLocal() as s:
        s.add(Job(id=jid, user_id=starter_user.id, scraper_config_id=scraper_config.id,
                  trigger="manual", status="enriching",
                  started_at=datetime(2026, 10, 11, 12, 0, tzinfo=UTC)))
        s.commit()
    return jid


def _state(jid: str, reason: str):
    with SyncSessionLocal() as s:
        marker = s.execute(text(
            "SELECT exc_type, repair_status, attempts, next_attempt_at FROM job_degradations "
            "WHERE job_id = :j AND reason = :r"), {"j": jid, "r": reason}).all()
        logs = s.execute(text(
            "SELECT level, message FROM job_logs WHERE job_id = :j"), {"j": jid}).all()
        alerts = s.execute(text(
            "SELECT detail FROM audit_events WHERE event = 'ops_alert' AND path = :p"),
            {"p": f"job_degraded:{jid}:{reason}"}).all()
    return marker, logs, alerts


def test_records_marker_log_line_and_alert(job_id, redis_client):
    with SyncSessionLocal() as db:
        assert record_degraded(db, redis_client, job_id, "nts_match", RuntimeError(_SECRET)) is True
    marker, logs, alerts = _state(job_id, "nts_match")
    assert [(m.exc_type, m.repair_status, m.attempts, m.next_attempt_at) for m in marker] == [
        ("RuntimeError", "none", 0, None)]
    assert [(lv, msg) for lv, msg in logs] == [("warning", REASONS["nts_match"])]
    assert len(alerts) == 1
    # Never the exception's text (P0-04): not in the row, the log line or the alert.
    assert "marker" not in repr((marker, logs, alerts)).lower()


def test_skip_trace_enqueue_is_queued_for_repair(job_id, redis_client):
    with SyncSessionLocal() as db:
        record_degraded(db, redis_client, job_id, "skip_trace_enqueue", RuntimeError("x"))
    (marker,), _, _ = _state(job_id, "skip_trace_enqueue")
    assert marker.repair_status == "pending" and marker.next_attempt_at is not None


def test_a_repeat_writes_and_sends_nothing(job_id, redis_client):
    with SyncSessionLocal() as db:
        assert record_degraded(db, redis_client, job_id, "owner_flags", None) is True
        assert record_degraded(db, redis_client, job_id, "owner_flags", None) is False
    marker, logs, alerts = _state(job_id, "owner_flags")
    assert len(marker) == 1 and len(logs) == 1 and len(alerts) == 1


def test_an_unwritable_marker_refuses_instead_of_letting_the_job_finish(redis_client):
    # A job id with no jobs row: the FK refuses the insert, as any write failure would.
    ghost = str(uuid.uuid4())
    with SyncSessionLocal() as db:
        with pytest.raises(DegradationNotRecordedError):
            record_degraded(db, redis_client, ghost, "enrichment", RuntimeError(_SECRET))
        # The session is usable again afterwards (rolled back, not left aborted).
        assert db.execute(text("SELECT 1")).scalar() == 1
    marker, logs, alerts = _state(ghost, "enrichment")
    assert marker == [] and logs == [] and alerts == []


def test_an_unknown_reason_is_a_programming_error(job_id, redis_client):
    with SyncSessionLocal() as db, pytest.raises(ValueError):
        record_degraded(db, redis_client, job_id, "made_up", None)


def test_every_reason_is_allowed_by_the_table(job_id, redis_client):
    # The migration's CHECK list and REASONS must not drift apart.
    with SyncSessionLocal() as db:
        for reason in REASONS:
            record_degraded(db, redis_client, job_id, reason, None)
    with SyncSessionLocal() as s:
        stored = {r for (r,) in s.execute(text(
            "SELECT reason FROM job_degradations WHERE job_id = :j"), {"j": job_id})}
    assert stored == set(REASONS)


def test_rows_go_with_their_job(job_id, redis_client):
    with SyncSessionLocal() as db:
        record_degraded(db, redis_client, job_id, "claim_transfer", None)
    with SyncSessionLocal() as s:
        s.execute(text("DELETE FROM job_logs WHERE job_id = :j"), {"j": job_id})
        s.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": job_id})
        s.commit()
        assert s.execute(text(
            "SELECT count(*) FROM job_degradations WHERE job_id = :j"), {"j": job_id}).scalar() == 0
