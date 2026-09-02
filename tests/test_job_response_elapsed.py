"""JobResponse.elapsed_* must stop at finished_at for terminal jobs.

Live 2026-09-02: an 8-second job (started 04:47:58Z, finished 04:48:06Z) reported
``elapsed_seconds=21429`` / ``"357m 9s"`` when read six hours later, because the
computed field measured started_at -> now regardless of status.
"""
from datetime import UTC, datetime, timedelta

from src.api.schemas import JobResponse


def _job(**over) -> JobResponse:
    base = {
        "id": "5db4a9c7-36aa-4426-ac73-b6ed9886dd0a",
        "user_id": "01dc9396-9a36-49b5-9b98-5343ec107232",
        "scraper_config_id": "6931754e-fe13-4314-b8cb-ba10e2d19434",
        "status": "done",
        "trigger": "manual",
        "page_current": 1,
        "page_total": 1,
        "record_count": 6,
        "export_key": None,
        "error_message": None,
        "retry_count": 0,
        "started_at": datetime(2026, 9, 2, 4, 47, 58, tzinfo=UTC),
        "finished_at": datetime(2026, 9, 2, 4, 48, 6, tzinfo=UTC),
        "created_at": datetime(2026, 9, 2, 4, 47, 57, tzinfo=UTC),
    }
    base.update(over)
    return JobResponse(**base)


def test_done_job_elapsed_is_started_to_finished():
    j = _job()
    assert j.elapsed_seconds == 8
    assert j.elapsed_time == "8s"


def test_failed_and_cancelled_also_stop_the_clock():
    for status in ("failed", "cancelled"):
        assert _job(status=status).elapsed_seconds == 8


def test_naive_finished_at_treated_as_utc():
    j = _job(finished_at=datetime(2026, 9, 2, 4, 48, 6))
    assert j.elapsed_seconds == 8


def test_running_job_still_measures_to_now():
    started = datetime.now(UTC) - timedelta(seconds=90)
    j = _job(status="scraping", started_at=started, finished_at=None)
    assert 89 <= j.elapsed_seconds <= 95


def test_done_without_finished_at_falls_back_to_now():
    # Legacy rows with a NULL finished_at keep the old behaviour rather than 0.
    started = datetime.now(UTC) - timedelta(seconds=30)
    j = _job(started_at=started, finished_at=None)
    assert 29 <= j.elapsed_seconds <= 35
