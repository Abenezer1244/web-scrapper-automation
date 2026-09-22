"""JobResponse must not describe a dead run as live, or a retry backoff as idle.

Live 2026-09-09, job 9c8b7259 (pierce/WA/probate): a Railway worker redeploy
stopped the container ~13 seconds into the scrape phase. A container stop raises
no Python exception, so the job row kept status='scraping' with finished_at NULL,
error_message NULL and record_count 0. Every label in model_post_init describes an
in-progress run, so the page showed "Scraping records..." with a live badge and a
climbing elapsed clock for a job nothing was working on.

The same job had spent the preceding five minutes in a transient-retry backoff,
where the row is status='pending' with retry_count=1 and read "Waiting to start".

These tests pin both readings, and pin the thresholds to the ones the WATCHDOG
uses: the UI must not call a job stalled before the code that recovers it would,
nor keep claiming progress after it acts.
"""
from datetime import UTC, datetime, timedelta

from src.api.schemas import JobResponse
from src.config.constants import (
    HEARTBEAT_STALE_MINUTES,
    STUCK_STARTED_AT_FALLBACK_MINUTES,
    ZOMBIE_UNSTARTED_MINUTES,
)


def _ago(**kw) -> datetime:
    """A timestamp relative to NOW AT CALL TIME.

    Deliberately not a module-level constant: the full suite runs for ~18 minutes,
    so an import-time "now" drifts far past the 10-minute zombie threshold and the
    "not yet stalled" cases start failing purely on how long the run took.
    """
    return datetime.now(UTC) - timedelta(**kw)


def _job(**over) -> JobResponse:
    base = {
        "id": "9c8b7259-a940-454e-b510-a074b861af59",
        "user_id": "e73585c6-e10d-48e3-941f-28090380ff51",
        "scraper_config_id": "ea533c9e-d194-4f74-a8e7-4b32e9d428e1",
        "status": "scraping",
        "trigger": "manual",
        "page_current": 0,
        "page_total": 0,
        "record_count": 0,
        "export_key": None,
        "error_message": None,
        "retry_count": 0,
        "started_at": _ago(minutes=2),
        "finished_at": None,
        "created_at": _ago(minutes=3),
        "last_heartbeat_at": _ago(seconds=30),
    }
    base.update(over)
    return JobResponse(**base)


# ─── A live run is still reported as live ────────────────────────────────────

def test_fresh_heartbeat_is_not_stalled():
    j = _job()
    assert j.progress_stalled is False
    # No stage reported yet and no counters: the activity alone, no numbers.
    assert j.progress_label == "Collecting records"
    assert j.progress_pct is None


def test_a_long_but_live_job_is_not_stalled():
    """The 2026-06-17 guarantee: a job far past the 70-minute age fallback is
    LIVE, not stuck, as long as it is still beating. Calling it stalled here is
    what once caused a live job to be re-queued and duplicate its results."""
    j = _job(
        started_at=_ago(minutes=200),
        last_heartbeat_at=_ago(minutes=1),
        page_current=40,
        page_total=120,
        record_count=900,
    )
    assert j.progress_stalled is False
    # Legacy counters, no progress_unit: the counts are shown, the NOUN is not
    # guessed. This row was written by a worker that predates migration 099.
    assert j.progress_label == "Collecting records: 40 of 120"


def test_real_page_progress_survives_the_liveness_check():
    j = _job(page_current=3, page_total=5, record_count=30)
    assert j.progress_stalled is False
    assert j.progress_label == "Collecting records: 3 of 5"
    assert j.progress_pct == 60


# ─── A dead run is reported honestly ─────────────────────────────────────────

def test_stale_heartbeat_marks_the_run_stalled():
    j = _job(last_heartbeat_at=_ago(minutes=HEARTBEAT_STALE_MINUTES + 5))
    assert j.progress_stalled is True
    assert j.progress_label == "No recent progress reported. Checking on this run."


def test_stalled_wins_over_a_page_counter_frozen_by_the_kill():
    """A worker killed on page 3 of 5 leaves those counters behind. Reporting
    "Page 3 of 5" would present a frozen number as live progress."""
    j = _job(
        page_current=3,
        page_total=5,
        record_count=30,
        last_heartbeat_at=_ago(minutes=HEARTBEAT_STALE_MINUTES + 1),
    )
    assert j.progress_stalled is True
    assert j.progress_label.startswith("No recent progress")


def test_every_active_status_is_liveness_checked():
    stale = _ago(minutes=HEARTBEAT_STALE_MINUTES + 1)
    for status in ("queued", "probing", "scraping", "enriching"):
        assert _job(status=status, last_heartbeat_at=stale).progress_stalled is True, status


def test_naive_heartbeat_is_treated_as_utc():
    naive = (_ago(minutes=HEARTBEAT_STALE_MINUTES + 5)).replace(tzinfo=None)
    assert _job(last_heartbeat_at=naive).progress_stalled is True


# ─── NULL heartbeat is "unobserved", never "dead" ────────────────────────────

def test_null_heartbeat_uses_the_conservative_age_fallback():
    """NULL means nothing has been observed yet (a row claimed by an older worker
    image), not that the worker died. Declaring it stalled early would alarm users
    about jobs the watchdog is not going to touch for another hour."""
    inside = _job(
        started_at=_ago(minutes=STUCK_STARTED_AT_FALLBACK_MINUTES - 10),
        last_heartbeat_at=None,
    )
    assert inside.progress_stalled is False

    beyond = _job(
        started_at=_ago(minutes=STUCK_STARTED_AT_FALLBACK_MINUTES + 10),
        last_heartbeat_at=None,
    )
    assert beyond.progress_stalled is True


def test_an_aged_zombie_with_no_started_at_is_stalled():
    """A worker that died between the broker delivery and the claim leaves an
    ACTIVE status with NEITHER a heartbeat NOR a started_at. The watchdog already
    re-queues these on the creation-age cutoff; before this branch existed the API
    reported them live forever, because both earlier signals were NULL (Codex)."""
    fresh = _job(
        status="queued",
        started_at=None,
        last_heartbeat_at=None,
        created_at=_ago(minutes=ZOMBIE_UNSTARTED_MINUTES - 9),
    )
    assert fresh.progress_stalled is False  # still plausibly waiting for capacity

    aged = _job(
        status="queued",
        started_at=None,
        last_heartbeat_at=None,
        created_at=_ago(minutes=ZOMBIE_UNSTARTED_MINUTES + 30),
    )
    assert aged.progress_stalled is True


def test_the_stalled_label_promises_no_restart():
    """The watchdog re-queues a stalled job only while its retry budget lasts and
    permanently FAILS it once that is spent, so promising a restart would break
    the promise for exactly the jobs that most need honest wording (Codex)."""
    label = _job(last_heartbeat_at=_ago(hours=1)).progress_label
    assert "restart" not in label.lower()
    assert "will " not in label.lower()


def test_thresholds_match_the_watchdog():
    """These are the watchdog's own numbers. If they ever diverge, the UI starts
    lying in one direction or the other."""
    assert HEARTBEAT_STALE_MINUTES == 15
    assert STUCK_STARTED_AT_FALLBACK_MINUTES == 70
    assert ZOMBIE_UNSTARTED_MINUTES == 10


def test_the_watchdog_reads_the_shared_constants_not_its_own_literals():
    """Asserting the constants equal 15/70/10 proves nothing on its own: the
    watchdog could still compute its cutoffs from bare literals that happen to
    agree today and silently drift tomorrow. It DID — the zombie branch was still
    `timedelta(minutes=10)` after the API had been hoisted onto the constant
    (Codex). Read the watchdog's source and require every cutoff to name a
    constant, so reintroducing a literal fails here instead of in production.
    """
    import inspect
    import re

    from src.workers.scheduler_helpers import health

    src = inspect.getsource(health._watchdog_stuck_jobs_impl)
    cutoffs = re.findall(r"(\w*cutoff)\s*=\s*now\s*-\s*timedelta\(minutes=([^)]+)\)", src)
    assert len(cutoffs) == 3, f"expected 3 cutoffs, found {cutoffs}"
    for name, expr in cutoffs:
        assert not expr.strip().isdigit(), (
            f"{name} uses the bare literal {expr.strip()} instead of a shared "
            "constant from src.config.constants"
        )
    assert {e.strip() for _, e in cutoffs} == {
        "HEARTBEAT_STALE_MINUTES",
        "STUCK_STARTED_AT_FALLBACK_MINUTES",
        "ZOMBIE_UNSTARTED_MINUTES",
    }


# ─── A retry backoff reads as a retry, not as idle ───────────────────────────

def test_pending_retry_reads_as_waiting_to_retry():
    j = _job(status="pending", retry_count=1, started_at=None, last_heartbeat_at=None)
    assert j.retry_pending is True
    assert j.progress_label == "Waiting to retry"


def test_a_first_run_still_reads_as_waiting_to_start():
    j = _job(status="pending", retry_count=0, started_at=None, last_heartbeat_at=None)
    assert j.retry_pending is False
    assert j.progress_label == "Waiting to start"


# ─── Terminal states are untouched by any of this ────────────────────────────

def test_terminal_states_are_never_stalled_or_retry_pending():
    for status in ("done", "failed", "cancelled"):
        j = _job(
            status=status,
            retry_count=2,
            finished_at=datetime.now(UTC),
            last_heartbeat_at=_ago(hours=6),
        )
        assert j.progress_stalled is False, status
        assert j.retry_pending is False, status


# ─── User-facing copy rules ──────────────────────────────────────────────────

def test_no_em_dash_and_no_internals_in_the_new_copy():
    """The new strings reach end users. No em dash, and nothing that leaks an
    exception class, URL, job id or Celery task id."""
    labels = [
        _job(last_heartbeat_at=_ago(hours=1)).progress_label,
        _job(
            status="pending", retry_count=1, started_at=None, last_heartbeat_at=None
        ).progress_label,
    ]
    for label in labels:
        assert "—" not in label, label
        lowered = label.lower()
        for leak in ("error:", "timeout", "http", "://", "traceback", "celery", "task["):
            assert leak not in lowered, (leak, label)


# ─── The transient-retry line the customer reads ─────────────────────────────
# Live 2026-09-09: "Transient error, retrying in ~5 min (retry 1 of 2)." read as
# though the records had failed, and quoted a time even when the broker publish
# had failed and nothing was actually scheduled.

def test_retry_notice_names_the_connection_and_the_attempt():
    from src.workers.tasks_helpers.status import transient_retry_notice

    msg = transient_retry_notice(
        retry_count=1, max_retries=2, countdown=358, published=True
    )
    assert msg == (
        "The county portal request could not be completed. Retrying in about 5 min "
        "(attempt 2 of 3)."
    )


def test_retry_notice_claims_no_schedule_when_the_publish_failed():
    """Nothing is scheduled on a publish failure: the row waits for the watchdog's
    stranded-retry sweep, which only looks at rows older than the 70-minute cutoff.
    Quoting a minute count would be wrong, and so would "shortly" (Codex)."""
    from src.workers.tasks_helpers.status import transient_retry_notice

    msg = transient_retry_notice(
        retry_count=1, max_retries=2, countdown=358, published=False
    )
    assert "min" not in msg
    assert "shortly" not in msg
    assert "queued" not in msg
    assert msg == (
        "The county portal request could not be completed. This run will be retried "
        "(attempt 2 of 3)."
    )


def test_retry_notice_never_reports_a_zero_minute_wait():
    from src.workers.tasks_helpers.status import transient_retry_notice

    msg = transient_retry_notice(
        retry_count=1, max_retries=2, countdown=20, published=True
    )
    assert "about 1 min" in msg


def test_retry_notice_counts_the_final_attempt_correctly():
    from src.workers.tasks_helpers.status import transient_retry_notice

    msg = transient_retry_notice(
        retry_count=2, max_retries=2, countdown=1200, published=True
    )
    assert "attempt 3 of 3" in msg
    assert "about 20 min" in msg


def test_retry_notice_leaks_no_internals_and_no_em_dash():
    from src.workers.tasks_helpers.status import transient_retry_notice

    for published in (True, False):
        msg = transient_retry_notice(
            retry_count=1, max_retries=2, countdown=358, published=published
        )
        assert "—" not in msg
        lowered = msg.lower()
        for leak in (
            "timeout", "playwright", "locator", "traceback", "://", "celery",
            "exception", "armsweb",
        ):
            assert leak not in lowered, (leak, msg)
