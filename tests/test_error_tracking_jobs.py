"""P0-03 5d: a job that ends FAILED is reported once, as a sanitised event.

report_job_failure runs with the real SDK on the in-memory transport from
test_error_tracking, in a fresh interpreter per scenario. The _fail_job tests run
the real function against the real test DB and Redis (CI; skipped locally
without Postgres), with the SDK initialised in-process and torn down after.
"""
import json
import uuid
from datetime import UTC, datetime

import pytest

from src.config import settings
from src.db.models import Job
from src.db.session import SyncSessionLocal
from src.workers.tasks_helpers.status import AttemptToken, _fail_job
from tests.test_error_tracking import _CANARY_EMAIL, _CANARY_SQL, _GOOD_DSN, _assert_clean, _run


def _events(out: dict) -> list[dict]:
    return [json.loads(env.splitlines()[-1]) for env in out["envelopes"]]


def _frames(exc: dict) -> list[dict]:
    return exc.get("stacktrace", {}).get("frames", [])


def test_inside_an_except_the_cause_travels_with_the_report():
    out = _run(
        f"""
        from src.utils.error_tracking import init_error_tracking, report_job_failure

        init_error_tracking("worker", transport=transport)

        def scrape_county():
            raise TimeoutError("portal slow for {_CANARY_SQL}")

        def fail_the_job():
            try:
                scrape_county()
            except TimeoutError:
                report_job_failure("failed")

        fail_the_job()
        sentry_sdk.flush()
        report()
        """
    )
    (event,) = _events(out)
    types = [v["type"] for v in event["exception"]["values"]]
    assert types == ["TimeoutError", "JobFailedError"]
    assert event["tags"] == {"service": "worker", "job_outcome": "failed"}
    assert "scrape_county" in [f.get("function") for f in _frames(event["exception"]["values"][0])]
    _assert_clean("".join(out["envelopes"]))


def test_outside_an_except_the_report_points_at_the_caller():
    out = _run(
        """
        from src.utils.error_tracking import init_error_tracking, report_job_failure

        init_error_tracking("worker", transport=transport)

        def plan_cap_refused():
            report_job_failure("failed")

        def run_scrape_job():
            plan_cap_refused()

        run_scrape_job()
        sentry_sdk.flush()
        report()
        """
    )
    (event,) = _events(out)
    (exc,) = event["exception"]["values"]
    assert exc["type"] == "JobFailedError" and exc["value"] == "[redacted]"
    functions = [f.get("function") for f in _frames(exc)]
    # oldest first, newest (the line that failed the job) last; the helper itself excluded
    assert functions[-2:] == ["run_scrape_job", "plan_cap_refused"]
    assert "report_job_failure" not in functions and "_caller_traceback" not in functions


def test_watchdog_outcome_has_its_own_type_and_tag():
    out = _run(
        """
        from src.utils.error_tracking import init_error_tracking, report_job_failure

        init_error_tracking("worker", transport=transport)
        report_job_failure("watchdog_failed")
        sentry_sdk.flush()
        report()
        """
    )
    (event,) = _events(out)
    assert event["exception"]["values"][-1]["type"] == "WatchdogFailedError"
    assert event["tags"]["job_outcome"] == "watchdog_failed"


def test_each_job_is_its_own_event_and_the_tag_does_not_stick():
    out = _run(
        """
        from src.utils.error_tracking import init_error_tracking, report_job_failure

        init_error_tracking("worker", transport=transport)

        def fail_the_job():
            report_job_failure("failed")

        fail_the_job()
        fail_the_job()
        try:
            raise KeyError("unrelated")
        except KeyError:
            sentry_sdk.capture_exception()
        sentry_sdk.flush()
        report()
        """
    )
    first, second, unrelated = _events(out)
    assert first["event_id"] != second["event_id"]
    assert first["tags"]["job_outcome"] == second["tags"]["job_outcome"] == "failed"
    assert "job_outcome" not in unrelated["tags"]


def test_frames_are_capped():
    out = _run(
        """
        from src.utils.error_tracking import _MAX_CALLER_FRAMES, init_error_tracking, report_job_failure

        init_error_tracking("worker", transport=transport)

        def deep(n):
            if n == 0:
                report_job_failure("failed")
            else:
                deep(n - 1)

        deep(60)
        sentry_sdk.flush()
        report(cap=_MAX_CALLER_FRAMES)
        """
    )
    (event,) = _events(out)
    assert len(_frames(event["exception"]["values"][-1])) == out["cap"]


def test_off_unknown_outcome_and_broken_transport_never_raise():
    off = _run(
        """
        from src.utils.error_tracking import report_job_failure

        report_job_failure("failed")
        report()
        """,
        dsn=None,
    )
    assert off["envelopes"] == []
    out = _run(
        f"""
        from src.utils.error_tracking import init_error_tracking, report_job_failure

        class Broken(MemoryTransport):
            def capture_envelope(self, envelope):
                raise OSError("disk full {_CANARY_EMAIL}")

        init_error_tracking("worker", transport=Broken())
        report_job_failure("{_CANARY_EMAIL}")
        report_job_failure("failed")
        report(survived=True)
        """
    )
    assert out["survived"] is True
    assert "unknown job outcome; not reported" in out["stderr"]
    assert _CANARY_EMAIL not in out["stderr"]


@pytest.fixture
def tracking():
    """Error tracking initialised IN this process on an in-memory transport, so the
    real _fail_job can run against the test DB; fully torn down afterwards."""
    import sentry_sdk
    from sentry_sdk.transport import Transport

    from src.utils import error_tracking

    class MemoryTransport(Transport):
        def __init__(self):
            super().__init__()
            self.envelopes = []

        def capture_envelope(self, envelope):
            self.envelopes.append(envelope.serialize().decode("utf-8", "replace"))

    sent = MemoryTransport()
    saved = settings.SENTRY_DSN
    settings.SENTRY_DSN = _GOOD_DSN
    try:
        assert error_tracking.init_error_tracking("worker", transport=sent)
        yield sent.envelopes
    finally:
        sentry_sdk.get_client().close()
        sentry_sdk.get_global_scope().set_client(None)
        sentry_sdk.get_global_scope().remove_tag("service")
        error_tracking._initialised = None
        settings.SENTRY_DSN = saved
        assert not sentry_sdk.get_client().is_active()
        assert "service" not in sentry_sdk.get_global_scope()._tags
        assert "service" not in sentry_sdk.get_isolation_scope()._tags


def _job(user_id, config_id, status="scraping"):
    job_id = str(uuid.uuid4())
    with SyncSessionLocal() as s:
        s.add(Job(id=job_id, user_id=user_id, scraper_config_id=config_id,
                  trigger="manual", status=status, started_at=datetime.now(UTC)))
        s.commit()
    return job_id


def test_fail_job_reports_once_when_its_cas_lands(tracking, starter_user, scraper_config, redis_client):
    job_id = _job(starter_user.id, scraper_config.id)
    with SyncSessionLocal() as s:
        job = s.get(Job, job_id)
        assert _fail_job(s, job, redis_client, job_id, f"boom {_CANARY_EMAIL}") is True
        assert job.status == "failed"
        # Already terminal: the CAS does not land again, so nothing more is sent.
        assert _fail_job(s, job, redis_client, job_id, "again") is False
    import sentry_sdk

    sentry_sdk.flush()
    (envelope,) = tracking
    _assert_clean(envelope)
    event = json.loads(envelope.splitlines()[-1])
    assert event["tags"] == {"service": "worker", "job_outcome": "failed"}
    assert event["exception"]["values"][-1]["type"] == "JobFailedError"


def test_fail_job_sends_nothing_for_a_business_outcome_or_a_stale_attempt(
    tracking, starter_user, scraper_config, redis_client
):
    refused = _job(starter_user.id, scraper_config.id)
    stale = _job(starter_user.id, scraper_config.id)
    with SyncSessionLocal() as s:
        job = s.get(Job, refused)
        assert _fail_job(s, job, redis_client, refused, "paused", report=False) is True
        job = s.get(Job, stale)
        old_token = AttemptToken(started_at=datetime(2020, 1, 1, tzinfo=UTC), retry_count=0)
        assert _fail_job(s, job, redis_client, stale, "late", expected_started_at=old_token) is False
        assert job.status == "scraping"
    import sentry_sdk

    sentry_sdk.flush()
    assert tracking == []


def _watchdog_outcomes(envelopes: list[str]) -> list[dict]:
    events = [json.loads(env.splitlines()[-1]) for env in envelopes]
    return [e for e in events if e.get("tags", {}).get("job_outcome") == "watchdog_failed"]


def test_watchdog_reports_a_permanent_failure_once(tracking):
    import sentry_sdk

    from src.workers.scheduler import watchdog_stuck_jobs
    from tests.test_workers import _create_stuck_job, _create_sync_config, _create_sync_user

    with SyncSessionLocal() as s:
        user = _create_sync_user(s)
        config = _create_sync_config(s, user.id)
        job = _create_stuck_job(s, user.id, config.id, minutes_ago=75)
        job.retry_count = 3
        job_id = job.id
        s.commit()

    watchdog_stuck_jobs()
    watchdog_stuck_jobs()  # the job is terminal now: the second tick's CAS cannot land
    sentry_sdk.flush()

    with SyncSessionLocal() as s:
        assert s.get(Job, job_id).status == "failed"
    (event,) = _watchdog_outcomes(tracking)
    assert event["exception"]["values"][-1]["type"] == "WatchdogFailedError"
    _assert_clean("".join(tracking))


def test_watchdog_requeue_is_not_a_failure(tracking):
    import sentry_sdk

    from src.workers.scheduler import watchdog_stuck_jobs
    from tests.test_workers import _create_stuck_job, _create_sync_config, _create_sync_user

    with SyncSessionLocal() as s:
        user = _create_sync_user(s)
        config = _create_sync_config(s, user.id)
        job = _create_stuck_job(s, user.id, config.id, minutes_ago=75)
        job_id = job.id
        s.commit()

    watchdog_stuck_jobs()
    sentry_sdk.flush()

    with SyncSessionLocal() as s:
        assert s.get(Job, job_id).status == "pending"
    assert _watchdog_outcomes(tracking) == []
