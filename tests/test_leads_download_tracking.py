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
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select
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


async def test_recording_an_unknown_user_is_a_no_op(db: AsyncSession):
    """A conditional UPDATE that matches nothing must not raise."""
    await mark_leads_downloaded(str(uuid.uuid4()))


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

@pytest.mark.parametrize(
    ("downloaded_at", "grandfathered", "expected"),
    [
        (None, False, False),
        (None, True, True),
        ("now", False, True),
    ],
)
async def test_activation_email_uses_the_observed_download(
    db: AsyncSession, starter_user: User, downloaded_at, grandfathered, expected
):
    """The email's has_download must agree with the checklist, not with export_key."""
    if downloaded_at == "now":
        starter_user.first_leads_downloaded_at = datetime.now(UTC)
    starter_user.onboarding_download_grandfathered = grandfathered
    await db.commit()

    refreshed = (
        await db.execute(select(User).where(User.id == starter_user.id))
    ).scalar_one()
    has_download = (
        refreshed.first_leads_downloaded_at is not None
        or bool(refreshed.onboarding_download_grandfathered)
    )
    assert has_download is expected


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
