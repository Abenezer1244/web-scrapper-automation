"""What a connector reports, and who is allowed to record it.

Two guarantees, both learned the hard way on the King probate run that showed
"0%" for 8m18s of an 8m42s job.

The denominator has to be real. It used to be estimated as
`max(1, span // chunk_days + 1)` while the loop walked its own `while`, and the
two disagreed at every exact multiple of the chunk size — including 90, which is
what `rolling_90` resolves to. The most common configuration could therefore
never show progress past 50%. `chunk_windows` now produces both the count and the
iteration, so they cannot drift.

And an observation may only be recorded by the attempt that made it. A scraper
that is slow rather than dead can call back long after the watchdog has replaced
its attempt, and that late call must not overwrite the live attempt's counters or
resurrect a cancelled run.
"""
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from src.api.auth import hash_password
from src.db.models import Job, ScraperConfig, User
from src.db.session import SyncSessionLocal
from src.scrapers.base_scraper import chunk_windows
from src.workers.tasks_helpers.status import _set_progress, _set_stage

# ─── The denominator is the windows, not an estimate ─────────────────────────

def _walked(span_days: int, chunk_days: int = 90) -> list[tuple[datetime, datetime]]:
    start = datetime(2026, 1, 1)
    return chunk_windows(start, start + timedelta(days=span_days), chunk_days)


def test_the_count_matches_the_windows_at_every_boundary():
    """The old estimate was wrong at exactly 0, 90, 180 and 270 days — every
    multiple of the chunk size — and right everywhere else, which is why it
    survived so long. 90 is the one that mattered: it is `rolling_90`."""
    def old_estimate(span: int) -> int:
        return max(1, span // 90 + 1)

    for span, expected in [(0, 0), (1, 1), (30, 1), (89, 1), (90, 1),
                           (91, 2), (180, 2), (181, 3), (270, 3)]:
        assert len(_walked(span)) == expected, f"span={span}"

    # And the failure is pinned, so nobody reintroduces the estimate.
    for span in (0, 90, 180, 270):
        assert old_estimate(span) != len(_walked(span)), (
            f"span={span} should expose the old off-by-one"
        )


def test_the_windows_tile_the_range_exactly():
    """No gap, no overlap, and the last window lands exactly on the end date. A
    gap would drop a date range of leads silently; an overlap would rescrape."""
    start = datetime(2026, 1, 1)
    end = start + timedelta(days=200)
    windows = chunk_windows(start, end, 90)

    assert windows[0][0] == start
    assert windows[-1][1] == end
    # Consecutive pairs, so the second sequence is deliberately one shorter.
    for (_, prev_end), (next_start, _) in zip(windows, windows[1:], strict=False):
        assert prev_end == next_start


def test_a_range_with_no_days_walks_nothing():
    """A same-day range used to report one chunk and run zero, so progress sat at
    0 of 1 forever. Zero windows is the honest answer, and the caller then reports
    no denominator at all rather than a total it will never reach."""
    assert _walked(0) == []
    assert _walked(-5) == []


# ─── Only the attempt that owns the job may record progress ──────────────────

def _job_row(db: Session, **over):
    user = User(
        id=str(uuid.uuid4()),
        email=f"prog_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
        password_hash=hash_password("TestPass123!"),
        plan="starter", records_used=0, records_limit=50,
    )
    db.add(user)
    db.flush()
    config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name="Progress Test",
        county="king", state="WA", record_type="probate",
        fields=[], enrichment=[], schedule={"frequency": "manual"},
        deliver={"format": "csv", "emails": []},
    )
    db.add(config)
    db.flush()
    fields = {
        "id": str(uuid.uuid4()), "user_id": user.id,
        "scraper_config_id": config.id, "status": "scraping",
        "trigger": "manual", "started_at": datetime.now(UTC),
    }
    fields.update(over)
    job = Job(**fields)
    db.add(job)
    db.commit()
    return job


def test_progress_from_the_owning_attempt_is_recorded():
    """Positive control. Without it every test below would pass on a helper that
    never writes anything."""
    with SyncSessionLocal() as db:
        job = _job_row(db)
        fired = _set_progress(
            db, job, expected_started_at=job.started_at,
            units_done=3, units_total=5, progress_unit="chunk", records_found=42,
        )
        assert fired is True

    with SyncSessionLocal() as db:
        row = db.get(Job, job.id)
        assert (row.units_done, row.units_total) == (3, 5)
        assert row.progress_unit == "chunk"
        assert row.records_found == 42


def test_a_superseded_attempt_cannot_overwrite_the_live_one():
    """The watchdog re-queues a job whose worker looks gone, and the replacement
    stamps a fresh started_at. A callback from the OLD attempt — a scraper that
    was slow rather than dead — must land nowhere."""
    with SyncSessionLocal() as db:
        job = _job_row(db)
        dead_attempt = job.started_at

        # The replacement attempt claims the row.
        db.execute(
            sa_text(
                "UPDATE jobs SET started_at = now(), units_done = 9, "
                "units_total = 10, records_found = 900 WHERE id = :j"
            ),
            {"j": job.id},
        )
        db.commit()

        fired = _set_progress(
            db, job, expected_started_at=dead_attempt,
            units_done=1, units_total=5, records_found=7,
        )
        assert fired is False

    with SyncSessionLocal() as db:
        row = db.get(Job, job.id)
        assert (row.units_done, row.units_total, row.records_found) == (9, 10, 900)


def test_a_cancelled_run_stops_accepting_progress():
    """A scraper can run on for a while after a cancel lands. Every further
    observation must be refused, or the Live Run page shows a cancelled job
    advancing."""
    with SyncSessionLocal() as db:
        job = _job_row(db)
        db.execute(
            sa_text("UPDATE jobs SET status='cancelled', finished_at=now() WHERE id=:j"),
            {"j": job.id},
        )
        db.commit()

        assert _set_progress(
            db, job, expected_started_at=job.started_at, units_done=4, records_found=99,
        ) is False
        assert _set_stage(db, job, "enriching", expected_started_at=job.started_at) is False

    with SyncSessionLocal() as db:
        row = db.get(Job, job.id)
        assert row.status == "cancelled"
        assert row.units_done is None and row.records_found is None and row.stage is None


def test_a_job_object_carrying_another_tenant_writes_nothing():
    """Every query filters by user_id, RLS being only the belt. A job object whose
    user_id does not match the row's owner must not land an observation, even with
    the right id and attempt token. The object is detached before the change, so
    the altered user_id cannot be autoflushed onto the row ahead of the UPDATE."""
    with SyncSessionLocal() as db:
        job = _job_row(db)
        other = _job_row(db)
        owner = job.user_id
        db.expunge(job)
        job.user_id = other.user_id

        fired = _set_progress(
            db, job, expected_started_at=job.started_at, units_done=1, units_total=5,
        )
        assert fired is False

    with SyncSessionLocal() as db:
        row = db.get(Job, job.id)
        assert row.user_id == owner
        assert row.units_done is None and row.units_total is None


def test_a_stage_write_stamps_when_the_stage_began():
    """stage_started_at is what lets the UI say 'still connecting after 3 minutes'
    without re-deriving it from log timestamps."""
    with SyncSessionLocal() as db:
        job = _job_row(db)
        before = datetime.now(UTC)
        assert _set_stage(db, job, "connecting", expected_started_at=job.started_at) is True

    with SyncSessionLocal() as db:
        row = db.get(Job, job.id)
        assert row.stage == "connecting"
        assert row.stage_started_at is not None
        assert row.stage_started_at >= before - timedelta(seconds=5)


def test_an_empty_observation_writes_nothing():
    """A caller with no facts to record must not issue an UPDATE that touches the
    row (and takes a lock on it) for no reason."""
    with SyncSessionLocal() as db:
        job = _job_row(db)
        assert _set_progress(db, job, expected_started_at=job.started_at) is False


# ─── Every connector says what it is doing ───────────────────────────────────

def test_every_live_connector_reports_a_stage():
    """A connector that never calls report_stage() sits on the worker's coarse
    "connecting" label for its entire run, however long that is.

    That was the state of 7 of the 10 connectors after the first pass: honest, but
    it meant a Snohomish run showed one label from start to finish. This is the
    test that fails when a NEW connector is added without one, which is the only
    way that gap comes back.

    Walks the registry's module allowlist rather than a list written here, so a
    connector added tomorrow is covered without anyone remembering this file.
    """
    import importlib
    import inspect

    from src.scrapers.base_scraper import BridgeScraper
    from src.scrapers.registry import _ALLOWED_SCRAPER_MODULES

    missing = []
    checked = []
    for mod_name in sorted(_ALLOWED_SCRAPER_MODULES):
        if mod_name.endswith("base_scraper"):
            continue  # the base defines report_stage; it does not scrape
        mod = importlib.import_module(mod_name)
        for _, cls in inspect.getmembers(mod, inspect.isclass):
            if not issubclass(cls, BridgeScraper) or cls is BridgeScraper:
                continue
            if cls.__module__ != mod_name:
                continue  # imported from elsewhere; checked with its own module
            if "scrape" not in cls.__dict__:
                continue  # inherits scrape(), so its parent's call covers it
            checked.append(cls.__name__)
            if "report_stage(" not in inspect.getsource(cls.__dict__["scrape"]):
                missing.append(cls.__name__)

    assert checked, "walked the allowlist and found no connectors — the walk is wrong"
    assert not missing, (
        "these connectors never report a stage, so the Live Run page shows one "
        f"label for their whole run: {sorted(set(missing))}"
    )
