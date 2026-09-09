"""The "did this user download their leads?" milestone must reflect a download.

It used to read ``jobs.export_key IS NOT NULL``, which the worker writes when it
marks a job DONE. So the onboarding checklist ticked "Download leads" for people
who had never downloaded, its download step was unreachable on a normal success,
the day-3 activation email judged those users activated, and the admin funnel's
job_to_download rate read ~100% by construction.

These tests pin the new rule: an export existing is not a download, only an
observed one counts, and the presentation-only grandfather flag never leaks into
anything that claims to measure.
"""
import csv
import io
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.download_tracking import mark_leads_downloaded
from src.db.models import Job, ScraperConfig, User


async def _finished_job(db: AsyncSession, user: User, config: ScraperConfig) -> Job:
    """A job that completed and produced an export, but was never downloaded."""
    job = Job(
        id=str(uuid.uuid4()),
        user_id=user.id,
        scraper_config_id=config.id,
        status="done",
        trigger="manual",
        record_count=7,
        finished_at=datetime.now(UTC),
        export_key="exports/never-downloaded.csv",
    )
    db.add(job)
    await db.commit()
    return job


async def _onboarding(client: AsyncClient, token: str) -> dict:
    resp = await client.get(
        "/auth/onboarding", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    return resp.json()


# ─── The milestone ────────────────────────────────────────────────────────────

async def test_an_export_existing_is_not_a_download(
    client: AsyncClient,
    db: AsyncSession,
    starter_user: User,
    starter_token: str,
    scraper_config: ScraperConfig,
):
    """The exact bug: a finished job used to complete the checklist by itself."""
    await _finished_job(db, starter_user, scraper_config)

    data = await _onboarding(client, starter_token)

    assert data["steps"]["first_scrape_completed"] is True
    assert data["steps"]["first_export_downloaded"] is False
    assert data["progress_pct"] != 100
    # And the user is told to go get their leads, which used to be unreachable.
    assert data["next_action"]["action"] == "download_export"


async def test_an_observed_download_completes_the_milestone(
    client: AsyncClient,
    db: AsyncSession,
    starter_user: User,
    starter_token: str,
    scraper_config: ScraperConfig,
):
    await _finished_job(db, starter_user, scraper_config)
    await mark_leads_downloaded(str(starter_user.id))

    data = await _onboarding(client, starter_token)

    assert data["steps"]["first_export_downloaded"] is True
    assert data["progress_pct"] == 100
    assert data["next_action"]["action"] == "complete"


async def test_grandfathered_accounts_are_not_re_nagged(
    client: AsyncClient,
    db: AsyncSession,
    starter_user: User,
    starter_token: str,
    scraper_config: ScraperConfig,
):
    """Accounts that already had an export at cutover keep their 5/5.

    Their real downloads were never instrumented, so re-opening the checklist for
    them would be a worse guess than leaving it closed.
    """
    await _finished_job(db, starter_user, scraper_config)
    starter_user.onboarding_download_grandfathered = True
    await db.commit()

    data = await _onboarding(client, starter_token)

    assert data["steps"]["first_export_downloaded"] is True
    assert data["progress_pct"] == 100
    # Grandfathering is presentation only: it must not fabricate an observation.
    assert starter_user.first_leads_downloaded_at is None


# ─── Recording ────────────────────────────────────────────────────────────────

async def test_recording_is_idempotent_and_keeps_the_first_time(
    db: AsyncSession, starter_user: User
):
    """A tenth download must not move "first"."""
    await mark_leads_downloaded(str(starter_user.id))
    await db.refresh(starter_user)
    first = starter_user.first_leads_downloaded_at
    assert first is not None

    await mark_leads_downloaded(str(starter_user.id))
    await db.refresh(starter_user)
    assert starter_user.first_leads_downloaded_at == first


async def test_recording_only_touches_the_downloading_user(
    db: AsyncSession, starter_user: User, business_user: User
):
    """One user's download must not mark anybody else activated."""
    await mark_leads_downloaded(str(starter_user.id))
    await db.refresh(starter_user)
    await db.refresh(business_user)

    assert starter_user.first_leads_downloaded_at is not None
    assert business_user.first_leads_downloaded_at is None


async def test_recording_an_unknown_user_is_a_no_op(
    db: AsyncSession, starter_user: User, caplog
):
    """A conditional UPDATE that matches nothing is a real no-op, not a failure.

    Asserting only "it did not raise" would also pass if the write blew up, since
    the tracker swallows and logs. So this checks the log stayed clean AND that
    nobody was stamped.
    """
    import logging

    from src.api import download_tracking

    logger_name = download_tracking._logger.name
    with caplog.at_level(logging.ERROR, logger=logger_name):
        await mark_leads_downloaded(str(uuid.uuid4()))

    assert not [r for r in caplog.records if r.name == logger_name], [
        r.getMessage() for r in caplog.records
    ]
    await db.refresh(starter_user)
    assert starter_user.first_leads_downloaded_at is None


async def test_a_failed_write_is_logged_and_does_not_escape(
    starter_user: User, caplog, monkeypatch
):
    """Proves the companion assertion above is not vacuous.

    The tracker swallows so a bookkeeping failure cannot cost someone their
    download, which makes "nothing was logged" meaningless unless a real failure
    demonstrably DOES get logged. Induce one and watch it land.
    """
    import logging

    from src.api import download_tracking

    def _boom(*args, **kwargs):
        raise RuntimeError("database is on fire")

    monkeypatch.setattr(download_tracking, "AsyncSessionLocal", _boom)
    logger_name = download_tracking._logger.name

    with caplog.at_level(logging.ERROR, logger=logger_name):
        await download_tracking.mark_leads_downloaded(str(starter_user.id))

    errors = [r for r in caplog.records if r.name == logger_name]
    assert errors, "a failed write must be logged"
    assert "database is on fire" in caplog.text


async def test_new_users_start_unobserved(db: AsyncSession, starter_user: User):
    """NULL means unobserved. Nothing may pre-set it, least of all a backfill."""
    assert starter_user.first_leads_downloaded_at is None
    assert starter_user.onboarding_download_grandfathered is False


# ─── The download endpoint records, and minting a token does not ─────────────

async def test_minting_a_download_token_is_not_a_download(
    client: AsyncClient,
    db: AsyncSession,
    starter_user: User,
    starter_token: str,
    scraper_config: ScraperConfig,
):
    """/export-url only hands out a token; the user may never follow it."""
    job = await _finished_job(db, starter_user, scraper_config)

    resp = await client.get(
        f"/jobs/{job.id}/export-url",
        headers={"Authorization": f"Bearer {starter_token}"},
    )
    assert resp.status_code == 200
    assert "url" in resp.json()

    await db.refresh(starter_user)
    assert starter_user.first_leads_downloaded_at is None


async def test_downloading_the_csv_records_the_download(
    client: AsyncClient,
    db: AsyncSession,
    starter_user: User,
    starter_token: str,
    scraper_config: ScraperConfig,
):
    """The end-to-end path: real rows, real CSV, milestone flips."""
    from src.db.models import Result

    job = await _finished_job(db, starter_user, scraper_config)
    db.add(
        Result(
            id=str(uuid.uuid4()),
            job_id=job.id,
            user_id=starter_user.id,
            party_name="DOE, JANE",
            property_address="123 Main St, Tacoma, WA 98402",
            is_duplicate=False,
        )
    )
    await db.commit()

    resp = await client.get(
        f"/jobs/{job.id}/download",
        headers={"Authorization": f"Bearer {starter_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    assert "DOE" in resp.text or "Jane" in resp.text

    await db.refresh(starter_user)
    assert starter_user.first_leads_downloaded_at is not None


async def test_a_failed_download_records_nothing(
    client: AsyncClient,
    db: AsyncSession,
    starter_user: User,
    starter_token: str,
    scraper_config: ScraperConfig,
):
    """A job with no export 404s, and must not count as activation."""
    job = Job(
        id=str(uuid.uuid4()),
        user_id=starter_user.id,
        scraper_config_id=scraper_config.id,
        status="done",
        trigger="manual",
        record_count=0,
    )
    db.add(job)
    await db.commit()

    resp = await client.get(
        f"/jobs/{job.id}/download",
        headers={"Authorization": f"Bearer {starter_token}"},
    )
    assert resp.status_code == 404

    await db.refresh(starter_user)
    assert starter_user.first_leads_downloaded_at is None


# ─── The day-3 activation email reads the same signal ────────────────────────

def _run_beat_capturing_activation_reminders(monkeypatch) -> list[dict]:
    """Run the real beat task, capturing what it decided to tell each user.

    monkeypatch (pytest's own, not a mock library) is the only way to observe the
    decision: send_activation_reminder ends in Resend, which no-ops without an API
    key and so reports nothing either way.
    """
    from src.workers import onboarding_emails
    from src.workers.scheduler_helpers import onboarding as beat

    sent: list[dict] = []

    def _capture(email, has_scraper, has_download, days_left):
        sent.append(
            {
                "email": email,
                "has_scraper": has_scraper,
                "has_download": has_download,
            }
        )

    monkeypatch.setattr(onboarding_emails, "send_activation_reminder", _capture)
    monkeypatch.setattr(onboarding_emails, "send_day1_nudge", lambda *a, **k: None)
    monkeypatch.setattr(onboarding_emails, "send_trial_ending_email", lambda *a, **k: None)
    beat._send_onboarding_emails_impl()
    return sent


@pytest.mark.parametrize(
    ("downloaded", "grandfathered", "expected"),
    [
        (False, False, False),   # an export exists, nothing was downloaded
        (False, True, True),     # grandfathered at cutover
        (True, False, True),     # observed download
    ],
)
async def test_day3_email_reads_the_observed_download(
    db: AsyncSession,
    starter_user: User,
    scraper_config: ScraperConfig,
    monkeypatch,
    downloaded,
    grandfathered,
    expected,
):
    """Drives the real beat task, not a copy of its predicate.

    The user is placed exactly 3 days past signup with a finished, exported job,
    which is the state the old export_key rule scored as activated.
    """
    await _finished_job(db, starter_user, scraper_config)
    starter_user.created_at = datetime.now(UTC) - timedelta(days=3)
    starter_user.trial_ends_at = datetime.now(UTC) + timedelta(days=4)
    if downloaded:
        starter_user.first_leads_downloaded_at = datetime.now(UTC)
    starter_user.onboarding_download_grandfathered = grandfathered
    await db.commit()

    sent = await db.run_sync(
        lambda _s: _run_beat_capturing_activation_reminders(monkeypatch)
    )

    mine = [m for m in sent if m["email"] == starter_user.email]
    assert len(mine) == 1, f"expected one day-3 reminder, got {len(mine)}"
    assert mine[0]["has_scraper"] is True
    assert mine[0]["has_download"] is expected


async def test_beat_helper_survives_a_user_with_several_scrapers(
    db: AsyncSession, starter_user: User, scraper_config: ScraperConfig
):
    """The old existence check raised MultipleResultsFound on the second scraper.

    It ran inside the daily beat loop, so that exception aborted the whole task
    and silently cost every LATER user their onboarding emails.
    """
    from sqlalchemy import select as sa_select

    from src.workers.scheduler_helpers.onboarding import _exists

    db.add(
        ScraperConfig(
            id=str(uuid.uuid4()),
            user_id=starter_user.id,
            name="Second scraper",
            county="king",
            state="WA",
            record_type="probate",
            fields=["party_name"],
            enrichment=[],
            schedule={"frequency": "manual"},
            deliver={"format": "csv", "emails": []},
        )
    )
    await db.commit()

    # Two configs for this user: the old scalar_one_or_none() call raised here.
    count = len(
        (
            await db.execute(
                sa_select(ScraperConfig).where(ScraperConfig.user_id == starter_user.id)
            )
        ).scalars().all()
    )
    assert count == 2

    def _check(sync_session):
        return _exists(sync_session, ScraperConfig.user_id == starter_user.id)

    assert await db.run_sync(_check) is True


# ─── A file is not leads ──────────────────────────────────────────────────────

async def test_an_empty_segment_export_does_not_count_as_activation(
    client: AsyncClient, db: AsyncSession, business_user: User, business_token: str
):
    """The fabrication Codex found: a user with NO jobs could export an empty
    Lists CSV and register a download, which made the funnel able to report more
    downloads than jobs.

    The plan is no longer incidental. This test was written on a Starter fixture
    because any authenticated user reached /segments; overlap/intersection is a
    Business and Agency line and the router now carries a plan dependency, so
    Starter gets a 402 here and never reaches the question this test asks. Moved
    to Business — the SUBJECT is "an empty export is not an activation", not the
    entitlement. The gate itself is pinned across every plan and every endpoint
    on the router by test_overlap_and_intersection_are_business_and_above.
    """
    resp = await client.post(
        "/segments/intersection/export",
        headers={"Authorization": f"Bearer {business_token}"},
        json={"record_types": ["probate", "pre_foreclosure"]},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    # A header row and nothing under it.
    assert len(resp.text.strip().splitlines()) <= 1

    await db.refresh(business_user)
    assert business_user.first_leads_downloaded_at is None

# ─── A file is not leads: the count comes from the renderer now ──────────────
# The old _csv_has_a_lead parsed the rendered bytes back to answer this, which
# could raise on an oversized field and 500 a good download (#264). The renderer
# returns the row count it already had, so there is nothing left to parse and
# nothing left to raise. See tests/test_batch_export.py for its coverage.

async def test_row_count_is_the_number_of_rows_actually_written(monkeypatch):
    """Ties the count to the real writer's output, not to a constant.

    Patches `src.db.session.system_sync_session`, NOT the batch_export attribute:
    render_combined_csv imports it INSIDE the function, so patching the module
    attribute is a no-op that leaves the test talking to a real session.
    """
    from src.db import session as db_session
    from src.workers import batch_export

    class _FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def rollback(self):
            pass

    monkeypatch.setattr(db_session, "system_sync_session", lambda: _FakeSession())

    def _pairs(n: int):
        return [
            (
                SimpleNamespace(
                    party_name=f"DOE,\nJANE {i}",  # a quoted newline inside a field
                    property_address=f"{i} Main St",
                    mailing_address=None,
                    parcel_id=None,
                    date_recorded=None,
                    county="pierce",
                    phone=None,
                    email=None,
                ),
                {"lists_count": 1, "lists": "probate", "counties": "pierce"},
            )
            for i in range(n)
        ]

    for n in (0, 1, 3):
        monkeypatch.setattr(batch_export, "_combined_pairs_all", lambda *a, n=n, **k: _pairs(n))
        rendered = batch_export.render_combined_csv("user-1", ["job-1"])

        assert rendered.row_count == n
        # Count CSV RECORDS, not lines: the party names above span lines.
        reader = csv.reader(io.StringIO(rendered.data.decode("utf-8")))
        records = [r for r in reader if r]
        assert len(records) == n + 1, f"expected header + {n} rows"


async def test_the_batch_download_tracks_only_when_it_carried_rows(monkeypatch):
    """The route's gate, not the NamedTuple's truthiness.

    Asserting on RenderedCsv alone would pass with the route's condition inverted
    or deleted, which is the whole failure mode this is here to catch.
    """
    from src.api.download_tracking import mark_leads_downloaded
    from src.api.routes.batches import _stream_run_csv
    from src.workers import batch_export

    run = SimpleNamespace(
        id=str(uuid.uuid4()),
        user_id=str(uuid.uuid4()),
        status="done",
        child_job_ids=[str(uuid.uuid4())],
    )

    async def _render(count: int):
        monkeypatch.setattr(
            batch_export,
            "render_combined_csv",
            lambda *a, **k: batch_export.RenderedCsv(b"party_name\nDOE\n", count),
        )
        return await _stream_run_csv(str(uuid.uuid4()), run, None, "everything")

    empty = await _render(0)
    assert empty.background is None, "a header-only export is not leads"

    carried = await _render(2)
    assert carried.background is not None
    assert carried.background.func is mark_leads_downloaded
    assert carried.background.args == (str(run.user_id),)


async def test_a_segment_export_with_rows_records_the_download(
    client: AsyncClient,
    db: AsyncSession,
    starter_user: User,
    starter_token: str,
    scraper_config: ScraperConfig,
):
    """The positive half of the segment case, end to end through the route.

    The empty-export test above proves a header-only file does NOT count. This
    proves the other side actually fires: a Lists CSV with real leads in it is
    leads in the user's hands, so it completes the activation milestone.
    """
    from src.db.models import Result

    job = Job(
        id=str(uuid.uuid4()),
        user_id=starter_user.id,
        scraper_config_id=scraper_config.id,
        status="done",
        trigger="manual",
        record_count=1,
        finished_at=datetime.now(UTC),
        export_key="exports/segment.csv",
    )
    db.add(job)
    await db.flush()
    db.add(
        Result(
            id=str(uuid.uuid4()),
            job_id=job.id,
            user_id=starter_user.id,
            party_name="DOE, JANE",
            property_address="123 Main St, Tacoma, WA 98402",
            is_duplicate=False,
        )
    )
    # Overlap / intersection is a Business and Agency line and /segments now
    # carries a router-level plan dependency, so Starter gets a 402 here and
    # never reaches the question this test asks. The SUBJECT is "a Lists CSV
    # with real leads in it completes the activation milestone", not the
    # entitlement, so the plan is raised rather than the assertion weakened.
    # Raised on the ROW, not by minting a new token: the gate reads the plan
    # from the database, and the job and results above are owned by this user.
    # The gate itself is pinned across every plan and endpoint by
    # test_overlap_and_intersection_are_business_and_above.
    starter_user.plan = "business"
    await db.commit()
    await db.refresh(starter_user)
    assert starter_user.first_leads_downloaded_at is None

    resp = await client.post(
        "/segments/union/export",
        headers={"Authorization": f"Bearer {starter_token}"},
        json={"record_types": [scraper_config.record_type]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.text.strip().splitlines()
    assert len(body) > 1, f"expected a lead row, got only a header: {body}"

    await db.refresh(starter_user)
    assert starter_user.first_leads_downloaded_at is not None

async def test_a_batch_download_with_rows_records_the_download(
    db: AsyncSession, starter_user: User, scraper_config: ScraperConfig
):
    """The positive half of the batch case, with the REAL renderer.

    No patched renderer: the run's child_job_ids point at a real job with a real
    Result, so render_combined_csv builds the CSV from the database the way it
    does in production. That matters because the thing being proved is that a
    batch download which genuinely carried leads moves the milestone, and a
    stubbed renderer would only prove the route forwards a number.

    _stream_run_csv is the single path both batch download routes take, so this
    covers /batches/<id>/download and the run-scoped one together.

    `await resp.background()` is exactly how Starlette invokes it (its __call__
    is an async method that awaits the func). It runs inline here rather than
    after the response, which is the one thing this cannot reproduce.
    """
    from src.api.routes.batches import _stream_run_csv
    from src.db.models import Result

    job = Job(
        id=str(uuid.uuid4()),
        user_id=starter_user.id,
        scraper_config_id=scraper_config.id,
        status="done",
        trigger="manual",
        record_count=1,
        finished_at=datetime.now(UTC),
        export_key="exports/batch.csv",
    )
    db.add(job)
    await db.flush()
    db.add(
        Result(
            id=str(uuid.uuid4()),
            job_id=job.id,
            user_id=starter_user.id,
            party_name="DOE, JANE",
            property_address="123 Main St, Tacoma, WA 98402",
            is_duplicate=False,
        )
    )
    await db.commit()
    await db.refresh(starter_user)
    assert starter_user.first_leads_downloaded_at is None

    run = SimpleNamespace(
        id=str(uuid.uuid4()),
        user_id=str(starter_user.id),
        status="done",
        child_job_ids=[str(job.id)],
    )
    resp = await _stream_run_csv(str(uuid.uuid4()), run, None, "everything")

    assert resp.background is not None, "a batch export carrying a lead must track"
    await resp.background()

    await db.refresh(starter_user)
    assert starter_user.first_leads_downloaded_at is not None
