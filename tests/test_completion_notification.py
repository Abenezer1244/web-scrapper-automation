"""The job_completed notification names what was already delivered (UX 2d, F-006).

A run whose leads were all delivered before used to notify "0 new records", the same
text as a county that returned nothing. The detail now carries `already_delivered`
from the job's persisted, validated breakdown snapshot (the number GET /jobs shows),
present even when it is 0, and ABSENT when the run has no valid snapshot: an unknown
count is never written as 0.

Drives the production emitter, `emit_job_completed`, on real rows and real
`notifications` inserts, after `finalize_billing_and_done` where the run matters.
The last test pins that run_scrape_job emits through it and nowhere else.
"""
# The `finalizing` fixture imported from test_run_breakdown_finalize is requested by
# parameter name, which ruff reads as redefining the import. Only F811, only this file.
# ruff: noqa: F811
import inspect
import logging
import uuid

from sqlalchemy import text

from src.api.run_breakdown import SNAPSHOT_COLUMNS
from src.db.models import Job, Result, ScraperConfig
from src.db.session import SyncSessionLocal
from src.workers.tasks_helpers.finalize import FinalizeKind, emit_job_completed
from src.workers.tasks_helpers.status import AttemptToken
from tests.test_run_breakdown_finalize import (  # noqa: F401 - fixture used by name
    ADDR,
    _finalize,
    _live,
    finalizing,
)

BASE_KEYS = {"scraper_name", "county", "record_count"}


def _snapshot(already_delivered: int, new: int = 3) -> dict:
    """A snapshot that validates: the six sum to records_found, new = billed = record."""
    return {
        "breakdown_dropped_before_save": 0, "breakdown_no_address": 0,
        "breakdown_same_run_merged": 0, "breakdown_already_delivered": already_delivered,
        "breakdown_over_quota": 0, "breakdown_new": new,
        "records_found": already_delivered + new, "record_count": new, "billed_count": new,
    }


def _done_job(user, config, **columns) -> str:
    job_id = str(uuid.uuid4())
    with SyncSessionLocal() as s:
        s.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id,
                  trigger="scheduled", status="done", **columns))
        s.commit()
    return job_id


def _emit(job_id: str, display_count: int) -> None:
    with SyncSessionLocal() as s:
        job = s.get(Job, job_id)
        emit_job_completed(job, s.get(ScraperConfig, job.scraper_config_id), job_id,
                           display_count)


def _details(job_id: str) -> list[dict]:
    with SyncSessionLocal() as s:
        return [row.detail for row in s.execute(
            text("SELECT detail FROM notifications "
                 "WHERE job_id = :j AND type = 'job_completed'"), {"j": job_id})]


def _only_detail(job_id: str) -> dict:
    details = _details(job_id)
    assert len(details) == 1, details
    return details[0]


# ── the detail, from the row ─────────────────────────────────────────────────

def test_a_valid_snapshot_names_the_already_delivered_count(starter_user, scraper_config):
    job_id = _done_job(starter_user, scraper_config, **_snapshot(already_delivered=123))

    _emit(job_id, 3)

    detail = _only_detail(job_id)
    assert detail["already_delivered"] == 123
    assert detail == {"scraper_name": scraper_config.name, "county": scraper_config.county,
                      "record_count": 3, "already_delivered": 123}


def test_a_frozen_zero_is_written_as_zero_not_left_out(starter_user, scraper_config):
    job_id = _done_job(starter_user, scraper_config, **_snapshot(already_delivered=0))

    _emit(job_id, 3)

    assert _only_detail(job_id)["already_delivered"] == 0


def test_no_snapshot_leaves_the_key_out_and_the_rest_unchanged(starter_user, scraper_config):
    job_id = _done_job(starter_user, scraper_config, records_found=5, record_count=2,
                       billed_count=2)

    _emit(job_id, 2)

    assert _only_detail(job_id) == {"scraper_name": scraper_config.name,
                                    "county": scraper_config.county, "record_count": 2}


def test_a_rejected_snapshot_leaves_the_key_out_and_is_logged(
    starter_user, scraper_config, caplog,
):
    columns = _snapshot(already_delivered=123)
    columns["breakdown_over_quota"] = None  # partial: cannot happen by construction
    job_id = _done_job(starter_user, scraper_config, **columns)

    with caplog.at_level(logging.WARNING):
        _emit(job_id, 3)

    assert set(_only_detail(job_id)) == BASE_KEYS
    assert any("partial snapshot" in r.getMessage() and job_id in r.getMessage()
               for r in caplog.records)


def test_the_job_completed_pref_still_suppresses_the_row(starter_user, scraper_config):
    with SyncSessionLocal() as s:
        s.execute(text("UPDATE users SET notification_prefs = "
                       "CAST('{\"job_completed\": false}' AS json) WHERE id = :u"),
                  {"u": starter_user.id})
        s.commit()
    job_id = _done_job(starter_user, scraper_config, **_snapshot(already_delivered=123))

    _emit(job_id, 3)

    assert _details(job_id) == []


# ── after a real finalization ────────────────────────────────────────────────

def _add_prior_delivery(f) -> None:
    """One more saved row, a lead an earlier run of this account delivered."""
    with SyncSessionLocal() as s:
        s.add(Result(id=str(uuid.uuid4()), job_id=f["job_id"], user_id=f["user_id"],
                     property_address=ADDR, is_duplicate=True,
                     duplicate_reason="prior_run"))
        s.execute(text("UPDATE jobs SET records_found = 4 WHERE id = :j"),
                  {"j": f["job_id"]})
        s.commit()


def test_a_fresh_run_notifies_what_it_froze(finalizing, redis_client):
    _add_prior_delivery(finalizing)

    out = _finalize(finalizing, finalizing["a"], redis_client)
    assert out.kind is FinalizeKind.DONE and out.frozen["already_delivered"] == 1
    _emit(finalizing["job_id"], out.display_count)

    assert _only_detail(finalizing["job_id"])["already_delivered"] == 1


def test_a_retried_run_froze_nothing_so_the_key_is_absent(finalizing, redis_client):
    _add_prior_delivery(finalizing)
    with SyncSessionLocal() as s:
        s.execute(text("UPDATE jobs SET retry_count = 1 WHERE id = :j"),
                  {"j": finalizing["job_id"]})
        s.commit()

    out = _finalize(finalizing, AttemptToken(finalizing["a"].started_at, 1), redis_client)
    assert out.kind is FinalizeKind.DONE and out.frozen is None
    _emit(finalizing["job_id"], out.display_count)

    assert set(_only_detail(finalizing["job_id"])) == BASE_KEYS


def test_a_rerun_billed_earlier_still_names_the_kept_snapshot(finalizing, redis_client):
    """Billed and frozen by an earlier pass, rows changed since: finalization keeps the
    columns and returns frozen=None, and the notification still reports the kept
    snapshot, because it reads the row rather than this pass's decision."""
    kept = _snapshot(already_delivered=123)
    with SyncSessionLocal() as s:
        s.execute(text(
            "UPDATE jobs SET billing_applied_at = clock_timestamp(), "
            + ", ".join(f"{c} = {v}" for c, v in kept.items())
            + " WHERE id = :jid"), {"jid": finalizing["job_id"]})
        s.add(Result(id=str(uuid.uuid4()), job_id=finalizing["job_id"],
                     user_id=finalizing["user_id"], property_address=ADDR,
                     is_duplicate=False))
        s.commit()

    out = _finalize(finalizing, finalizing["a"], redis_client)
    assert out.kind is FinalizeKind.DONE and out.frozen is None and out.display_count == 3
    with SyncSessionLocal() as s:
        row = s.get(Job, finalizing["job_id"])
        assert row.breakdown_already_delivered == 123
        assert {c: getattr(row, c) for c in SNAPSHOT_COLUMNS.values()} == {
            c: kept[c] for c in SNAPSHOT_COLUMNS.values()}
    _emit(finalizing["job_id"], out.display_count)

    assert _only_detail(finalizing["job_id"])["already_delivered"] == 123


# ── run_scrape_job emits through the helper, once ────────────────────────────

def test_run_scrape_job_emits_job_completed_only_through_the_helper():
    from src.workers.tasks import run_scrape_job

    body = _live(inspect.getsource(run_scrape_job.__wrapped__))
    tail = body[body.index("if _outcome.kind is not FinalizeKind.DONE:"):]
    assert body.count("emit_job_completed(") == 1
    assert "emit_job_completed(job, config, job_id, display_count)" in tail
    assert '"job_completed"' not in body and "'job_completed'" not in body


def test_tasks_calls_the_finalize_module_helper():
    from src.workers import tasks

    assert tasks.emit_job_completed is emit_job_completed
