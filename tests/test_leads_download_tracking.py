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
import uuid
from datetime import UTC, datetime, timedelta

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
    client: AsyncClient, db: AsyncSession, starter_user: User, starter_token: str
):
    """The fabrication Codex found: a user with NO jobs could export an empty
    Lists CSV and register a download, which made the funnel able to report more
    downloads than jobs.
    """
    resp = await client.post(
        "/segments/intersection/export",
        headers={"Authorization": f"Bearer {starter_token}"},
        json={"record_types": ["probate", "pre_foreclosure"]},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    # A header row and nothing under it.
    assert len(resp.text.strip().splitlines()) <= 1

    await db.refresh(starter_user)
    assert starter_user.first_leads_downloaded_at is None


def test_header_only_batch_csv_is_not_a_lead():
    from src.api.routes.batches import _csv_has_a_lead

    assert _csv_has_a_lead(b"party_name,property_address\n") is False
    assert _csv_has_a_lead(b"") is False


def test_a_batch_csv_with_a_row_is_a_lead():
    from src.api.routes.batches import _csv_has_a_lead

    assert _csv_has_a_lead(b"party_name,property_address\nDOE JANE,123 Main St\n") is True


def test_a_quoted_newline_inside_a_field_is_not_counted_as_a_row():
    """Counting lines instead of parsing would call this header-only file a lead."""
    from src.api.routes.batches import _csv_has_a_lead

    header_only_with_wrapped_heading = b'"party\nname",address\n'
    assert _csv_has_a_lead(header_only_with_wrapped_heading) is False

    one_row_spanning_lines = b'party_name,address\n"DOE,\nJANE","123 Main St"\n'
    assert _csv_has_a_lead(one_row_spanning_lines) is True


def test_segment_response_only_tracks_when_there_are_rows():
    """The gate lives on the shared helper, so both segment exports inherit it."""
    from src.api.routes.segments import _segment_csv_response

    empty = _segment_csv_response([], "bridgeleads_overlap_none", "user-1")
    assert empty.background is None


# ─── The lead check must never cost someone their download ───────────────────

def test_an_outsized_field_does_not_blow_up_the_lead_check():
    """csv.reader refuses a field over 131,072 chars.

    legal_description and heirs are uncapped Text, and this check runs OUTSIDE
    the caller's error handler, so one outsized lead used to turn a good export
    into a 500. It must answer, not raise.
    """
    from src.api.routes.batches import _csv_has_a_lead

    big = "x" * 200_000
    oversized = f'party_name,legal_description\n"DOE","{big}"\n'.encode()
    assert _csv_has_a_lead(oversized) is False  # unreadable, so not counted


def test_a_blank_line_is_not_a_lead():
    from src.api.routes.batches import _csv_has_a_lead

    assert _csv_has_a_lead(b"party_name,address\n\n") is False
    # ...but a real row after a blank line still is one.
    assert _csv_has_a_lead(b"party_name,address\n\nDOE,123 Main St\n") is True


def test_lead_check_handles_crlf_and_invalid_utf8():
    from src.api.routes.batches import _csv_has_a_lead

    assert _csv_has_a_lead(b"party_name,address\r\n") is False
    assert _csv_has_a_lead(b"party_name,address\r\nDOE,123 Main St\r\n") is True
    # errors="replace": undecodable bytes must not raise out of the check.
    assert _csv_has_a_lead(b"party_name,address\nDOE,\xff\xfe bad\n") is True
